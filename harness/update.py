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


def apply_macos(dmg, on_note=print):
    """Replace /Applications/Collie.app from a verified dmg. Returns (ok, detail)."""
    ok, why = verify_macos(dmg)
    on_note("  verify: %s" % why)
    if not ok:
        return False, why

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
        return True, "installed to " + APP_PATH
    finally:
        subprocess.run(["hdiutil", "detach", mnt, "-quiet"], capture_output=True, timeout=120)
        shutil.rmtree(mnt, ignore_errors=True)


def apply_windows(exe, digest, on_note=print):
    """Re-run Collie-Setup.exe over the existing install. Returns (ok, detail).

    Collie-Setup.exe is NOT code-signed — the PE certificate table is empty — so unlike macOS there
    is no signature to check and Windows already shows an unknown-publisher warning on first run.
    The digest GitHub publishes is therefore the only integrity check there is, and it is required:
    without it this would be "download an executable and run it", which is not something to do
    quietly on a user's machine.
    """
    ok, why = verify_digest(exe, digest)
    on_note("  verify: %s" % why)
    if not ok:
        return False, why
    # /SILENT keeps the wizard out of the way on an upgrade; /NORESTART because collie never needs
    # one and a surprise reboot is worse than a stale process.
    r = subprocess.run([exe, "/SILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
                       capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        return False, "installer exited %d: %s" % (r.returncode,
                                                   (r.stdout or r.stderr or "").strip()[:160])
    return True, "reinstalled over the existing copy"


def apply_pip(wheel_url, on_note=print):
    """Upgrade the installed package straight from the release wheel (collie is not on PyPI)."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", wheel_url]
    on_note("  %s" % " ".join(cmd[-3:]))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
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
