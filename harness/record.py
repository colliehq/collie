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


def _bubble_filter(cam_idx, cam_size, mirror, position, margin):
    """filter_complex fragment that turns input `cam_idx` into a circular webcam bubble with a white
    ring + soft drop shadow, overlaid on [0:v] at the chosen corner. Ends producing [v]."""
    s = int(cam_size)
    ring = max(3, s // 48)          # white border thickness
    d = s + 2 * ring               # full bubble diameter
    s2, d2 = s / 2.0, d / 2.0
    rr = "((X-{d2})*(X-{d2})+(Y-{d2})*(Y-{d2}))".format(d2=d2)   # squared dist from bubble centre
    flip = "hflip," if mirror else ""
    xy = {
        "bl": ("%d" % margin, "H-h-%d" % margin),
        "br": ("W-w-%d" % margin, "H-h-%d" % margin),
        "tl": ("%d" % margin, "%d" % margin),
        "tr": ("W-w-%d" % margin, "%d" % margin),
    }.get(position, ("%d" % margin, "H-h-%d" % margin))
    x, y = xy
    # one geq at diameter d: camera inside radius s2, white in the ring band s2..d2, transparent outside
    bubble = (
        "[{ci}:v]{flip}scale={d}:{d}:force_original_aspect_ratio=increase,crop={d}:{d},format=rgba,"
        "geq="
        "r='if(gt({rr},{s2}*{s2}),255,r(X,Y))':"
        "g='if(gt({rr},{s2}*{s2}),255,g(X,Y))':"
        "b='if(gt({rr},{s2}*{s2}),255,b(X,Y))':"
        "a='if(gt({rr},{d2}*{d2}),0,255)'[bub];"
    ).format(ci=cam_idx, flip=flip, d=d, rr=rr, s2=s2, d2=d2)
    # split off a black, blurred silhouette as the drop shadow, offset a few px behind the bubble
    shadow = ("[bub]split[bs][bm];"
              "[bs]format=rgba,geq=r=0:g=0:b=0:a='0.45*alpha(X,Y)',boxblur=6:1[sh];")
    compose = ("[0:v][sh]overlay=x={x}+5:y={y}+5[t];"
               "[t][bm]overlay=x={x}:y={y}[v]").format(x=x, y=y)
    return bubble + shadow + compose


def _build_cmd(exe, out, fps, webcam, mic, sysaudio, cam_size, margin, position, mirror, region):
    args = [exe, "-hide_banner", "-y", "-f", "gdigrab", "-framerate", str(fps)]
    if region:
        rx, ry, rw, rh = region
        args += ["-offset_x", str(rx), "-offset_y", str(ry), "-video_size", "%dx%d" % (rw, rh)]
    args += ["-i", "desktop"]

    cam_idx = None
    if webcam:
        cam_idx = 1
        args += ["-f", "dshow", "-framerate", str(fps), "-i", "video=" + webcam]
    nxt = (2 if webcam else 1)
    mic_idx = sys_idx = None
    if mic:
        mic_idx = nxt; nxt += 1
        args += ["-f", "dshow", "-i", "audio=" + mic]
    if sysaudio:
        sys_idx = nxt; nxt += 1
        args += ["-f", "dshow", "-i", "audio=" + sysaudio]

    parts = []
    if webcam:
        parts.append(_bubble_filter(cam_idx, cam_size, mirror, position, margin))
        vmap = "[v]"
    else:
        vmap = "0:v"
    amap = None
    if mic_idx is not None and sys_idx is not None:
        parts.append("[%d:a][%d:a]amix=inputs=2:duration=longest:dropout_transition=0,"
                     "dynaudnorm[a]" % (mic_idx, sys_idx))
        amap = "[a]"
    elif mic_idx is not None:
        amap = "%d:a" % mic_idx
    elif sys_idx is not None:
        amap = "%d:a" % sys_idx

    if parts:
        args += ["-filter_complex", ";".join(parts)]
    args += ["-map", vmap]
    if amap:
        args += ["-map", amap]
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if amap:
        args += ["-c:a", "aac", "-b:a", "160k"]
    args += [out]
    return args


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
          position="bl", mirror=True, monitor=None, region=None, out=None,
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
        out = os.path.join(_default_outdir(), time.strftime("collie-%Y%m%d-%H%M%S.mkv"))

    cmd = _build_cmd(exe, out, fps, webcam, mic, sysaudio, cam_size, margin, position, mirror, reg)

    for n in range(int(countdown or 0), 0, -1):
        print("  recording in %d..." % n, flush=True)
        time.sleep(1)

    flags = (subprocess.CREATE_NEW_PROCESS_GROUP | _NOWIN) if os.name == "nt" else 0
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    # give ffmpeg a moment to fail fast on a bad device/filter, so we don't report a phantom success
    time.sleep(1.2)
    if p.poll() is not None:
        _clear()
        return ("failed to start (ffmpeg exited immediately — likely a bad device name or a system-"
                "audio device that isn't a loopback).\n  see your devices:  collie record devices\n"
                "  cmd: %s" % " ".join('"%s"' % a if " " in a else a for a in cmd))
    _save({"pid": p.pid, "out": out, "started": time.time(),
           "webcam": webcam, "mic": mic, "sysaudio": sysaudio, "region": reg})
    bits = "screen" + (" [%dx%d]" % (reg[2], reg[3]) if reg else " [full desktop]")
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
    # graceful finalize first (CTRL_BREAK == pressing q), then force. The .mkv stays playable either way.
    if os.name == "nt":
        try:
            import signal
            os.kill(int(pid), signal.CTRL_BREAK_EVENT)
        except Exception:
            pass
    if not _wait_gone(pid, 5):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_NOWIN)
        except Exception:
            pass
        _wait_gone(pid, 4)
    _clear()
    dur = time.time() - st.get("started", time.time())
    lines = ["saved -> %s  (%.0fs)" % (out, dur)]
    if remux_mp4 and out and out.lower().endswith(".mkv") and os.path.exists(out):
        mp4 = out[:-4] + ".mp4"
        try:
            subprocess.run([_ffmpeg(), "-hide_banner", "-y", "-i", out, "-c", "copy", mp4],
                           capture_output=True, creationflags=_NOWIN)
            if os.path.exists(mp4):
                lines.append("mp4   -> %s" % mp4)
        except Exception:
            pass
    return "\n".join(lines)


def status():
    st = _load()
    if st and _alive(st.get("pid")):
        return ("recording -> %s  (%.0fs, pid %s)"
                % (st.get("out"), time.time() - st.get("started", time.time()), st.get("pid")))
    return "not recording"
