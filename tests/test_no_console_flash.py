"""Nothing collie spawns may throw a console window at whoever is using the machine.

Reported from a desk, not from a log: "他不停的弹了好多好多框框，非常影响使用" — black boxes
flashing open across the screen while a dog worked. Every collie surface that matters runs under
pythonw (the Slack dog, the wallpaper, the desktop app), and a windowless parent on Windows gets
its children a BRAND NEW console each. A run doing twenty shell steps threw twenty windows.

The bug is not any one spawn — plat.no_window_kwargs() already existed and most callers used it.
The bug is that a spawn added later inherits nothing, and the failure is invisible to everyone
developing on macOS. So this walks the source rather than trusting a habit.

    python3 tests/test_no_console_flash.py
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []

# Exemption is per SPAWN, by what it runs — not per file. A file-level list would have excused
# every future spawn added to a file that happens to contain one macOS helper, which is the same
# blindness that let this class of bug spread in the first place.
POSIX_ONLY = (
    # These binaries do not exist on Windows, so the branch never executes there and passing
    # creationflags would raise ValueError on the platform it DOES run on.
    "osascript", "screencapture", "sips", "security", "ioreg", "tccutil", "socketfilterfw",
    "xdg-open", "/usr/", "pbcopy", "diskutil", "launchctl", '"ps"', "'ps'",
    "codesign", "hdiutil", "spctl", "ditto", "brew", "avfoundation", "wslpath",
    "zenity", "kdialog",
)
# Opening a file manager, a browser or an editor is a REQUEST for a window. Suppressing it is the
# bug, not the fix.
WANTS_A_WINDOW = ("explorer", '"open"', "'open'", "QuickTime", "_app_window_flags", "[cli, root]")
# Terminal-run drivers: benchmarks and test harnesses, started by a person who has a console.
EXEMPT = {"e2e.py", "compare.py", "swe.py", "swe_predict_one.py", "reval.py"}

SPAWN = re.compile(r"subprocess\.(Popen|run|call|check_output)\(")   # no \s*: "Popen (unlike…" is prose
# Ways a call can already be carrying the flag without naming it here.
CARRIES = ("no_window_kwargs", "creationflags", "_NOWIN", "**kw", "_quiet()",
           "**popen_kw", "**spawn_kw")


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def spawns(src: str):
    """(line_no, text) for each spawn, with its full call — args may span several lines."""
    out = []
    for m in SPAWN.finditer(src):
        # Prose about a spawn is not a spawn: several comments in this codebase quote the call they
        # are explaining, and counting those would make the scan cry wolf until someone muted it.
        bol = src.rfind("\n", 0, m.start()) + 1
        if "#" in src[bol:m.start()]:
            continue
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "(":
                depth += 1
            elif src[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        out.append((src.count("\n", 0, m.start()) + 1, src[m.start():i + 1]))
    return out


def main():
    hdir = os.path.join(ROOT, "harness")
    offenders = []
    scanned = 0
    for fn in sorted(os.listdir(hdir)):
        if not fn.endswith(".py") or fn in EXEMPT:
            continue
        src = open(os.path.join(hdir, fn), encoding="utf-8").read()
        for line, call in spawns(src):
            scanned += 1
            if any(c in call for c in CARRIES):
                continue
            if any(p in call for p in POSIX_ONLY) or any(w in call for w in WANTS_A_WINDOW):
                continue
            offenders.append("%s:%d  %s" % (fn, line, " ".join(call.split())[:90]))

    check(scanned > 0, "the scan actually found spawns to check (%d)" % scanned)
    for o in offenders:
        print("      %s" % o)
    check(not offenders,
          "every spawn outside the exempt list is windowless (%d offender(s))" % len(offenders))

    # And the helper itself has to stay platform-safe, since that is why it exists rather than the
    # flag being written inline: passing creationflags off Windows raises.
    from harness import plat
    kw = plat.no_window_kwargs()
    if plat.is_windows():
        check(kw == {"creationflags": 0x08000000}, "on Windows it carries CREATE_NO_WINDOW")
    else:
        check(kw == {}, "off Windows it carries nothing — the flag would raise ValueError")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "no console flash: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
