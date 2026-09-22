"""Pin the self-updater — most of all, that it REFUSES what it should.

An updater downloads code over the network and then runs it. The interesting tests are not the ones
where a good build installs; they are the ones where a bad build does not. So: a disk image nobody
signed, and a real notarised image with sixty-four bytes changed in the middle, both have to be
turned away — and the reason has to name what was wrong, because "update failed" teaches nobody
anything.

Deterministic and offline apart from the two tests that say they need the network.
"""
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from harness import update as up                                        # noqa: E402

failures = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        failures.append(what)


def test_version_compare():
    print("test_version_compare")
    cases = [("0.20.1", "v0.20.2", True), ("0.20.2", "v0.20.2", False),
             ("0.21.0", "v0.20.2", False), ("0.9.0", "v0.20.2", True),
             ("1.0.0", "v0.99.9", False), ("0.20.2", "0.20.10", True)]
    for cur, new, want in cases:
        got = up._ver(new) > up._ver(cur)
        check(got == want, "%s -> %s  needs update = %s" % (cur, new, want))
    # 0.9 must not beat 0.20: string comparison says it does, which is the classic version bug
    check(up._ver("v0.20.0") > up._ver("0.9.0"), "0.20 sorts above 0.9 (numeric, not lexical)")
    check(up._ver("garbage") == (0, 0, 0), "an unparseable tag sorts lowest instead of raising")


def test_install_kind_detects_the_bundle():
    print("test_install_kind_detects_the_bundle")
    old = os.environ.get("COLLIE_BUNDLED")
    real_isw = up.plat.is_windows
    up.plat.is_windows = lambda: False     # COLLIE_BUNDLED/.app is a macOS concept — test the mac path
    try:                                    # (else on Windows install_kind() short-circuits to setup/pip)
        os.environ["COLLIE_BUNDLED"] = "1"
        check(up.install_kind() == "app", "COLLIE_BUNDLED marks an .app install")
        os.environ.pop("COLLIE_BUNDLED", None)
        check(up.install_kind() in ("pip", "brew"), "otherwise pip or brew (%s)" % up.install_kind())
    finally:
        up.plat.is_windows = real_isw
        if old is None:
            os.environ.pop("COLLIE_BUNDLED", None)
        else:
            os.environ["COLLIE_BUNDLED"] = old


def _mkdmg(path, volname="Collie"):
    r = subprocess.run(["hdiutil", "create", "-size", "2m", "-fs", "HFS+",
                        "-volname", volname, "-quiet", path],
                       capture_output=True, timeout=180)
    return r.returncode == 0 and os.path.exists(path)


def test_refuses_an_unsigned_image():
    """The whole point. Nobody signed this, so it must not be mounted."""
    print("test_refuses_an_unsigned_image")
    if sys.platform != "darwin":
        print("  SKIP (macOS only)")
        return
    d = tempfile.mkdtemp(prefix="collie-updtest-")
    dmg = os.path.join(d, "unsigned.dmg")
    if not _mkdmg(dmg):
        print("  SKIP (hdiutil unavailable)")
        return
    ok, why = up.verify_macos(dmg)
    check(not ok, "an unsigned disk image is refused")
    check("reject" in why.lower() or "notaris" in why.lower() or "notariz" in why.lower(),
          "and the reason says why (%s)" % why[:60])
    # and apply_macos must refuse before it mounts anything
    ok2, why2 = up.apply_macos(dmg, on_note=lambda *_a: None)
    check(not ok2, "apply_macos stops at verification, before mounting")


