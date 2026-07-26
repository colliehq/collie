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
                       capture_output=True, text=True)
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


def _build_cmd(exe, out, fps, webcam, mic, cam_size, margin):
    r = cam_size // 2
    args = [exe, "-hide_banner", "-y",
            "-f", "gdigrab", "-framerate", str(fps), "-i", "desktop"]
    if webcam:
        args += ["-f", "dshow", "-framerate", str(fps), "-i", "video=" + webcam]
    if mic:
        args += ["-f", "dshow", "-i", "audio=" + mic]

    if webcam:
        # scale+crop the camera to a square, then punch a circular alpha with geq, overlay bottom-left
        fc = (
            "[1:v]scale={s}:{s}:force_original_aspect_ratio=increase,crop={s}:{s},format=rgba,"
            "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
            "a='if(gt((X-{r})*(X-{r})+(Y-{r})*(Y-{r}),{r}*{r}),0,255)'[cam];"
            "[0:v][cam]overlay=x={m}:y=H-h-{m}[v]"
        ).format(s=cam_size, r=r, m=margin)
        args += ["-filter_complex", fc, "-map", "[v]"]
    else:
        args += ["-map", "0:v"]
    if mic:
        args += ["-map", "2:a" if webcam else "1:a"]

    args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if mic:
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
                             capture_output=True, text=True).stdout or ""
        return ("ffmpeg" in out.lower()) and (str(pid) in out)
    except Exception:
        return False


def start(webcam=None, mic=None, fps=30, cam_size=240, margin=40, out=None,
          no_cam=False, no_mic=False):
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

    if out is None:
        out = os.path.join(_default_outdir(), time.strftime("collie-%Y%m%d-%H%M%S.mkv"))

    cmd = _build_cmd(exe, out, fps, webcam, mic, cam_size, margin)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
    # give ffmpeg a moment to fail fast on a bad device/filter, so we don't report a phantom success
    time.sleep(1.2)
    if p.poll() is not None:
        _clear()
        return ("failed to start (ffmpeg exited immediately — likely a device name).\n"
                "  see your devices:  collie record devices\n"
                "  cmd: %s" % " ".join('"%s"' % a if " " in a else a for a in cmd))
    _save({"pid": p.pid, "out": out, "started": time.time(), "webcam": webcam, "mic": mic})
    bits = "screen"
    if webcam:
        bits += " + webcam bubble (%s)" % webcam
    if mic:
        bits += " + mic (%s)" % mic
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
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
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
                           capture_output=True)
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
