"""Screen recording with a circular webcam bubble (Loom / Reframe style), built on ffmpeg — no
third-party recorder. `collie record start` captures the desktop + a circular webcam overlay in the
bottom-left corner + the microphone into an .mkv; `collie record stop` ends it and also leaves an
.mp4. State lives in ~/.collie/record.json so start and stop are separate CLI invocations.

One capture command, two backends — only the `-f`/`-i` input section differs, so the encode and the
offline bubble composite are shared:
  Windows  gdigrab (screen, incl. a single window by title) + dshow (camera/mic)
  macOS    avfoundation, where every screen and camera is its own device index and a region is a
           crop of the whole display (there is no offset capture, and no window selector)
ffmpeg must be on PATH (winget install Gyan.FFmpeg / brew install ffmpeg). macOS additionally needs
the Screen Recording TCC grant for whatever runs collie, or the capture yields no frames.

The container is Matroska on purpose — it stays playable even if the recorder is hard-killed, so
`stop` can never leave a corrupt file.
"""
import glob
import json
import os
import re
import shutil
import signal
import subprocess
import time

from . import plat

STATE_DIR = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
STATE = os.path.join(STATE_DIR, "record.json")

# CREATE_NO_WINDOW — every helper subprocess (ffmpeg probe, tasklist, taskkill, remux) MUST run
# windowless. Without it, a GUI/pythonw caller (the web record button, the desktop app) with no console
# of its own pops a black CMD window on every call — and the status poll + stop loop make them flash
# "frantically". The recording ffmpeg gets it too (see start()).
_NOWIN = 0x08000000 if os.name == "nt" else 0


def _require_capture_os():
    """Screen/camera capture is an ffmpeg *input device*, and which one exists is per-OS: gdigrab +
    dshow on Windows, avfoundation on macOS. Linux (x11grab/pipewire) isn't wired up yet — say so
    plainly rather than emitting a command whose -f its ffmpeg has never heard of."""
    if not (plat.is_windows() or plat.is_macos()):
        raise RuntimeError(
            "collie record has capture backends for Windows (gdigrab + dshow) and macOS "
            "(avfoundation); this is %s, where it isn't wired up yet. Everything else (`collie`, "
            "`collie web`, `collie app`) runs natively." % plat.os_label())


def _ffmpeg():
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    # winget just installed it but PATH isn't refreshed until the next login — look where it lands so
    # `collie record` works immediately after `winget install Gyan.FFmpeg`.
    la = os.environ.get("LOCALAPPDATA", "")
    for pat in (os.path.join(la, "Microsoft", "WinGet", "Links", "ffmpeg.exe"),
                os.path.join(la, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "ffmpeg.exe")):
        hits = glob.glob(pat, recursive=True)
        if hits:
            return hits[0]
    how = ("winget install Gyan.FFmpeg  (then reopen your terminal, or collie will find it "
           "automatically)" if plat.is_windows() else
           "brew install ffmpeg" if plat.is_macos() else "your package manager, e.g. apt install ffmpeg")
    raise RuntimeError("ffmpeg not found — install it:  %s" % how)


def _default_outdir():
    # ~/Videos on Windows/Linux; macOS names the same folder ~/Movies.
    vids = os.path.join(os.path.expanduser("~"), "Movies" if plat.is_macos() else "Videos")
    d = os.path.join(vids if os.path.isdir(vids) else os.path.expanduser("~"), "Collie")
    os.makedirs(d, exist_ok=True)
    return d


