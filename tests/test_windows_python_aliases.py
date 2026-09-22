"""Windows Python aliases must not escape the shell's owning process tree."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from harness import plat


def test_real_commands_and_non_windows_environments_unchanged(monkeypatch):
    env = {"PATH": "selected-path", "USER_VALUE": "retained"}
    monkeypatch.setattr(plat, "is_windows", lambda: False)
    assert plat.shell_environment(env) is env
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat.shutil, "which", lambda name, path: "C:/venv/" + name + ".exe")
    assert plat.shell_environment(env) is env


@pytest.mark.parametrize("names", [("python",), ("python3",), ("python", "python3")])
def test_only_brokered_names_are_shadowed(monkeypatch, tmp_path, names):
    real = tmp_path / "selected venv" / "python.exe"
    real.parent.mkdir()
    real.write_bytes(b"fixture")
    alias_root = "C:/Users/example/AppData/Local/Microsoft/WindowsApps/"
    monkeypatch.setattr(plat, "is_windows", lambda: True)
    monkeypatch.setattr(plat.sys, "executable", str(real))
    monkeypatch.setattr(plat.shutil, "which", lambda name, path:
                        alias_root + name + ".exe" if name in names else str(real))
    env = {"PATH": "unchanged", "USER_VALUE": "retained"}
    result = plat.shell_environment(env)
    assert env["PATH"] == "unchanged" and result["USER_VALUE"] == "retained"
    folder = Path(result["PATH"].split(os.pathsep)[0])
    for name in ("python", "python3"):
        assert (folder / name).exists() == (name in names)
        assert (folder / (name + ".cmd")).exists() == (name in names)
    assert plat.shell_environment(env)["PATH"] == result["PATH"]
    assert str(real).replace("\\", "/") in (folder / names[0]).read_text()


@pytest.mark.skipif(os.name != "nt", reason="Windows Job and actual execution alias")
@pytest.mark.parametrize("shell", ["bash", "cmd"])
def test_python3_alias_launch_is_in_the_parent_job_and_forwards_arguments(tmp_path, shell):
    aliases = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"
    if not (aliases / "python3.exe").exists() or not plat.has_posix_shell():
        pytest.skip("requires an installed WindowsApps Python3 alias and Git Bash")
    env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + str(aliases)
               + os.pathsep + os.environ.get("PATH", ""))
    env = plat.shell_environment(env)
    script = tmp_path / "probe.py"
    script.write_text('''import ctypes,json,sys
from ctypes import wintypes
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.OpenJobObjectW.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.LPCWSTR]
k.OpenJobObjectW.restype=wintypes.HANDLE
k.GetCurrentProcess.restype=wintypes.HANDLE
k.IsProcessInJob.argtypes=[wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)]
k.CloseHandle.argtypes=[wintypes.HANDLE]
h=k.OpenJobObjectW(4,False,sys.argv[1])
assert h
inside=wintypes.BOOL()
assert k.IsProcessInJob(k.GetCurrentProcess(),h,ctypes.byref(inside))
k.CloseHandle(h)
print(json.dumps({'inside':bool(inside.value),'args':sys.argv[2:],'executable':sys.executable}))
''', encoding="utf-8")
    import shlex
    name = "Local\\CollieAliasProbe-" + str(os.getpid()) + "-" + str(time.time_ns())
    command = "python3 " + " ".join(shlex.quote(x.replace("\\", "/") if i == 0 else x)
        for i, x in enumerate([str(script), name, "space and 中文", "literal$;&"]))
    argv, use_shell = plat.shell_argv(command)
    if shell == "cmd":
        launcher = Path(env["PATH"].split(os.pathsep)[0]) / "python3.cmd"
        argv = (subprocess.list2cmdline([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"])
                + ' ""' + str(launcher) + '" "' + str(script) + '" "' + name
                + '" "space and 中文" "literal$;&""')
    # Assign before release; the native interpreter must belong to this exact Job.
    bootstrap = "import json,subprocess,sys; assert sys.stdin.readline()=='GO\\n'; raise SystemExit(subprocess.call(json.loads(sys.argv[1])))"
    proc = subprocess.Popen([sys.executable, "-c", bootstrap, json.dumps(argv)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        cwd=tmp_path, env=env, text=True, encoding="utf-8", **plat.no_window_kwargs())
    owner = None
    try:
        owner = plat.attach_kill_on_close_job(proc, name=name)
        out, err = proc.communicate("GO\n", timeout=20)
        assert proc.returncode == 0, err
        data = json.loads(out)
        assert data["inside"] is True
        assert data["args"] == ["space and 中文", "literal$;&"]
        assert os.path.samefile(data["executable"], sys.executable)
    finally:
        if owner is not None:
            owner.close(timeout_s=5)
        elif proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@pytest.mark.skipif(not plat.has_posix_shell(), reason="regression launcher requires Bash")
def test_full_suite_launcher_resolves_real_interpreter_with_spaces(tmp_path):
    script = Path(__file__).with_name("run_all.sh").read_text(encoding="utf-8")
    prefix = script.split("rc=0", 1)[0]
    fixture = tmp_path / "launcher.sh"
    fixture.write_text(prefix + '\n"$PY" -c \'import sys; print(sys.executable)\'\n',
                       encoding="utf-8", newline="\n")
    env = dict(os.environ, COLLIE_TEST_PYTHON=sys.executable)
    result = subprocess.run([plat.posix_shell(), str(fixture)], env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=20, **plat.no_window_kwargs())
    assert result.returncode == 0, result.stderr
    assert os.path.samefile(result.stdout.strip(), sys.executable)
    env["COLLIE_TEST_PYTHON"] = str(tmp_path / "missing interpreter")
    failed = subprocess.run([plat.posix_shell(), str(fixture)], env=env, capture_output=True,
                            text=True, encoding="utf-8", timeout=20, **plat.no_window_kwargs())
    assert failed.returncode == 2
    assert "must name a working Python 3" in failed.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows alias cancellation")
def test_cancelled_python3_cannot_finish_a_delayed_write(tmp_path):
    from harness import tool_process
    aliases = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps"
    if not (aliases / "python3.exe").exists() or not plat.has_posix_shell():
        pytest.skip("requires WindowsApps alias and Git Bash")
    env = plat.shell_environment(dict(os.environ,
        PATH=str(Path(sys.executable).parent) + os.pathsep + str(aliases) + os.pathsep + os.environ.get("PATH", "")))
    script = tmp_path / "write_later.py"
    script.write_text("from pathlib import Path\nimport time\nPath('started').write_text('yes')\ntime.sleep(2)\nPath('late').write_text('wrong')\n",encoding="utf-8")
    args, shell = plat.shell_argv("python3 write_later.py")
    result = tool_process.run_owned(args, use_shell=shell, cwd=str(tmp_path), env=env,
        timeout_s=15, cancelled=lambda: (tmp_path / "started").exists())
    assert result.status == tool_process.CANCELED
    assert result.tree_terminated and not result.effect_uncertain
    time.sleep(2.3)
    assert not (tmp_path / "late").exists()
