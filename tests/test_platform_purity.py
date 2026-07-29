"""One codebase, three platforms — this is the gate that keeps it true.

collie ships from a single repo: one `git tag` builds Collie-Setup.exe on windows-latest,
Collie-arm64.dmg on macos-14 and the wheel on ubuntu. That property does not survive on its own.
It broke silently and stayed broken for two releases, because Windows-only calls were written
inline in shared modules and wrapped in `except Exception`, so on macOS they returned False
instead of raising:

    launch()          os.startfile          -> could not open any app, ever
    open_project()    %LOCALAPPDATA%\\Code   -> could not open any project
    _seed_apps()      C:\\ paths             -> launcher was empty
    icon_png()        PowerShell + .NET     -> every icon fell back to a letter
    _ensure_ytdlp()   downloads yt-dlp.exe  -> an 18MB PE32+ binary on a Mac
    (14 call sites)   creationflags=        -> ValueError on every non-Windows call

Six features, one failure mode, zero error messages. None of them were found by reading the code;
they were found by running it on a Mac for the first time.

So: platform-specific APIs belong in harness/plat.py, behind a helper. Anywhere else they must sit
inside an explicit platform branch. This test fails on the code as it was before those fixes, which
is the only evidence that it is worth having.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(ROOT, "harness")

# API -> why it cannot appear unguarded in shared code
FORBIDDEN = {
    r"\bos\.startfile\s*\(": "os.startfile does not exist off Windows (AttributeError)",
    r"creationflags\s*=": "creationflags= raises ValueError off Windows",
    r"subprocess\.CREATE_[A-Z_]+": "subprocess.CREATE_* is Windows-only",
}
# A line is fine if the platform is being tested on it or just above it.
GUARD = re.compile(r"is_windows\(\)|os\.name\s*==\s*[\"']nt[\"']|sys\.platform\s*==\s*[\"']win|"
                   r"platform\.system\(\)\s*==\s*[\"']Windows[\"']|no_window_kwargs|"
                   r"new_group_kwargs|hasattr\(os,\s*[\"']startfile[\"']\)|is_wsl\(\)|"
                   # a Windows path list is fine when the code first proves it is on Windows…
                   r"which\([\"']powershell|"
                   # …or supplies the counterpart for the other platform right beside it
                   r"\bmac\s*=\s*[\[(]|is_macos\(\)")


def _code_only(lines):
    """Blank out docstrings and comments. A docstring that *describes* a Windows path is not a
    Windows path — flagging prose would train everyone to ignore this test."""
    out, in_doc, delim = [], False, ""
    for line in lines:
        stripped = line.strip()
        if in_doc:
            out.append("")
            if delim in line:
                in_doc = False
            continue
        m = re.match(r'^[^#]*?("""|\'\'\')', line)
        if m and line.count(m.group(1)) == 1:
            in_doc, delim = True, m.group(1)
            out.append(line[:m.start(1)])
            continue
        out.append("" if stripped.startswith("#") else line)
    return out


def _declared_platform(lines):
    """A module may declare itself platform-specific — wallpaper.py is the WebView2 engine and has
    no meaning off Windows. Declaring it is fine; leaving it implicit is what hid six bugs."""
    for line in lines[:40]:
        m = re.match(r'^PLATFORM\s*=\s*["\'](\w+)["\']', line)
        if m:
            return m.group(1)
    return ""

failures = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        failures.append(what)


def scan(path, raw_lines):
    """Report (lineno, api, reason) for unguarded uses. A guard counts if it is on the same line,
    on any of the 6 lines above (an `if` block), or assigned to the name being used."""
    lines = _code_only(raw_lines)
    hits = []
    for i, line in enumerate(lines):
        for pat, why in FORBIDDEN.items():
            if not re.search(pat, line):
                continue
            window = "".join(lines[max(0, i - 6):i + 1])
            if GUARD.search(window):
                continue
            hits.append((i + 1, why))
    return hits


def modules():
    for name in sorted(os.listdir(HARNESS)):
        if name.endswith(".py") and name != "plat.py":
            yield os.path.join(HARNESS, name)


def test_no_unguarded_platform_apis():
    bad = []
    for path in modules():
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if _declared_platform(lines):
            continue
        for lineno, why in scan(path, lines):
            bad.append("%s:%d  %s" % (os.path.relpath(path, ROOT), lineno, why))
    check(not bad, "no unguarded Windows-only API outside plat.py" +
          ("" if not bad else ":\n    " + "\n    ".join(bad)))


