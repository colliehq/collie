"""What the user made must not live inside the program.

DATA was `<wherever harness is installed>/data` — sessions, memory.db, runs.db, the sandbox. Fine
for a checkout, wrong everywhere else:

  * From the .app it resolved INSIDE the signed bundle, which is read-only. Nothing could be saved,
    so the phone showed "no chats yet" forever and every run was forgotten the moment it ended.
  * Had it been writable, each update replaces the bundle — and takes the history with it.
  * From pip it landed in site-packages, which the next upgrade deletes.

A checkout with no override keeps its own data/ (the suite and a dev box depend on it, and an
existing store must not be orphaned). An explicit state directory is always an isolation boundary;
installed copies otherwise keep data beside the user's other Collie state.

    python3 tests/test_data_dir.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def ask(cwd, env):
    """Report DATA from a fresh interpreter, so import order cannot leak the checkout in."""
    p = subprocess.run(
        [sys.executable, "-c", "from harness.cli import DATA; print(DATA)"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    return (p.stdout or "").strip(), (p.stderr or "").strip()


def main():
    # A checkout keeps its own data/.
    out, err = ask(ROOT, dict(os.environ, PYTHONPATH=ROOT))
    check(out == os.path.join(ROOT, "data"),
          "a source checkout still uses its own data/ (got %s)" % (out or err[-120:]))

    isolated = tempfile.mkdtemp(prefix="collie-source-state-")
    isolated_env = dict(os.environ, PYTHONPATH=ROOT, COLLIE_STATE_DIR=isolated)
    isolated_env.pop("COLLIE_DATA_DIR", None)
    out, err = ask(ROOT, isolated_env)
    check(out == os.path.join(isolated, "data"),
          "explicit state isolates a source checkout too (got %s)" % (out or err[-120:]))
    shutil.rmtree(isolated, ignore_errors=True)

    # An install must not: copy the package somewhere with no pyproject.toml beside it, which is
    # exactly the shape of site-packages and of the .app.
    stage = tempfile.mkdtemp(prefix="collie-datadir-")
    site = os.path.join(stage, "site-packages")
    os.makedirs(site)
    shutil.copytree(os.path.join(ROOT, "harness"), os.path.join(site, "harness"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    state = os.path.join(stage, "state")

    env = dict(os.environ, PYTHONPATH=site, COLLIE_STATE_DIR=state)
    env.pop("COLLIE_DATA_DIR", None)
    out, err = ask(stage, env)
    check(out == os.path.join(state, "data"),
          "an installed copy writes beside the user's state, not into itself (got %s)"
          % (out or err[-120:]))
    check(site not in out, "and never anywhere under the installation directory")

    # The escape hatch still wins over both.
    forced = os.path.join(stage, "elsewhere")
    out, err = ask(stage, dict(env, COLLIE_DATA_DIR=forced))
    check(out == forced, "COLLIE_DATA_DIR overrides either rule")

    shutil.rmtree(stage, ignore_errors=True)
    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "data dir: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