def list_dshow_devices():
    """(cameras, microphones) as ffmpeg sees them — the exact names dshow needs. Windows only."""
    exe = _ffmpeg()
    p = subprocess.run([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                       capture_output=True, text=True, creationflags=_NOWIN)
    text = (p.stderr or "") + (p.stdout or "")
    cams, mics = [], []
    for line in text.splitlines():
        m = re.search(r'"([^"]+)"', line)
        if not m:
            continue
        if "(video)" in line:
            cams.append(m.group(1))
        elif "(audio)" in line:
            mics.append(m.group(1))
    return cams, mics


# avfoundation prints one device per line, prefixed by its own log tag:
#   [AVFoundation indev @ 0x14d004080] AVFoundation video devices:
#   [AVFoundation indev @ 0x14d004080] [0] FaceTime HD Camera
#   [AVFoundation indev @ 0x14d004080] [1] Capture screen 0
# The tag carries brackets too, so strip it before reading the index off the front.
_AVF_TAG = re.compile(r"^\[AVFoundation indev @ [^\]]*\]\s*")
_AVF_ROW = re.compile(r"^\[(\d+)\]\s+(.*\S)")


def list_avfoundation_devices():
    """([(idx, name)] video, [(idx, name)] audio) exactly as avfoundation numbers them — the index IS
    the address ffmpeg takes (`-i "1:0"`), and names are not unique (two identical USB cams), so the
    index is what we keep. Screens show up among the VIDEO devices as "Capture screen N"."""
    exe = _ffmpeg()
    p = subprocess.run([exe, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                       capture_output=True, text=True)      # exits nonzero by design; output is stderr
    vids, auds, cur = [], [], None
    for line in ((p.stderr or "") + (p.stdout or "")).splitlines():
        line = _AVF_TAG.sub("", line).strip()
        if line.endswith("video devices:"):
            cur = vids; continue
        if line.endswith("audio devices:"):
            cur = auds; continue
        m = _AVF_ROW.match(line)
        if m and cur is not None:
            cur.append((int(m.group(1)), m.group(2)))
    return vids, auds


def _is_screen(name):
    return name.lower().startswith("capture screen")


def list_capture_devices():
    """(cameras, microphones) by NAME, for the picker and for `--webcam`/`--mic`. Screens are excluded
    from the camera list — they're a capture SOURCE, selected with --monitor, not a webcam."""
    _require_capture_os()
    if plat.is_windows():
        return list_dshow_devices()
    vids, auds = list_avfoundation_devices()
    return [n for _i, n in vids if not _is_screen(n)], [n for _i, n in auds]


def _avf_index(name, kind):
    """Resolve a device NAME back to the avfoundation index ffmpeg addresses it by. Exact match first,
    then case-insensitive substring, so `--mic "MacBook Pro Mic"` finds the full name."""
    vids, auds = list_avfoundation_devices()
    pool = auds if kind == "audio" else [(i, n) for i, n in vids if not _is_screen(n)]
    for i, n in pool:
        if n == name:
            return i
    low = str(name).lower()
    for i, n in pool:
        if low in n.lower():
            return i
    raise ValueError("no %s device matching %r — see:  collie record devices" % (kind, name))


def list_screens():
    """['1: 3024x1964 @ 0,0', ...] — the displays `--monitor N` can pick, labelled per backend: a
    virtual-desktop rect on Windows, the device name on macOS (avfoundation exposes no geometry)."""
    if plat.is_macos():
        try:
            return ["%d: %s" % (n, name) for n, (_i, name) in enumerate(_screen_devices(), 1)]
        except Exception:
            return []
    return ["%d: %dx%d @ %d,%d" % (n, w, h, x, y) for n, (x, y, w, h) in enumerate(_monitors(), 1)]


# "1920x1080@[15.000000 30.000000]fps" — the rates a camera will actually accept
_AVF_MODE = re.compile(r"@\[([\d.\s]+)\]fps")


def _avf_camera_rate(idx, want):
    """The framerate to open camera `idx` at. avfoundation REJECTS any rate the device doesn't
    advertise, and when given none it defaults to 29.97 — which Mac cameras list as 30, not 29.97, so
    an unspecified rate kills the whole capture on input #1:

        Selected framerate (29.970030) is not supported by the device.

    Asking with a deliberately impossible rate makes ffmpeg print the supported modes; pick the
    closest one at or below `want` so --fps 60 degrades to the camera's 30 instead of failing."""
    try:
        p = subprocess.run([_ffmpeg(), "-hide_banner", "-f", "avfoundation", "-framerate", "1",
                            "-i", "%d:" % idx, "-t", "0.1", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=20, creationflags=_NOWIN)
    except Exception:
        return 30
    rates = set()
    for m in _AVF_MODE.finditer((p.stderr or "") + (p.stdout or "")):
        for tok in m.group(1).split():
            try:
                rates.add(float(tok))
            except ValueError:
                pass
    if not rates:
        return 30
    at_or_below = [r for r in rates if r <= want + 0.01]
    return int(round(max(at_or_below) if at_or_below else min(rates)))


def _monitors():
    """[(x, y, w, h), ...] for each display in virtual-desktop coordinates (Windows). Best-effort;
    the process is made DPI-aware first so the rects match what gdigrab captures."""
    if not plat.is_windows():
        return []
    import ctypes
    from ctypes import wintypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor-v2, so coords are physical px
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    mons = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                              ctypes.POINTER(wintypes.RECT), ctypes.c_double)

    def _cb(hmon, hdc, lprc, data):
        r = lprc.contents
        mons.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
        return 1

    try:
        ctypes.windll.user32.EnumDisplayMonitors(0, 0, proc(_cb), 0)
    except Exception:
        return []
    # left-to-right, top-to-bottom so `--monitor 1` is the leftmost — matches how people count screens
    mons.sort(key=lambda m: (m[0], m[1]))
    return mons


def _screen_devices():
    """[(avfoundation index, name)] for each display, in the order avfoundation reports them (macOS)."""
    vids, _auds = list_avfoundation_devices()
    return [(i, n) for i, n in vids if _is_screen(n)]


def resolve_screen(monitor=None):
    """macOS: the avfoundation VIDEO INDEX of the display to capture. Unlike gdigrab, avfoundation has
    no virtual-desktop coordinate space — each screen is its own device, so `--monitor` selects a
    device rather than an (x, y, w, h) rect. 1-based, matching how people count screens."""
    screens = _screen_devices()
    if not screens:
        raise RuntimeError(
            "ffmpeg reported no 'Capture screen' device. On macOS that is almost always the Screen "
            "Recording permission: System Settings → Privacy & Security → Screen & System Audio "
            "Recording → enable your terminal, then restart it.")
    i = (int(monitor) - 1) if monitor else 0
    if i < 0 or i >= len(screens):
        raise ValueError("monitor %s out of range (found %d: %s)"
                         % (monitor, len(screens), ", ".join(n for _i, n in screens)))
    return screens[i][0]


def resolve_region(monitor=None, region=None):
    """Return (x, y, w, h) for gdigrab, or None for the whole virtual desktop.
    `region` = 'X,Y,W,H'; `monitor` = 1-based index into resolve of the displays."""
    if region:
        parts = [int(p) for p in str(region).replace("x", ",").replace("+", ",").split(",") if p != ""]
        if len(parts) == 4:
            return tuple(parts)
        raise ValueError("--region must be 'X,Y,W,H'")
    if monitor:
        mons = _monitors()
        i = int(monitor) - 1
        if not mons:
            raise RuntimeError("could not enumerate monitors")
        if i < 0 or i >= len(mons):
            raise ValueError("monitor %s out of range (found %d: %s)" % (
                monitor, len(mons), ", ".join("%dx%d@%d,%d" % (w, h, x, y) for (x, y, w, h) in mons)))
        return mons[i]
    return None


def _bubble_post_filter(cam_size, mirror, position, margin):
    """OFFLINE filter_complex: the webcam stream [0:v:1] -> a circular bubble with a white ring + soft
    drop shadow, overlaid on the screen stream [0:v:0] at the chosen corner, producing [v]. Runs in
    stop() on the recorded file, where the two streams are already frame-aligned — no live-capture sync,
    so it composites fast (~8x realtime) and the shadow is affordable."""
    s = int(cam_size)
    ring = max(3, s // 48)
    d = s + 2 * ring
    s2, d2 = s / 2.0, d / 2.0
    rr = "((X-{d2})*(X-{d2})+(Y-{d2})*(Y-{d2}))".format(d2=d2)
    flip = "hflip," if mirror else ""
    x, y = {
        "bl": ("%d" % margin, "H-h-%d" % margin),
        "br": ("W-w-%d" % margin, "H-h-%d" % margin),
        "tl": ("%d" % margin, "%d" % margin),
        "tr": ("W-w-%d" % margin, "%d" % margin),
    }.get(position, ("%d" % margin, "H-h-%d" % margin))
    return (
        "[0:v:1]{flip}scale={d}:{d}:force_original_aspect_ratio=increase,crop={d}:{d},format=rgba,geq="
        "r='if(gt({rr},{s2}*{s2}),255,r(X,Y))':"
        "g='if(gt({rr},{s2}*{s2}),255,g(X,Y))':"
        "b='if(gt({rr},{s2}*{s2}),255,b(X,Y))':"
        "a='if(gt({rr},{d2}*{d2}),0,255)'[bub];"
        "[bub]split[bs][bm];"
        "[bs]format=rgba,geq=r=0:g=0:b=0:a='0.5*alpha(X,Y)',boxblur=7:1[sh];"
        "[0:v:0][sh]overlay=x={x}+5:y={y}+5[t];"
        "[t][bm]overlay=x={x}:y={y}[v]"
    ).format(flip=flip, d=d, rr=rr, s2=s2, d2=d2, x=x, y=y)


def _build_cmd(exe, out, fps, webcam, mic, sysaudio, region, window, screen=0):
    """CAPTURE command: record the source (a specific window / a region / the whole desktop) + optional
    webcam + optional mic / system audio as SEPARATE streams, no filtering. Compositing two live
    captures through one overlay in real time stalls the pipeline to ~2fps on Windows (gdigrab + dshow),
    so the circular bubble is composited afterwards in stop() from the recorded file (fast). Output is
    MPEG-TS with a per-packet flush, so a hard kill on stop loses nothing.

    A single WINDOW is also the smooth path: it's far smaller than a 5120x1440 desktop, so it captures
    at a real 30fps."""
    args = [exe, "-hide_banner", "-y"]
    crop = "crop=trunc(iw/2)*2:trunc(ih/2)*2"
    if plat.is_macos():
        # avfoundation addresses inputs as "<video>:<audio>", one DEVICE per -i, so the screen, the
        # camera and each audio source are separate inputs — same input numbering as the dshow path
        # below, which is why everything downstream is shared.
        args += ["-f", "avfoundation", "-capture_cursor", "1", "-framerate", str(fps),
                 "-i", "%d:" % screen]
        if region:
            # no -offset_x here: avfoundation always hands over the whole display, so a region is a
            # crop of it. Forced even for libx264/yuv420p, same reason as the default crop.
            rx, ry, rw, rh = region
            crop = "crop=trunc(%d/2)*2:trunc(%d/2)*2:%d:%d" % (rw, rh, rx, ry)
        if webcam:
            # the camera gets its OWN rate, negotiated against what it advertises (see
            # _avf_camera_rate) — it rarely matches the screen's. The offline overlay in stop()
            # reconciles the two.
            ci = _avf_index(webcam, "video")
            args += ["-f", "avfoundation", "-framerate", str(_avf_camera_rate(ci, fps)),
                     "-i", "%d:" % ci]
        if mic:
            args += ["-f", "avfoundation", "-i", ":%d" % _avf_index(mic, "audio")]
        if sysaudio:
            args += ["-f", "avfoundation", "-i", ":%d" % _avf_index(sysaudio, "audio")]
    else:
        args += ["-f", "gdigrab", "-framerate", str(fps)]
        if window:
            args += ["-i", "title=" + window]       # capture just that window (gdigrab title=)
        else:
            if region:
                rx, ry, rw, rh = region
                args += ["-offset_x", str(rx), "-offset_y", str(ry), "-video_size", "%dx%d" % (rw, rh)]
            args += ["-i", "desktop"]
        if webcam:
            args += ["-f", "dshow", "-framerate", str(fps), "-i", "video=" + webcam]
        if mic:
            args += ["-f", "dshow", "-i", "audio=" + mic]
        if sysaudio:
            args += ["-f", "dshow", "-i", "audio=" + sysaudio]

    # crop the source to EVEN width/height — a captured window is often an odd size (e.g. 1263x1415),
    # and libx264 with yuv420p refuses odd dimensions ("width not divisible by 2"). No-op when already
    # even (full desktop / most regions).
    args += ["-map", "0:v", "-filter:v:0", crop,
             "-c:v:0", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if webcam:
        args += ["-map", "1:v", "-c:v:1", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    abase = 2 if webcam else 1
    n_a = 0
    if mic:
        args += ["-map", "%d:a" % abase]; n_a += 1
    if sysaudio:
        args += ["-map", "%d:a" % (abase + (1 if mic else 0))]; n_a += 1
    if n_a:
        args += ["-c:a", "aac", "-b:a", "160k"]
    args += ["-flush_packets", "1", out]
    return args


def _postprocess(src, webcam, has_mic, has_sys, cam_size, margin, position, mirror):
    """Turn the raw multi-stream .ts into a clean .mp4: composite the circular webcam bubble (if a cam
    was recorded) and mix mic+system audio. Returns the .mp4 path on success, else None."""
    dst = src[:-3] + ".mp4" if src.lower().endswith(".ts") else src + ".mp4"
    args = [_ffmpeg(), "-hide_banner", "-y", "-i", src]
    parts = []
    vmap = "0:v:0"
    if webcam:
        parts.append(_bubble_post_filter(cam_size, mirror, position, margin))
        vmap = "[v]"
    amap = None
    if has_mic and has_sys:
        parts.append("[0:a:0][0:a:1]amix=inputs=2:duration=longest:dropout_transition=0,dynaudnorm[a]")
        amap = "[a]"
    elif has_mic or has_sys:
        amap = "0:a:0"
    if parts:
        args += ["-filter_complex", ";".join(parts)]
    args += ["-map", vmap]
    if amap:
        args += ["-map", amap]
    if webcam:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]      # no bubble to composite — just remux the screen stream, instant
    if amap:
        args += ["-c:a", "aac", "-b:a", "160k"]
    args += ["-movflags", "+faststart", dst]
    try:
        subprocess.run(args, capture_output=True, creationflags=_NOWIN)
    except Exception:
        return None
    return dst if (os.path.exists(dst) and os.path.getsize(dst) > 1024) else None


def _load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return None


def _save(d):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(STATE, "w") as f:
        json.dump(d, f)


def _clear():
    try:
        os.remove(STATE)
    except Exception:
        pass


def _alive(pid):
    if not pid:
        return False
    try:
        if not plat.is_windows():
            # `ps -o command=` rather than a bare kill(pid, 0): pids are recycled, and confirming the
            # process is still an ffmpeg is what stops us reporting some unrelated new process as a
            # live recording (the same check the tasklist branch makes).
            out = subprocess.run(["ps", "-p", str(int(pid)), "-o", "command="],
                                 capture_output=True, text=True).stdout or ""
            return "ffmpeg" in out.lower()
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
                             capture_output=True, text=True, creationflags=_NOWIN).stdout or ""
        return ("ffmpeg" in out.lower()) and (str(pid) in out)
    except Exception:
        return False


def _kill(pid):
    """Hard-kill the recorder by pid. stop() runs in a DIFFERENT process from start(), so there is no
    Popen to hand to plat.kill_tree — only the pid from the state file."""
    try:
        if plat.is_windows():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, creationflags=_NOWIN)
        else:
            os.kill(int(pid), signal.SIGKILL)
    except Exception:
        pass


def start(webcam=None, mic=None, sysaudio=None, fps=30, cam_size=240, margin=40,
          position="bl", mirror=True, monitor=None, region=None, window=None, out=None,
          no_cam=False, no_mic=False, countdown=0):
    _require_capture_os()
    exe = _ffmpeg()
    st = _load()
    if st and _alive(st.get("pid")):
        return ("already recording -> %s (pid %s)\n  stop it first:  collie record stop"
                % (st.get("out"), st.get("pid")))

    cams, mics = [], []
    try:
        cams, mics = list_capture_devices()
    except Exception:
        pass
    webcam = None if no_cam else (webcam or (cams[0] if cams else None))
    mic = None if no_mic else (mic or (mics[0] if mics else None))
    # system audio is opt-in only: no reliable way to auto-pick a loopback device (a Windows dshow
    # loopback, or BlackHole/Loopback on macOS — neither is present by default)
    screen = 0
    if plat.is_macos():
        if window:
            return ("recording a single window isn't available on macOS — avfoundation captures whole "
                    "displays, with no title= selector like gdigrab's.\n"
                    "  use:  collie record start --region X,Y,W,H   (or --monitor N)")
        screen = resolve_screen(monitor=monitor)            # avfoundation device index for the display
        reg = resolve_region(region=region)                 # a region is cropped from it, not offset
    else:
        reg = resolve_region(monitor=monitor, region=region)  # raises with a clear message on bad input

    if out is None:
        out = os.path.join(_default_outdir(), time.strftime("collie-%Y%m%d-%H%M%S.ts"))

    # a window source and a region are mutually exclusive; a chosen window wins.
    if window:
        reg = None
    cmd = _build_cmd(exe, out, fps, webcam, mic, sysaudio, reg, window, screen=screen)

    for n in range(int(countdown or 0), 0, -1):
        print("  recording in %d..." % n, flush=True)
        time.sleep(1)

    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | _NOWIN) if plat.is_windows() else 0
    # capture ffmpeg's stderr to a log so a failure (busy device, filter stall) is diagnosable instead
    # of a silent 0-byte file. The child keeps its own inherited handle, so closing ours here is fine.
    os.makedirs(STATE_DIR, exist_ok=True)
    logf = open(os.path.join(STATE_DIR, "record-ffmpeg.log"), "w", encoding="utf-8", errors="replace")
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=logf, creationflags=flags)
    logf.close()
    # Confirm the pipeline actually STARTS WRITING before we call it a recording. The webcam-bubble
    # composite has a ~2s cold start on a big desktop, and on a very large one it can stall at frame 0
    # forever (the real-time overlay can't keep up). Watch the output grow for a few seconds; if it
    # doesn't, kill it and tell the user NOW instead of letting them record into a 0-byte void.
    logpath = os.path.join(STATE_DIR, "record-ffmpeg.log")
    started_ok = False
    for _ in range(15):                       # ~6s grace
        time.sleep(0.4)
        if p.poll() is not None:              # ffmpeg died (bad device, etc.)
            break
        if os.path.exists(out) and os.path.getsize(out) > 8192:   # low: a small window is low-bitrate;
            started_ok = True                                     # a real stall stays at 0 bytes
            break
    if not started_ok:
        _kill(p.pid)
        _clear()
        try:
            os.remove(out)
        except Exception:
            pass
        hint = ""
        if plat.is_macos():
            # The overwhelmingly common cause on macOS, and one no ffmpeg log explains well: without
            # the TCC grant the capture opens and then produces nothing. Name the exact pane.
            hint = ("\n  on macOS this is usually the permission, not the device: System Settings → "
                    "Privacy & Security → Screen & System Audio Recording → enable your terminal "
                    "(and Camera/Microphone for the bubble + mic), then restart the terminal.")
        return ("recording didn't start — no frames were written (a device may be busy or the name "
                "wrong).%s\n  check your devices:  collie record devices\n  ffmpeg log: %s"
                % (hint, logpath))
    _save({"pid": p.pid, "out": out, "started": time.time(),
           "webcam": webcam, "mic": mic, "sysaudio": sysaudio, "region": reg, "window": window,
           "cam_size": cam_size, "margin": margin, "position": position, "mirror": mirror})
    if window:
        bits = "window “%s”" % (window[:40])
    elif reg:
        bits = "region [%dx%d]" % (reg[2], reg[3])
    else:
        bits = "screen [full desktop]"
    if webcam:
        bits += " + webcam bubble @%s (%s)" % (position, webcam)
    if mic and sysaudio:
        bits += " + mic + system audio"
    elif mic:
        bits += " + mic"
    elif sysaudio:
        bits += " + system audio"
    return ("recording: %s\n  -> %s  (pid %d)\n  stop with:  collie record stop" % (bits, out, p.pid))