def test_refuses_a_tampered_image():
    """A real notarised build with bytes changed in the middle. Needs the network and a release."""
    print("test_refuses_a_tampered_image")
    if sys.platform != "darwin" or os.environ.get("COLLIE_SKIP_NET"):
        print("  SKIP (macOS + network only)")
        return
    try:
        rel = up.latest()
    except Exception as e:
        print("  SKIP (release feed unreachable: %s)" % str(e)[:60])
        return
    name = next((n for n in rel["assets"] if n.endswith(".dmg")), "")
    if not name:
        print("  SKIP (no dmg in the latest release)")
        return
    d = tempfile.mkdtemp(prefix="collie-updtest-")
    good = os.path.join(d, name)
    try:
        up._download(rel["assets"][name], good)
    except Exception as e:
        print("  SKIP (download failed: %s)" % str(e)[:60])
        return
    ok, why = up.verify_macos(good)
    check(ok, "the real published dmg verifies (%s)" % why[:50])

    bad = os.path.join(d, "tampered.dmg")
    with open(good, "rb") as a, open(bad, "wb") as b:
        b.write(a.read())
    with open(bad, "r+b") as f:
        f.seek(os.path.getsize(bad) // 2)
        f.write(b"\x00" * 64)
    ok2, _why2 = up.verify_macos(bad)
    check(not ok2, "the same dmg with 64 bytes changed is refused")


def test_windows_digest_gate():
    """The release digest binds the signed installer bytes to one specific release."""
    print("test_windows_digest_gate")
    good = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    good.write(b"collie" * 5000)
    good.close()
    digest = "sha256:" + up.sha256_of(good.name)

    ok, why = up.verify_digest(good.name, digest)
    check(ok, "matching sha256 passes (%s)" % why[:40])

    with open(good.name, "r+b") as f:
        f.seek(100)
        f.write(b"\x00" * 8)
    ok2, why2 = up.verify_digest(good.name, digest)
    check(not ok2, "eight changed bytes are caught")
    check("mismatch" in why2, "and the reason says mismatch, with both digests (%s)" % why2[:44])

    ok3, why3 = up.verify_digest(good.name, "")
    check(not ok3, "no published digest means no install")
    check("no digest" in why3, "and says so rather than passing silently")
    os.unlink(good.name)


def test_windows_authenticode_gate_with_mocked_os_verifier():
    """Portable contract test: Windows status and the exact Azure publisher must both match."""
    print("test_windows_authenticode_gate_with_mocked_os_verifier")
    artifact = tempfile.NamedTemporaryFile(delete=False, suffix=".exe")
    artifact.write(b"mock PE fixture")
    artifact.close()
    real_windows = up.plat.is_windows
    real_isfile = up.os.path.isfile
    real_powershell = up._system_powershell
    real_run = up.subprocess.run
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"

    def verdict(status, publisher, subject=""):
        up.subprocess.run = lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=up.json.dumps({"status": status, "publisher": publisher,
                                  "subject": subject or ("CN=" + publisher if publisher else "")}),
            stderr="")
        return up.verify_windows_authenticode(artifact.name)

    try:
        up.plat.is_windows = lambda: True
        up._system_powershell = lambda: powershell
        up.os.path.isfile = lambda path: (
            path in (artifact.name, os.path.abspath(artifact.name), powershell))

        ok, why = verdict("Valid", "Daming Wu")
        check(ok, "a valid chain from CN=Daming Wu passes (%s)" % why)
        assert ok

        ok, why = verdict("NotSigned", "")
        check(not ok and "not valid" in why, "an unsigned installer is refused")
        assert not ok and "not valid" in why

        ok, why = verdict("Valid", "Another Publisher", "CN=Another Publisher")
        check(not ok and "publisher" in why.lower() and "Daming Wu" in why,
              "a valid signature from the wrong publisher is refused")
        assert not ok and "publisher" in why.lower() and "Daming Wu" in why
    finally:
        up.plat.is_windows = real_windows
        up._system_powershell = real_powershell
        up.os.path.isfile = real_isfile
        up.subprocess.run = real_run
        os.unlink(artifact.name)


def test_apply_windows_requires_digest_and_authenticode_before_execution():
    """The Windows execution path itself must call both independent release gates."""
    print("test_apply_windows_requires_digest_and_authenticode_before_execution")
    artifact = tempfile.NamedTemporaryFile(delete=False, suffix=".exe")
    artifact.write(b"signed installer fixture")
    artifact.close()
    saved = {name: getattr(up, name) for name in (
        "verify_digest", "verify_windows_authenticode", "_install_root",
        "begin_update_journal", "record_update_handoff")}
    real_run = up.subprocess.run
    real_owner = os.environ.pop("COLLIE_PROCESS_OWNER", None)
    calls = []
    try:
        up.verify_digest = lambda *_a, **_k: (True, "release digest matches")
        up._install_root = lambda: None
        up.begin_update_journal = lambda **_k: {}
        up.record_update_handoff = lambda **_k: {}
        up.subprocess.run = lambda argv, **_k: (
            calls.append(argv) or SimpleNamespace(returncode=0, stdout="", stderr=""))

        up.verify_windows_authenticode = lambda *_a, **_k: (False, "signature is not valid")
        ok, why = up.apply_windows(artifact.name, "sha256:fixture", on_note=lambda _m: None)
        check(not ok and not calls, "an invalid signature stops before executing the installer")
        assert not ok and not calls and "signature" in why

        up.verify_windows_authenticode = lambda *_a, **_k: (
            True, "Authenticode chain is valid; publisher CN=Daming Wu")
        ok, why = up.apply_windows(artifact.name, "sha256:fixture", on_note=lambda _m: None)
        check(ok and len(calls) == 1 and calls[0][0] == artifact.name,
              "matching digest plus trusted publisher reaches installer execution")
        assert ok and len(calls) == 1 and calls[0][0] == artifact.name
    finally:
        for name, value in saved.items():
            setattr(up, name, value)
        up.subprocess.run = real_run
        if real_owner is not None:
            os.environ["COLLIE_PROCESS_OWNER"] = real_owner
        os.unlink(artifact.name)


def test_check_does_not_download():
    """`collie update` with no --yes must not fetch a 135MB disk image just to say a version."""
    print("test_check_does_not_download")
    if os.environ.get("COLLIE_SKIP_NET"):
        print("  SKIP (network)")
        return
    calls = []
    real = up._download
    up._download = lambda *a, **k: calls.append(a) or ""
    try:
        info = up.check()
        check(not calls, "check() downloaded nothing")
        check(set(("current", "latest", "newer", "kind")) <= set(info), "check() reports the basics")
    except Exception as e:
        print("  SKIP (release feed unreachable: %s)" % str(e)[:60])
    finally:
        up._download = real


if __name__ == "__main__":
    test_version_compare()
    test_install_kind_detects_the_bundle()
    test_refuses_an_unsigned_image()
    test_refuses_a_tampered_image()
    test_windows_digest_gate()
    test_windows_authenticode_gate_with_mocked_os_verifier()
    test_apply_windows_requires_digest_and_authenticode_before_execution()
    test_check_does_not_download()
    print("\n" + ("all green" if not failures else "%d FAILED" % len(failures)))
    sys.exit(1 if failures else 0)
