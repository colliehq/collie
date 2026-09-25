"""The Windows update bootstrap, run for real by Windows PowerShell.

`Start-Process -Wait` waits for the installer AND every process it leaves behind. The installer
starts the supervisor and leaves it running, so from 0.21.23 to 0.30.0 the bootstrap never got
past the install. It logged no exit code and restarted nothing. It stayed asleep until the next
update's installer closed that supervisor, then relaunched pieces in the middle of that install.
"""
import os
import socket
import subprocess
import sys
import time
import uuid

import pytest

from harness import update as up

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell bootstrap")


def _kill_marked(marker):
    subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                    "Get-CimInstance Win32_Process | Where-Object { ([string]$_.CommandLine) -match '%s' "
                    "-and $_.Name -ne 'powershell.exe' } | ForEach-Object { Stop-Process -Id $_.ProcessId "
                    "-Force -ErrorAction SilentlyContinue }" % marker],
                   capture_output=True, timeout=60)


def _bridge_port_up():
    """Make 127.0.0.1:8677 accept, whether or not a real bridge already holds it."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 8677))
    except OSError:
        s.close()
        return None          # something (a real bridge) already listens there
    s.listen(4)
    return s


def test_bootstrap_does_not_wait_for_what_the_installer_leaves_running(tmp_path):
    marker = "collieboot%s" % uuid.uuid4().hex[:8]
    # A stand-in installer: leaves a long-lived child behind (like the supervisor) and exits 0.
    (tmp_path / "collie-update-test").mkdir()
    installer = tmp_path / "collie-update-test" / "setup.cmd"
    installer.write_text(
        '@echo off\r\nstart "" /b "%s" -c "import time; time.sleep(120)" %s\r\nexit /b 0\r\n'
        % (sys.executable, marker), encoding="ascii")
    root = tmp_path / "Collie"
    (root / "python").mkdir(parents=True)
    (root / "python" / "pythonw.exe").write_bytes(b"")
    log = tmp_path / "update.log"
    gone = subprocess.Popen([sys.executable, "-c", "pass"]); gone.wait()

    restarts = "\n".join([
        up._RESTART["bridge"],
        up._unless_up("probe", "Up-Lines 'no-such-process-%s'" % marker, 0,
                      '"[collie-update] PROBE STARTED"'),
    ])
    script = tmp_path / "bootstrap.ps1"
    script.write_text(up._BOOTSTRAP.format(pid=gone.pid, exe=str(installer), root=str(root),
                                           log=str(log), restarts=restarts), encoding="utf-8")
    listener = _bridge_port_up()
    try:
        started = time.time()
        r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-File", str(script)], capture_output=True, timeout=110)
        took = time.time() - started
    finally:
        if listener:
            listener.close()
        _kill_marked(marker)
    text = log.read_text(encoding="utf-8", errors="replace")
    assert took < 90, "the bootstrap waited on the installer's leftover child (%.0fs)\n%s" % (took, text)
    assert r.returncode == 0, text
    assert "installer exit code: 0" in text, text
    assert "browser bridge is already running" in text, text
    assert "restarting browser bridge" not in text, text
    assert "restarting probe" in text and "PROBE STARTED" in text, text
    assert "[collie-update] done" in text, text
    assert not installer.parent.exists(), "the downloaded installer is left in %TEMP%"


def test_restarts_run_supervisor_first_and_leave_dogs_to_it():
    parts = ["window", "bridge", "slack:slack-rowan.pyw", "wallpaper", "supervisor"]
    ordered = sorted(parts, key=lambda p: up._RESTART_ORDER.get(p.split(":", 1)[0], 9))
    assert ordered == ["supervisor", "wallpaper", "slack:slack-rowan.pyw", "bridge", "window"]
    dog = up._restart_script("slack:slack-rowan.pyw", r"C:\r")
    assert "Wait-Up" in dog and "--name rowan" in dog, dog
    assert dog.index("already running") < dog.index("Start-Process"), dog


def test_bootstrap_stops_only_old_copies_of_itself():
    s = up._BOOTSTRAP.format(pid=1, exe="C:\\x\\setup.exe", root="C:\\r", log="C:\\l.log",
                             restarts='"noop"')
    stop = next(l for l in s.splitlines() if "collie-update\\.ps1" in l)
    assert "$_.ProcessId -ne $PID" in stop and "AddMinutes(-15)" in stop, stop


def _installer_sleeper_stop():
    """The command line collie.iss's PrepareToInstall runs to stop sleeping update scripts."""
    import re
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    iss = open(os.path.join(root, "installer", "collie.iss"), encoding="utf-8-sig").read()
    prepare = iss.split("function PrepareToInstall", 1)[1].split("\nend;", 1)[0]
    blocks = prepare.split("Exec(ExpandConstant('{cmd}'),")[1:]
    block = next(b for b in blocks if "collie-update" in b.split(");", 1)[0])
    lits = re.findall(r"'((?:[^']|'')*)'", block.split(");", 1)[0])
    params = "".join(lits[:lits.index("")]).replace("''", "'")
    kill_at = prepare.index("taskkill.exe")
    return params, prepare.index("collie-update") < kill_at


def test_installer_stops_sleeping_update_scripts_before_it_ends_the_runtime():
    params, first = _installer_sleeper_stop()
    assert first, "the sleepers must be stopped before the taskkill that would wake them"
    assert params.startswith('/C powershell -NoProfile -Command "') and "AddMinutes(-15)" in params
    assert "-Force -ErrorAction SilentlyContinue" in params
    dry = params.replace("-Force -ErrorAction SilentlyContinue", "-WhatIf")
    marker = "collie-update.ps1 %s" % uuid.uuid4().hex[:8]
    fake = subprocess.Popen(["powershell.exe", "-NoProfile", "-Command", "Start-Sleep 60 # " + marker],
                            creationflags=0x08000000)
    try:
        time.sleep(1.5)
        target = "(%d)" % fake.pid
        # Run exactly as Inno's Exec does: cmd.exe with the parameters verbatim.
        young = subprocess.run("cmd.exe " + dry, capture_output=True, text=True, timeout=60)
        assert young.returncode == 0 and not young.stderr.strip(), young
        assert target not in young.stdout, "a script under 15 minutes old is this update's own"
        old = subprocess.run("cmd.exe " + dry.replace("AddMinutes(-15)", "AddMinutes(5)"),
                             capture_output=True, text=True, timeout=60)
        assert target in old.stdout, (old.stdout, old.stderr)
    finally:
        fake.kill()