def _wait_gone(pid, secs):
    for _ in range(int(secs * 5)):
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return not _alive(pid)


def stop(remux_mp4=True):
    st = _load()
    if not st or not _alive(st.get("pid")):
        _clear()
        return "not recording"
    pid, out = st["pid"], st.get("out")
    # ffmpeg runs windowless (CREATE_NO_WINDOW) so a CTRL_BREAK can't reach it — that old graceful path
    # just wasted ~5s before the red dot cleared. Kill it outright; the .ts (per-packet flushed) holds
    # every captured frame regardless.
    _kill(pid)
    _wait_gone(pid, 3)
    _clear()
    dur = time.time() - st.get("started", time.time())
    sz = os.path.getsize(out) if (out and os.path.exists(out)) else 0
    if sz < 16384:   # header only / nothing captured — tell the truth, not a phantom "saved"
        return ("recording FAILED — no frames were captured (%d bytes; a device may have been busy).\n"
                "  ffmpeg log: %s" % (sz, os.path.join(STATE_DIR, "record-ffmpeg.log")))
    # OFFLINE composite: bubble + audio mix from the raw multi-stream .ts into a clean .mp4 (fast, since
    # the streams are already frame-aligned). Drop the .ts on success; keep it if the composite failed.
    if not remux_mp4:
        return "saved -> %s  (%.0fs, %.1f MB)" % (out, dur, sz / 1048576.0)
    mp4 = _postprocess(out, bool(st.get("webcam")), bool(st.get("mic")), bool(st.get("sysaudio")),
                       st.get("cam_size", 240), st.get("margin", 40),
                       st.get("position", "bl"), st.get("mirror", True))
    if mp4:
        try:
            os.remove(out)
        except Exception:
            pass
        return "saved -> %s  (%.0fs, %.1f MB)" % (mp4, dur, os.path.getsize(mp4) / 1048576.0)
    return ("saved (raw) -> %s  (%.0fs, %.1f MB)\n  note: the bubble/audio composite step failed — the "
            "raw .ts is intact.\n  ffmpeg log: %s"
            % (out, dur, sz / 1048576.0, os.path.join(STATE_DIR, "record-ffmpeg.log")))