def test_no_hardcoded_windows_paths_outside_a_windows_branch():
    """A literal C:\\ or %LOCALAPPDATA% in a shared code path means that feature is Windows-only and
    silently does nothing everywhere else — which is exactly how the app launcher shipped empty."""
    pat = re.compile(r"(?<!\w)(?:[Cc]:\\\\|%LOCALAPPDATA%|%PROGRAMFILES)")
    bad = []
    for path in modules():
        with open(path, encoding="utf-8") as f:
            raw = f.readlines()
        if _declared_platform(raw):
            continue
        lines = _code_only(raw)
        for i, line in enumerate(lines):
            if not pat.search(line):
                continue
            window = "".join(lines[max(0, i - 8):i + 1])
            fn = re.findall(r"def\s+(\w+)", window)
            # a function whose own name says "win" is allowed to be Windows-shaped
            if GUARD.search(window) or (fn and "win" in fn[-1].lower()):
                continue
            bad.append("%s:%d" % (os.path.relpath(path, ROOT), i + 1))
    check(not bad, "no Windows-only literal paths in shared code" +
          ("" if not bad else ": " + ", ".join(bad)))


def test_platform_helpers_answer_on_this_machine():
    """The helpers must return something usable HERE — a helper that only makes sense on the build
    machine is the same bug one level up."""
    sys.path.insert(0, ROOT)
    from harness import plat
    kw = plat.no_window_kwargs()
    check(isinstance(kw, dict), "plat.no_window_kwargs() returns a dict (%r)" % kw)
    check(("creationflags" in kw) == plat.is_windows(),
          "no_window_kwargs carries creationflags only on Windows")
    check(isinstance(plat.new_group_kwargs(), dict), "plat.new_group_kwargs() returns a dict")
    check(plat.is_macos() == (sys.platform == "darwin"), "plat.is_macos() agrees with sys.platform")


def test_the_desktop_backend_works_on_this_platform():
    """desktop.py was the worst offender: five of its features were Windows-only. Assert the
    platform-facing ones answer here rather than returning a silent False."""
    sys.path.insert(0, ROOT)
    from harness import desktop as dt
    check(dt.launch("") is False, "launch('') is False without raising")
    check(dt.launch("/nonexistent/nope.app") is False, "launch(missing) is False")
    apps = dt.apps()
    check(isinstance(apps, list), "apps() returns a list")
    if sys.platform == "darwin":
        check(len(apps) > 0, "apps() finds installed applications on macOS (%d)" % len(apps))
        check(all(a["path"].endswith(".app") for a in apps), "every entry is an .app bundle")
        seeds = dt._seed_apps()
        check(isinstance(seeds, list) and len(seeds) > 0,
              "the launcher seeds real apps on macOS (%d)" % len(seeds))
    check(dt._YTDLP_ASSET != "yt-dlp.exe" or sys.platform == "win32",
          "yt-dlp asset matches this platform (%s)" % dt._YTDLP_ASSET)


def check_tests_never_open_a_browser():
    """A test that starts `collie web` without --no-open opens a real browser tab on the machine
    running it, every single time. The server dies with the test, so what is left behind is a row of
    tabs pointing at a dead port — and nothing in the output says where they came from."""
    import glob
    bad = []
    for path in glob.glob(os.path.join(os.path.dirname(os.path.abspath(__file__)), "*.py")):
        src = open(path, encoding="utf-8").read()
        for i, line in enumerate(src.splitlines(), 1):
            if '"web"' not in line or "--no-open" in line:
                continue
            # the flag is often on the same call but a line or two down
            window = "\n".join(src.splitlines()[i - 1:i + 2])
            if "--no-open" not in window:
                bad.append("%s:%d" % (os.path.basename(path), i))
    check(not bad, "no test starts `collie web` without --no-open%s"
          % ("" if not bad else " (" + ", ".join(bad) + ")"))


if __name__ == "__main__":
    test_no_unguarded_platform_apis()
    test_no_hardcoded_windows_paths_outside_a_windows_branch()
    test_platform_helpers_answer_on_this_machine()
    test_the_desktop_backend_works_on_this_platform()
    check_tests_never_open_a_browser()
    print("\n" + ("all green" if not failures else "%d FAILED" % len(failures)))
    sys.exit(1 if failures else 0)
