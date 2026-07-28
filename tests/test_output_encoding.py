"""A character must not be able to kill a command.

Windows consoles hand Python the active code page — cp1252 on the CI runners — and `print` raises
UnicodeEncodeError on anything it cannot encode. That is not a mangled line: it is an unhandled
exception that ends the command mid-sentence. `collie init` died on the single U+2713 in
"✓ codemap:", exited 1 with half a line written, and three checks failed for what looked like an
unrelated reason. Windows CI had been red for days over one tick mark.

Reproduced here on any platform by forcing the same encoding through PYTHONIOENCODING.

    python3 tests/test_output_encoding.py
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def run(args, cwd, encoding="cp1252"):
    env = dict(os.environ,
               PYTHONIOENCODING=encoding,
               COLLIE_EMBED="bm25", COLLIE_PROVIDER="mock", PYTHONPATH=ROOT)
    # Decode defensively: the child is being told to WRITE cp1252, so reading it back as strict
    # utf-8 makes this harness die on the very bytes it exists to check. A test that crashes instead
    # of failing tells you almost nothing.
    p = subprocess.run([sys.executable, "-m", "harness.cli"] + args, cwd=cwd, env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=180)
    return p.stdout, p.stderr, p.returncode


def main():
    repo = tempfile.mkdtemp(prefix="collie-enc-")
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=False)
    with open(os.path.join(repo, "a.py"), "w") as fh:
        fh.write("def f():\n    return 1\n")

    # The exact failure: cp1252 cannot encode U+2713, and the line that carries it is the one the
    # suite reads for "codemap:".
    out, err, rc = run(["init", "--no-config"], repo)
    check(rc == 0, "collie init exits 0 on a console that cannot encode its own output")
    check("UnicodeEncodeError" not in err, "and does not raise UnicodeEncodeError")
    check("codemap:" in out, "the codemap line still gets printed")

    # ASCII-only consoles are harsher still; the command must survive those too.
    out2, err2, rc2 = run(["init", "--no-config"], repo, encoding="ascii")
    check(rc2 == 0, "the same holds for a plain ASCII console")
    check("codemap:" in out2, "and the line survives there as well")

    # And nothing is lost where the terminal can take it.
    out3, _, rc3 = run(["init", "--no-config"], repo, encoding="utf-8")
    check(rc3 == 0 and "codemap:" in out3, "a UTF-8 console is unaffected")
    check("✓" in out3, "which still shows the real tick rather than a replacement")

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "output encoding: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
