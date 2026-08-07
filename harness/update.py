"""collie self-update — check GitHub for a newer release and install it in place.

One codebase, three ways in, so three ways to update: the macOS .app is replaced from the signed
dmg, Windows re-runs Collie-Setup.exe, and a pip install upgrades from the release wheel. How collie
got here decides which, and it is detected rather than asked.

THE DOWNLOAD IS VERIFIED BEFORE IT IS INSTALLED. An updater that fetches a binary over the network
and runs it is a way to hand someone your machine, so on macOS the dmg has to satisfy Gatekeeper —
`spctl` must call it accepted and notarised by our Developer ID — before it is mounted, and the app
inside is checked again after. A build that cannot be verified is not installed, and says so.

Nothing here downloads anything unless asked: `collie update` reports, `collie update --yes` acts.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request

from . import plat
from . import __version__

REPO = os.environ.get("COLLIE_UPDATE_REPO", "colliehq/collie")
API = "https://api.github.com/repos/%s/releases/latest" % REPO
TEAM_ID = "58Y98W3QQK"          # the Developer ID the macOS builds are signed with
APP_PATH = "/Applications/Collie.app"


def _ver(s):
    """'v0.20.2' -> (0, 20, 2). Unparseable parts sort low rather than raising."""
    nums = re.findall(r"\d+", (s or "").strip().lstrip("vV"))
    return tuple(int(n) for n in nums[:3]) + (0,) * (3 - len(nums[:3]))


def latest():
    """The newest published release. Raises on network or API failure — a silent 'you are up to
    date' after a failed check is how machines stay on an old build for months."""
    req = urllib.request.Request(API, headers={"User-Agent": "collie-update/1.0",
                                               "X-GitHub-Api-Version": "2022-11-28"})
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:                                   # shared CI IPs hit the anonymous rate limit
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read().decode("utf-8"))
    return {"tag": d.get("tag_name") or "", "notes": (d.get("body") or "").strip(),
            "url": d.get("html_url") or "",
            "assets": {a["name"]: a["browser_download_url"] for a in (d.get("assets") or [])},
            # GitHub reports "sha256:<hex>" per asset. It is what makes the Windows path safe at
            # all: Collie-Setup.exe carries no Authenticode signature, so there is nothing in the
            # file itself to check, and this digest is the only integrity claim available.
            "digests": {a["name"]: (a.get("digest") or "") for a in (d.get("assets") or [])}}


