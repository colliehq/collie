"""Screen recording with a circular webcam bubble (Loom / Reframe style), built on ffmpeg — no
third-party recorder. `collie record start` captures the desktop + a circular webcam overlay in the
bottom-left corner + the microphone into an .mkv; `collie record stop` ends it and also leaves an
.mp4. State lives in ~/.collie/record.json so start and stop are separate CLI invocations.

Windows-first: gdigrab for the screen, dshow for the camera/mic. ffmpeg must be on PATH
(winget install Gyan.FFmpeg). The container is Matroska on purpose — it stays playable even if the
recorder is hard-killed, so `stop` can never leave a corrupt file.
"""
import glob
import json
import os
import re
import shutil
import subprocess
import time

STATE_DIR = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
STATE = os.path.join(STATE_DIR, "record.json")

# CREATE_NO_WINDOW — every helper subprocess (ffmpeg probe, tasklist, taskkill, remux) MUST run
# windowless. Without it, a GUI/pythonw caller (the web record button, the desktop app) with no console
# of its own pops a black CMD window on every call — and the status poll + stop loop make them flash
# "frantically". The recording ffmpeg gets it too (see start()).
_NOWIN = 0x08000000 if os.name == "nt" else 0


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
    raise RuntimeError("ffmpeg not found — install it:  winget install Gyan.FFmpeg  "
                       "(then reopen your terminal, or collie will find it automatically)")


def _default_outdir():
    vids = os.path.join(os.path.expanduser("~"), "Videos")
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


def _monitors():
    """[(x, y, w, h), ...] for each display in virtual-desktop coordinates (Windows). Best-effort;
    the process is made DPI-aware first so the rects match what gdigrab captures."""
    if os.name != "nt":
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


def _build_cmd(exe, out, fps, webcam, mic, sysaudio, region, window):
    """CAPTURE command: record the source (a specific window / a region / the whole desktop) + optional
    webcam + optional mic / system audio as SEPARATE streams, no filtering. Compositing two live
    captures through one overlay in real time stalls the pipeline to ~2fps on Windows (gdigrab + dshow),
    so the circular bubble is composited afterwards in stop() from the recorded file (fast). Output is
    MPEG-TS with a per-packet flush, so a hard kill on stop loses nothing.

    A single WINDOW is also the smooth path: it's far smaller than a 5120x1440 desktop, so it captures
    at a real 30fps."""
    args = [exe, "-hide_banner", "-y", "-f", "gdigrab", "-framerate", str(fps)]
    if window:
        args += ["-i", "title=" + window]           # capture just that window (gdigrab title=)
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
    args += ["-map", "0:v", "-filter:v:0", "crop=trunc(iw/2)*2:trunc(ih/2)*2",
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
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % int(pid), "/NH"],
                             capture_output=True, text=True, creationflags=_NOWIN).stdout or ""
        return ("ffmpeg" in out.lower()) and (str(pid) in out)
    except Exception:
        return False


def start(webcam=None, mic=None, sysaudio=None, fps=30, cam_size=240, margin=40,
          position="bl", mirror=True, monitor=None, region=None, window=None, out=None,
          no_cam=False, no_mic=False, countdown=0):
    exe = _ffmpeg()
    st = _load()
    if st and _alive(st.get("pid")):
        return ("already recording -> %s (pid %s)\n  stop it first:  collie record stop"
                % (st.get("out"), st.get("pid")))

    cams, mics = [], []
    try:
        cams, mics = list_dshow_devices()
    except Exception:
        pass
    webcam = None if no_cam else (webcam or (cams[0] if cams else None))
    mic = None if no_mic else (mic or (mics[0] if mics else None))
    # system audio is opt-in only: there's no reliable way to auto-pick a loopback device on Windows
    reg = resolve_region(monitor=monitor, region=region)   # raises with a clear message on bad input

    if out is None:
        out = os.path.join(_default_outdir(), time.strftime("collie-%Y%m%d-%H%M%S.ts"))

    # a window source and a region are mutually exclusive; a chosen window wins.
    if window:
        reg = None
    cmd = _build_cmd(exe, out, fps, webcam, mic, sysaudio, reg, window)

    for n in range(int(countdown or 0), 0, -1):
        print("  recording in %d..." % n, flush=True)
        time.sleep(1)

    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | _NOWIN) if os.name == "nt" else 0
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
        try:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, creationflags=_NOWIN)
        except Exception:
            pass
        _clear()
        try:
            os.remove(out)
        except Exception:
            pass
        return ("recording didn't start — no frames were written (a device may be busy or the name "
                "wrong).\n  check your devices:  collie record devices\n  ffmpeg log: %s" % logpath)
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
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_NOWIN)
    except Exception:
        pass
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
    if os.name != "nt":
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
