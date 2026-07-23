"""Pin harness.plat — the OS-abstraction layer. Runs on the current OS; the cross-OS
branches are asserted structurally so the same test is meaningful on Linux/macOS/Windows.

Run: python tests/test_plat.py   (exit 0 = all green)
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import plat  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def test_detection():
    print("test_detection")
    check(isinstance(plat.os_label(), str) and plat.os_label(), "os_label is a non-empty string")
    check(plat.is_windows() == (os.name == "nt"), "is_windows tracks os.name")
    check(isinstance(plat.is_wsl(), bool) and isinstance(plat.is_macos(), bool), "detectors return bools")
    if plat.is_wsl():
        check(not plat.is_windows(), "WSL classifies as posix, not Windows")


def test_rmtree():
    print("test_rmtree")
    d = tempfile.mkdtemp()
    open(os.path.join(d, "f"), "w").write("x")
    plat.rmtree(d)
    check(not os.path.exists(d), "rmtree removed the tree")
    plat.rmtree(d)  # missing path
    check(True, "rmtree on a missing path does not raise")


def test_open_excl():
    print("test_open_excl")
    fd, p = tempfile.mkstemp()
    os.close(fd)
    os.unlink(p)
    fd = plat.open_excl(p)
    os.write(fd, b"x")
    os.close(fd)
    check(os.path.exists(p), "open_excl created the file")
    try:
        plat.open_excl(p)
        check(False, "open_excl over an existing file must fail (O_EXCL)")
    except FileExistsError:
        check(True, "open_excl refuses to overwrite")
    os.unlink(p)


def test_new_group_kwargs():
    print("test_new_group_kwargs")
    kw = plat.new_group_kwargs()
    if plat.is_windows():
        check(kw == {}, "windows: no special group flag (taskkill /T walks the tree)")
    else:
        check(kw == {"start_new_session": True}, "posix: start_new_session for group-kill")


def test_chmod_private():
    print("test_chmod_private")
    fd, p = tempfile.mkstemp()
    os.close(fd)
    plat.chmod_private(p)   # must NOT raise on any OS
    if not plat.is_windows():
        import stat
        check(stat.S_IMODE(os.stat(p).st_mode) == 0o600, "posix: chmod_private -> owner-only 0600")
    os.unlink(p)
    check(True, "chmod_private is safe on every OS")


def test_kill_tree():
    print("test_kill_tree")
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                         **plat.new_group_kwargs())
    plat.kill_tree(p)
    try:
        p.wait(timeout=6)
        dead = True
    except subprocess.TimeoutExpired:
        dead = False
    check(dead, "kill_tree reaped the process")


def test_to_host_path():
    print("test_to_host_path")
    out = plat.to_host_path("/home/x/file")
    check(isinstance(out, str) and out, "to_host_path returns a string")
    if not plat.is_wsl():
        check(out == "/home/x/file", "non-WSL: to_host_path is the identity")


def main():
    for t in (test_detection, test_rmtree, test_open_excl, test_new_group_kwargs,
              test_chmod_private, test_kill_tree, test_to_host_path):
        t()
    if _fails:
        print(f"\n{len(_fails)} FAILED")
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