def sha256_of(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_digest(path, claimed):
    """(ok, detail) against GitHub's "sha256:<hex>". Weaker than a signature — it proves the bytes
    are the ones the release lists, not who built them — but it is what Windows has until
    Collie-Setup.exe is signed, and it does stop a truncated or swapped download."""
    want = (claimed or "").split(":")[-1].strip().lower()
    if not want:
        return False, "the release publishes no digest for this asset"
    got = sha256_of(path)
    if got != want:
        return False, "sha256 mismatch (got %s…, expected %s…)" % (got[:12], want[:12])
    return True, "sha256 matches the release listing"


def install_kind():
    """How this copy of collie was installed: 'app' | 'setup' | 'brew' | 'pip'.

    Checked in order of specificity. The bundle is the only one that can be told for certain (the
    launcher exports COLLIE_BUNDLED and the interpreter lives inside the .app), so it is asked first.
    """
    here = os.path.abspath(sys.executable)
    if plat.is_windows():
        # The Inno installer lays the runtime down under %LOCALAPPDATA%\Programs\Collie; a pip
        # install on the same machine does not live there.
        root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Collie")
        if root and here.lower().startswith(root.lower()):
            return "setup"
        # A custom install dir (the shell offers a folder picker) won't match the default path, so
        # also look for the Inno uninstaller that sits at the install root (…/<install>/python/python.exe
        # -> <install>). Without this, a custom-dir install misdetects as 'pip' and update does nothing.
        try:
            inst = os.path.dirname(os.path.dirname(here))
            if any(f.lower().startswith("unins") and f.lower().endswith(".exe")
                   for f in os.listdir(inst)):
                return "setup"
        except OSError:
            pass
        return "pip"
    if os.environ.get("COLLIE_BUNDLED") or "/Collie.app/Contents/" in here:
        return "app"
    if "/Cellar/collie/" in os.path.abspath(__file__) or "/homebrew/" in here:
        return "brew"
    return "pip"


def check():
    """{'current', 'latest', 'newer': bool, ...}. Does not download anything."""
    rel = latest()
    return {"current": __version__, "latest": rel["tag"].lstrip("vV"),
            "newer": _ver(rel["tag"]) > _ver(__version__),
            "kind": install_kind(), "notes": rel["notes"], "url": rel["url"],
            "assets": rel["assets"], "digests": rel["digests"]}


def _download(url, dest, on_progress=None):
    with urllib.request.urlopen(urllib.request.Request(
            url, headers={"User-Agent": "collie-update/1.0"}), timeout=60) as r:
        total = int(r.headers.get("content-length") or 0)
        got = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if on_progress and total:
                    on_progress(got, total)
    return dest


def verify_macos(path):
    """(ok, detail). Gatekeeper's verdict, plus the signing team — both, because either alone can
    be satisfied by something we did not build. Refusing here is the entire point of the function:
    everything downstream mounts this file and copies an executable out of it."""
    try:
        r = subprocess.run(["spctl", "-a", "-vv", "-t", "open",
                            "--context", "context:primary-signature", path],
                           capture_output=True, text=True, timeout=120)
    except Exception as e:
        return False, "could not run spctl: %s" % e
    out = (r.stdout or "") + (r.stderr or "")
    if "accepted" not in out:
        return False, "Gatekeeper rejected the download: " + out.strip().replace("\n", " ")[:180]
    if "Notarized Developer ID" not in out:
        return False, "not notarised: " + out.strip().replace("\n", " ")[:180]
    try:
        c = subprocess.run(["codesign", "-dv", "--verbose=2", path],
                           capture_output=True, text=True, timeout=60)
        blob = (c.stdout or "") + (c.stderr or "")
    except Exception as e:
        return False, "could not read the signature: %s" % e
    if ("TeamIdentifier=" + TEAM_ID) not in blob:
        return False, "signed by an unexpected team (expected %s)" % TEAM_ID
    return True, "notarised, team %s" % TEAM_ID


def _loaded_slack_agents():
    """Loaded per-dog LaunchAgents whose old runtime must be replaced too."""
    if sys.platform != "darwin":
        return []
    directory = os.path.expanduser("~/Library/LaunchAgents")
    try:
        names = sorted(name[:-6] for name in os.listdir(directory)
                       if re.fullmatch(r"run\.collie\.slack\.[A-Za-z0-9_-]+\.plist", name))
    except OSError:
        return []
    loaded = []
    for label in names:
        target = "gui/%d/%s" % (os.getuid(), label)
        try:
            r = subprocess.run(["launchctl", "print", target],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if r.returncode == 0:
                loaded.append(label)
        except (OSError, subprocess.SubprocessError):
            pass
    return loaded


def _restart_slack_agents(labels):
    """Force loaded agents onto the newly swapped app; return failures."""
    if sys.platform != "darwin":
        return []
    failures = []
    uid = os.getuid()
    for label in labels:
        if not re.fullmatch(r"run\.collie\.slack\.[A-Za-z0-9_-]+", label):
            failures.append(label)
            continue
        target = "gui/%d/%s" % (uid, label)
        try:
            r = subprocess.run(["launchctl", "kickstart", "-k", target],
                               capture_output=True, text=True, timeout=30)
            if r.returncode == 0:
                continue
            # A loaded job can disappear during the app swap. Re-bootstrap its
            # stable plist instead of leaving it offline until next login.
            plist = os.path.expanduser("~/Library/LaunchAgents/%s.plist" % label)
            subprocess.run(["launchctl", "bootout", target], capture_output=True, timeout=15)
            r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, plist],
                               capture_output=True, text=True, timeout=30)
            if r.returncode != 0:
                failures.append(label)
        except (OSError, subprocess.SubprocessError):
            failures.append(label)
    return failures