def status():
    st = _load()
    if st and _alive(st.get("pid")):
        return ("recording -> %s  (%.0fs, pid %s)"
                % (st.get("out"), time.time() - st.get("started", time.time()), st.get("pid")))
    return "not recording"


def list_windows():
    """Visible top-level window titles, for the record-source picker (gdigrab captures by title)."""
    if not plat.is_windows():
        return []
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    titles = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            t = (buf.value or "").strip()
            if t and t != "Program Manager" and t not in titles:
                titles.append(t)
        except Exception:
            pass
        return True

    try:
        user32.EnumWindows(proc(_cb), 0)
    except Exception:
        return []
    return titles


def list_recordings():
    """Recordings in the output dir, newest first: [{name, size, mb, mtime}]."""
    d = _default_outdir()
    out = []
    try:
        for name in os.listdir(d):
            if name.lower().endswith((".mp4", ".ts", ".mkv")):
                p = os.path.join(d, name)
                try:
                    stt = os.stat(p)
                    out.append({"name": name, "size": stt.st_size,
                                "mb": round(stt.st_size / 1048576.0, 1), "mtime": stt.st_mtime})
                except Exception:
                    pass
    except Exception:
        pass
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _safe_path(name):
    """A path inside the output dir for `name` (basename only, blocks traversal), or None."""
    d = _default_outdir()
    p = os.path.join(d, os.path.basename(name or ""))
    if os.path.dirname(os.path.abspath(p)) != os.path.abspath(d):
        return None
    return p


def play(name):
    p = _safe_path(name)
    if p and os.path.exists(p):
        try:
            os.startfile(p)   # opens in the default video player
            return True
        except Exception:
            return False
    return False


def reveal(name=None):
    """Open the recordings folder (Explorer)."""
    try:
        os.startfile(_default_outdir())
        return True
    except Exception:
        return False


def delete_recording(name):
    p = _safe_path(name)
    if not p or not os.path.exists(p):
        return False
    try:
        os.remove(p)
        return True
    except Exception:
        return False
