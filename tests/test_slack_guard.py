"""The Slack execution guard: a dead listener cannot leave live side effects behind."""
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
fails = []


def check(ok, label):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)


def main():
    tmp = tempfile.mkdtemp(prefix="collie_slack_guard_")
    started, finished = os.path.join(tmp, "started"), os.path.join(tmp, "finished")
    child = (
        "import pathlib,sys,time; "
        "pathlib.Path(sys.argv[1]).write_text('yes'); "
        "time.sleep(.75); pathlib.Path(sys.argv[2]).write_text('bad')")
    state = os.path.join(tmp, "guard-state.json")
    cmd = [sys.executable, "-m", "harness.slackguard", "--state", state, "--",
           sys.executable, "-c", child, started, finished]

    guard = subprocess.Popen(cmd, cwd=ROOT, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    guard.stdin.write("go\n")           # production shape: Windows writes CRLF here
    guard.stdin.flush()
    deadline = time.time() + 3
    while time.time() < deadline and not os.path.exists(started):
        time.sleep(.02)
    check(os.path.exists(started), "the exact durable release starts the guarded task")
    guard.stdin.close()                 # exactly what an exited listener's OS pipe does
    guard.stdin = None
    guard.communicate(timeout=10)
    time.sleep(1.0)                      # past the child's scheduled effect
    check(not os.path.exists(finished),
          "listener EOF terminates the execution tree before later effects can happen")

    never = os.path.join(tmp, "never")
    unreleased = subprocess.run(
        [sys.executable, "-m", "harness.slackguard", "--state",
         os.path.join(tmp, "never-state.json"), "--", sys.executable, "-c",
         "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('bad')", never],
        cwd=ROOT, input=b"", stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    check(unreleased.returncode != 0 and not os.path.exists(never),
          "a parent crash before durable attach cannot start task code")

    if os.name != "nt":
        abrupt = subprocess.Popen(
            [sys.executable, "-m", "harness.slackguard", "--state",
             os.path.join(tmp, "abrupt-state.json"), "--", sys.executable, "-c",
             "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"],
            cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)
        abrupt.stdin.write("go\n")
        abrupt.stdin.flush()
        abrupt.stdin = None
        abrupt.communicate(timeout=10)
        check(abrupt.returncode == 76,
              "a signal-killed executor becomes the portable outcome-unknown guard code")

    # Killing the intermediate guard itself must not bypass process-tree
    # ownership. This is the failure a plain supervisor pipe cannot cover.
    started2, effect2 = os.path.join(tmp, "started2"), os.path.join(tmp, "effect2")
    guard2 = subprocess.Popen(
        [sys.executable, "-m", "harness.slackguard", "--state",
         os.path.join(tmp, "guard2-state.json"), "--", sys.executable, "-c",
         child, started2, effect2], cwd=ROOT, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    life2 = guard2.stdin
    life2.write("go\n")
    life2.flush()
    deadline = time.time() + 3
    while time.time() < deadline and not os.path.exists(started2):
        time.sleep(.02)
    guard2.kill()
    guard2.wait(timeout=10)
    life2.close()
    time.sleep(1.0)                      # past the child's scheduled effect
    check(os.path.exists(started2) and not os.path.exists(effect2),
          "even a killed guard cannot leave its executor tree running")

    # Managed tools used to start a second POSIX session for their own timeout
    # boundary. That escaped the Slack executor's process group, so the ordinary
    # shell tool could outlive a cancelled mission. Under slackexec, plat keeps
    # managed descendants inside the outer ownership boundary instead.
    managed_started = os.path.join(tmp, "managed-started")
    managed_effect = os.path.join(tmp, "managed-effect")
    nested = (
        "import pathlib,subprocess,sys; from harness import plat; "
        "p=subprocess.Popen([sys.executable,'-c',"
        "'import pathlib,sys,time; time.sleep(.75); pathlib.Path(sys.argv[1]).write_text(\"bad\")',"
        "sys.argv[2]], **plat.new_group_kwargs()); "
        "pathlib.Path(sys.argv[1]).write_text(repr(plat.new_group_kwargs())); p.wait()")
    managed = subprocess.Popen(
        [sys.executable, "-m", "harness.slackguard", "--state",
         os.path.join(tmp, "managed-state.json"), "--", sys.executable, "-c",
         nested, managed_started, managed_effect], cwd=ROOT, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    managed.stdin.write("go\n")
    managed.stdin.flush()
    deadline = time.time() + 3
    while time.time() < deadline and not os.path.exists(managed_started):
        time.sleep(.02)
    managed.stdin.close()
    managed.stdin = None
    managed.communicate(timeout=10)
    time.sleep(1.0)
    check(os.path.exists(managed_started) and not os.path.exists(managed_effect),
          "a managed shell subtree cannot escape cancellation in a second POSIX session")

    # The lifetime signal is control plumbing, not task input. Keep the parent
    # pipe open and prove a task reading stdin still gets immediate EOF.
    stdin_seen = os.path.join(tmp, "stdin-seen")
    stdin_task = ("import os,pathlib,sys; "
                  "pathlib.Path(sys.argv[1]).write_bytes(os.read(0, 1))")
    stdin_guard = subprocess.Popen(
        [sys.executable, "-m", "harness.slackguard", "--state",
         os.path.join(tmp, "stdin-state.json"), "--", sys.executable, "-c",
         stdin_task, stdin_seen], cwd=ROOT, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdin_life = stdin_guard.stdin
    stdin_life.write("go\n")
    stdin_life.flush()
    stdin_guard.stdin = None
    stdin_guard.communicate(timeout=10)   # lifetime pipe remains open here
    stdin_life.close()
    check(stdin_guard.returncode == 0 and os.path.exists(stdin_seen)
          and open(stdin_seen, "rb").read() == b"",
          "guarded task stdin remains DEVNULL while the listener life pipe stays open")

    # A normal production-shaped run keeps the life pipe open while waiting and
    # must exit cleanly — no buffered-stdin daemon crash at interpreter shutdown.
    normal = subprocess.Popen(
        [sys.executable, "-m", "harness.slackguard", "--state",
         os.path.join(tmp, "normal-state.json"), "--", sys.executable,
         "-m", "harness.cli", "run", "hello", "--json", "--provider", "mock"],
        cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    life = normal.stdin
    life.write("go\n")
    life.flush()
    normal.stdin = None
    out, err = normal.communicate(timeout=30)
    life.close()
    check(normal.returncode == 0 and out.lstrip().startswith("{")
          and "Fatal Python error" not in err,
          "the exact Worker text-mode handshake completes a real CLI run with rc=0")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "slack guard: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