def apply_macos(dmg, on_note=print):
    """Replace /Applications/Collie.app from a verified dmg. Returns (ok, detail)."""
    ok, why = verify_macos(dmg)
    on_note("  verify: %s" % why)
    if not ok:
        return False, why

    slack_agents = _loaded_slack_agents()
    mnt = tempfile.mkdtemp(prefix="collie-update-")
    try:
        r = subprocess.run(["hdiutil", "attach", dmg, "-nobrowse", "-quiet", "-mountpoint", mnt],
                           capture_output=True, text=True, timeout=180)
        if r.returncode != 0:
            return False, "could not mount the disk image: " + (r.stderr or "").strip()[:160]
        src = os.path.join(mnt, "Collie.app")
        if not os.path.isdir(src):
            return False, "no Collie.app inside the disk image"

        # Check the app itself, not just its container: notarisation of the dmg says nothing about
        # what someone may have put inside a repackaged one.
        a = subprocess.run(["spctl", "-a", "-vv", "-t", "exec", src],
                           capture_output=True, text=True, timeout=120)
        if "accepted" not in ((a.stdout or "") + (a.stderr or "")):
            return False, "the app inside the image is not accepted by Gatekeeper"

        staged = APP_PATH + ".new"
        shutil.rmtree(staged, ignore_errors=True)
        c = subprocess.run(["ditto", src, staged], capture_output=True, text=True, timeout=600)
        if c.returncode != 0:
            return False, "copy failed: " + (c.stderr or "").strip()[:160]

        old = APP_PATH + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.isdir(APP_PATH):
            os.rename(APP_PATH, old)            # keep the previous one until the swap succeeds
        try:
            os.rename(staged, APP_PATH)
        except OSError as e:
            if os.path.isdir(old):
                os.rename(old, APP_PATH)        # put it back rather than leave nothing installed
            return False, "could not replace %s: %s" % (APP_PATH, e)
        shutil.rmtree(old, ignore_errors=True)
        failed = _restart_slack_agents(slack_agents)
        if failed:
            on_note("  warning: could not restart Slack agent(s): %s" % ", ".join(failed))
        elif slack_agents:
            on_note("  restarted Slack agent(s): %s" % ", ".join(slack_agents))
        detail = "installed to " + APP_PATH
        if failed:
            detail += "; Slack restart failed for " + ", ".join(failed)
        return True, detail
    finally:
        subprocess.run(["hdiutil", "detach", mnt, "-quiet"], capture_output=True, timeout=120)
        shutil.rmtree(mnt, ignore_errors=True)


def _install_root():
    """The Inno install root, if this interpreter lives inside one. …/<root>/python/python.exe."""
    if not plat.is_windows():
        return ""
    here = os.path.abspath(sys.executable)
    root = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Collie")
    if root and here.lower().startswith(root.lower()):
        return root
    inst = os.path.dirname(os.path.dirname(here))
    try:
        if any(f.lower().startswith("unins") and f.lower().endswith(".exe") for f in os.listdir(inst)):
            return inst
    except OSError:
        pass
    return ""


def running_parts(root):
    """Which pieces of Collie are up right now, so the same ones can be brought back afterwards.

    Returned as plain strings (including ``slack:<launcher>``) rather than pids:
    everything here is about to be killed, so a pid is worthless by the time it
    would be used.
    """
    parts = []
    # The wallpaper and the app window are the SAME exe in two modes, told apart only by `--window`
    # on the command line. Port 8787 cannot do it: that is the server, and the wallpaper is holding
    # it open whether or not a window exists — using the port would pop a window open after every
    # update on a machine that only ever ran the wallpaper.
    ps = ("Get-CimInstance Win32_Process -Filter \"Name like 'collie-wallpaper%' or Name like 'cw-build%'\""
          " | ForEach-Object { $_.CommandLine }; "
          "Get-CimInstance Win32_Process -Filter \"Name = 'python.exe' or Name = 'pythonw.exe'\""
          " | ForEach-Object { $_.CommandLine }")
    try:
        out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=25,
                             **plat.no_window_kwargs()).stdout or ""
    except Exception:
        out = ""
    lines = [l for l in out.splitlines() if l.strip()]
    wallpaper_lines = [line for line in lines
                       if "collie-wallpaper" in line.lower() or "cw-build" in line.lower()]
    if any("--window" in l for l in wallpaper_lines):
        parts.append("window")
    if any("--window" not in l for l in wallpaper_lines):
        parts.append("wallpaper")

    # Inno closes every python/pythonw living under the install root.  Slack
    # listeners are launched by per-dog .pyw files outside that root, so record
    # exactly which of those launchers is active and bring only those back.
    # The filename is generated from a strict slug; retaining only the basename
    # also keeps it safe to embed in the post-install PowerShell script.
    kennel = os.path.join(os.path.expanduser("~"), ".collie")
    runtime = os.path.normcase(os.path.abspath(os.path.join(root, "python"))).replace("/", "\\")
    normalized = [os.path.normcase(line).replace("/", "\\") for line in lines]
    try:
        launchers = sorted(name for name in os.listdir(kennel)
                           if re.fullmatch(r"slack-[A-Za-z0-9_-]+\.pyw", name))
    except OSError:
        launchers = []
    for name in launchers:
        path = os.path.normcase(os.path.abspath(os.path.join(kennel, name))).replace("/", "\\")
        if any(runtime in line and path in line for line in normalized):
            parts.append("slack:" + name)
    try:
        import socket
        s = socket.create_connection(("127.0.0.1", 8677), timeout=0.6)
        s.close()
        parts.append("bridge")
    except OSError:
        pass
    return parts


# CREATE_NO_WINDOW went through plat.no_window_kwargs() instead: passing `creationflags` at all
# raises ValueError off Windows, and both call sites below sat outside any platform branch — the
# exact shape that once turned six Windows-only features into silent no-ops on macOS.

# Runs AFTER collie has exited. PowerShell, not python: the only interpreter guaranteed to exist
# outside the directory the installer is about to overwrite.
_BOOTSTRAP = r'''
$ErrorActionPreference = "Continue"
Start-Transcript -Path "{log}" -Append | Out-Null
"[collie-update] waiting for pid {pid} to exit"
for ($i = 0; $i -lt 120; $i++) {{
  if (-not (Get-Process -Id {pid} -ErrorAction SilentlyContinue)) {{ break }}
  Start-Sleep -Milliseconds 500
}}
"[collie-update] running the installer"
$p = Start-Process -FilePath "{exe}" -ArgumentList "/SILENT","/NORESTART","/SUPPRESSMSGBOXES" -Wait -PassThru
"[collie-update] installer exit code: $($p.ExitCode)"
if ($p.ExitCode -ne 0) {{ "[collie-update] installer FAILED — not restarting anything"; Stop-Transcript | Out-Null; exit $p.ExitCode }}
$pyw = "{root}\python\pythonw.exe"
if (-not (Test-Path $pyw)) {{ "[collie-update] no pythonw at $pyw"; Stop-Transcript | Out-Null; exit 1 }}
{restarts}
"[collie-update] done"
Stop-Transcript | Out-Null
'''

_RESTART = {
    "wallpaper": '"[collie-update] restarting wallpaper"\n'
                 'Start-Process -FilePath $pyw -ArgumentList "$env:USERPROFILE\\.collie\\wallpaper-boot.pyw" '
                 '-WindowStyle Hidden\nStart-Sleep -Seconds 3',
    "bridge":    '"[collie-update] restarting browser bridge"\n'
                 'Start-Process -FilePath $pyw -ArgumentList "$env:USERPROFILE\\.collie\\bridge-boot.pyw" '
                 '-WindowStyle Hidden\nStart-Sleep -Seconds 2',
    "window":    '"[collie-update] reopening the Collie window"\n'
                 'Start-Process -FilePath $pyw -ArgumentList "-m","harness.cli","app" '
                 '-WorkingDirectory "{root}\\python" -WindowStyle Hidden',
}


def _restart_script(part, root):
    if part in _RESTART:
        return _RESTART[part].replace("{root}", root)
    if part.startswith("slack:"):
        launcher = part.split(":", 1)[1]
        if not re.fullmatch(r"slack-[A-Za-z0-9_-]+\.pyw", launcher):
            return ""
        return ('"[collie-update] restarting %s"\n' % launcher
                + 'Start-Process -FilePath $pyw -ArgumentList '
                  '([char]34 + "$env:USERPROFILE\\.collie\\%s" + [char]34) '
                  '-WindowStyle Hidden\n' % launcher
                + 'Start-Sleep -Seconds 2')
    return ""


def apply_windows(exe, digest, on_note=print):
    """Re-run Collie-Setup.exe over the existing install. Returns (ok, detail).

    The digest GitHub publishes is checked first. Collie-Setup.exe IS Authenticode-signed now (Azure
    Trusted Signing, verified on the published asset), but the digest is kept: it binds the bytes to
    THIS release, which a signature does not.

    Then the awkward part. This updater usually runs from inside the very directory the installer is
    about to replace, and Inno closes whatever holds those files (CloseApplications defaults to yes)
    — so the installer kills the updater mid-wait. The install actually succeeded, but the process
    was gone before it could say so: `collie update --yes` printed a download progress bar, then
    exit code -1 and nothing else, and Collie stayed shut afterwards because the wizard's own
    relaunch step is `skipifsilent`.

    So when we are inside the install tree we do not run the installer at all. We hand it to a
    PowerShell bootstrap outside that tree, which waits for this process to exit, installs, and
    brings back exactly the pieces that were running. Reporting honestly matters here: this returns
    'handed off', not 'installed', because at that point it genuinely does not know yet.
    """
    ok, why = verify_digest(exe, digest)
    on_note("  verify: %s" % why)
    if not ok:
        return False, why

    # A Slack task is deliberately inside a kill-on-close Job Object. The
    # PowerShell bootstrap must outlive this Python process, so launching it
    # here would produce a convincing "handed off" and then have the guard kill
    # both bootstrap and installer. Refuse honestly; an interactive/app update
    # runs outside that ownership boundary.
    if os.environ.get("COLLIE_PROCESS_OWNER") == "slackexec":
        return False, ("self-update cannot be handed off from a Slack task; run `collie update "
                       "--yes` in a terminal or use the Collie app")

    root = _install_root()
    if not root:
        # Not inside the install tree (a pip-style layout): nothing will close us, so run it here
        # and report the real outcome.
        r = subprocess.run([exe, "/SILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
                           capture_output=True, text=True, timeout=1800,
                           **plat.no_window_kwargs())
        if r.returncode != 0:
            return False, "installer exited %d: %s" % (r.returncode,
                                                       (r.stdout or r.stderr or "").strip()[:160])
        return True, "reinstalled over the existing copy"

    parts = running_parts(root)
    log = os.path.join(tempfile.gettempdir(), "collie-update.log")
    restarts = "\n".join(line for line in (_restart_script(p, root) for p in parts) if line) or \
        '"[collie-update] nothing was running; not starting anything"'
    script = _BOOTSTRAP.format(pid=os.getpid(), exe=exe, root=root, log=log, restarts=restarts)
    sp = os.path.join(tempfile.gettempdir(), "collie-update.ps1")
    with open(sp, "w", encoding="utf-8") as f:
        f.write(script)

    # CREATE_NO_WINDOW ALONE. Not DETACHED_PROCESS: a detached console application gets no console
    # at all, and powershell.exe then exits without running a line — while Popen returns a healthy
    # process object, so the handoff looks like it worked and nothing ever happens. Measured, both
    # ways round. A child is not killed by its parent exiting on Windows, so nothing more is needed
    # for the bootstrap to outlive us.
    subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", sp],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     cwd=tempfile.gettempdir(), **plat.no_window_kwargs())
    on_note("  the installer runs once Collie exits; it will bring back: %s"
            % (", ".join(parts) or "nothing (none of it was running)"))
    on_note("  log: %s" % log)
    return True, "handed off to the installer (it restarts Collie itself)"


def apply_pip(wheel_url, on_note=print):
    """Upgrade the installed package straight from the release wheel (collie is not on PyPI)."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", wheel_url]
    on_note("  %s" % " ".join(cmd[-3:]))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900,
                       **plat.no_window_kwargs())
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "pip failed").strip().splitlines()[-1][:180]
    return True, "upgraded"


def apply_brew(on_note=print):
    if not shutil.which("brew"):
        return False, "brew is not on PATH"
    r = subprocess.run(["brew", "upgrade", "collie"], capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        return False, (r.stderr or "").strip().splitlines()[-1][:180] if r.stderr else "brew failed"
    return True, "upgraded"
