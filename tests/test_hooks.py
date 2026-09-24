import json
import os
import sys

from harness.hooks import HookManager, HookTrustStore, validate_config


def _script(tmp_path, body):
    path = tmp_path / "hook.py"
    path.write_text(body, encoding="utf-8")
    # shell_argv is POSIX-on-Windows when Git Bash is present; forward slashes
    # keep the command valid in both that shell and native cmd.exe.
    return '"%s" "%s"' % (sys.executable.replace("\\", "/"),
                            str(path).replace("\\", "/"))


def _config(event, command, matcher=None, timeout=5):
    group = {"hooks": [{"type": "command", "command": command, "timeout": timeout}]}
    if matcher is not None:
        group["matcher"] = matcher
    return {"_source": "test", "hooks": {event: [group]}}


def test_pre_tool_hook_can_deny_with_auditable_reason(tmp_path):
    command = _script(tmp_path,
        "import json,sys\n"
        "p=json.load(sys.stdin)\n"
        "assert p['tool_name']=='bash'\n"
        "print(json.dumps({'decision':'deny','reason':'policy says no'}))\n")
    hooks = HookManager(str(tmp_path), [_config("PreToolUse", command, "bash")])
    result = hooks.dispatch("PreToolUse", {"tool_name": "bash"}, subject="bash")
    assert not result.allowed
    assert result.reason == "policy says no"
    assert result.receipts[0]["exit_code"] == 0


def test_non_ascii_payload_reaches_the_hook_intact_in_any_code_page(tmp_path):
    # The payload travels through a text pipe in the system code page; as raw UTF-8 text it came
    # out as "?" where that code page cannot spell it. Escaped, it is ASCII on the wire.
    command = _script(tmp_path,
        "import json,sys\n"
        "raw=sys.stdin.buffer.read()\n"
        "p=json.loads(raw.decode('ascii'))\n"
        "print(json.dumps({'decision':'deny','reason':p['tool_input']['command']}))\n")
    hooks = HookManager(str(tmp_path), [_config("PreToolUse", command, "bash")])
    result = hooks.dispatch("PreToolUse", {"tool_name": "bash",
                                           "tool_input": {"command": "echo 提交说明 café"}},
                            subject="bash")
    assert result.receipts[0]["exit_code"] == 0, result.receipts
    assert result.reason == "echo 提交说明 café"


def test_a_timed_out_hook_does_not_wait_for_what_it_left_running(tmp_path):
    # subprocess.run killed only the shell; on Windows its drain then waited for as long as a
    # background process the hook started kept the output pipe open.
    import time
    from harness import plat
    if plat.is_windows() and not plat.posix_shell():
        import pytest
        pytest.skip("needs a POSIX shell to background a process")
    hooks = HookManager(str(tmp_path),
                        [_config("PreToolUse", "sleep 20 & sleep 20", "bash", timeout=1)])
    t0 = time.monotonic()
    result = hooks.dispatch("PreToolUse", {"tool_name": "bash"}, subject="bash")
    elapsed = time.monotonic() - t0
    assert result.receipts[0]["timed_out"] is True
    assert not result.allowed
    assert elapsed < 10, "waited %.1fs for a background process of a timed-out hook" % elapsed


def test_matcher_and_additional_context(tmp_path):
    command = _script(tmp_path,
        "import json,sys\njson.load(sys.stdin)\n"
        "print(json.dumps({'decision':'allow','additionalContext':'run formatter'}))\n")
    hooks = HookManager(str(tmp_path), [_config("PostToolUse", command, "edit_*|write_file")])
    miss = hooks.dispatch("PostToolUse", {}, subject="bash")
    assert miss.receipts == []
    hit = hooks.dispatch("PostToolUse", {}, subject="write_file")
    assert hit.allowed and hit.additional_context == ["run formatter"]


def test_authority_hooks_fail_closed_but_observer_hooks_fail_open(tmp_path):
    command = _script(tmp_path, "import sys\nsys.stderr.write('boom')\nsys.exit(7)\n")
    pre = HookManager(str(tmp_path), [_config("PreToolUse", command)])
    post = HookManager(str(tmp_path), [_config("PostToolUse", command)])
    assert not pre.dispatch("PreToolUse", {}, subject="bash").allowed
    assert post.dispatch("PostToolUse", {}, subject="bash").allowed


def test_timeout_is_fail_closed_at_stop(tmp_path):
    command = _script(tmp_path, "import time\ntime.sleep(2)\n")
    hooks = HookManager(str(tmp_path), [_config("Stop", command, timeout=.1)])
    result = hooks.dispatch("Stop", {}, subject="project")
    assert not result.allowed
    assert result.receipts[0]["timed_out"] is True


def test_validate_config_reports_structural_errors(tmp_path):
    path = tmp_path / "hooks.json"
    path.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{}]}]}}), encoding="utf-8")
    errors = validate_config(str(path))
    assert errors and "command is required" in errors[0]


def test_file_hooks_require_exact_hash_review(monkeypatch, tmp_path):
    state = tmp_path / "state"; state.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(state))
    hook_file = state / "hooks.json"
    hook_file.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "python -c \"print('{}')\""}
    ]}]}}), encoding="utf-8")
    pending = HookManager(str(tmp_path))
    assert not pending.active and pending.pending[0]["path"] == str(hook_file)
    HookTrustStore().set(str(hook_file), True)
    active = HookManager(str(tmp_path))
    assert active.active and not active.pending
    hook_file.write_text(hook_file.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    changed = HookManager(str(tmp_path))
    assert not changed.active and changed.pending, "changed hook bytes must require re-review"


def _count(path):
    return path.read_text(encoding="utf-8").count("alive") if path.exists() else 0


def test_a_timed_out_hook_ends_what_outlived_its_shell(tmp_path):
    # When the shell has already exited, taskkill /T cannot find what it left behind; the Job
    # (Windows) or the process group (POSIX) still owns it.
    import time
    from harness import plat
    if plat.is_windows() and not plat.posix_shell():
        import pytest
        pytest.skip("needs a POSIX shell to background a process")
    marker = tmp_path / "marker.txt"
    command = "(sleep 3; echo alive >> '%s') & exit 0" % str(marker).replace("\\", "/")
    hooks = HookManager(str(tmp_path), [_config("PreToolUse", command, "bash", timeout=1)])
    result = hooks.dispatch("PreToolUse", {"tool_name": "bash"}, subject="bash")
    assert result.receipts[0]["timed_out"] is True
    time.sleep(4.5)
    assert _count(marker) == 0, "a process the timed-out hook left behind kept running"


def test_an_interrupted_hook_is_not_left_running(tmp_path, monkeypatch):
    # subprocess.run killed its child on any exception; a bare Popen did not, so Ctrl-C during a
    # hook left the hook running (and, in its own session, out of the terminal's reach).
    import subprocess
    import pytest
    from harness import hooks as hooks_mod
    started = []

    class Interrupted(subprocess.Popen):
        def communicate(self, *a, **kw):
            if not started:
                started.append(self)
                raise KeyboardInterrupt
            return super().communicate(*a, **kw)

    monkeypatch.setattr(hooks_mod.subprocess, "Popen", Interrupted)
    command = _script(tmp_path, "import time\ntime.sleep(30)\n")
    hooks = HookManager(str(tmp_path), [_config("PreToolUse", command, "bash", timeout=60)])
    with pytest.raises(KeyboardInterrupt):
        hooks.dispatch("PreToolUse", {"tool_name": "bash"}, subject="bash")
    assert started and started[0].poll() is not None, "the hook is still running"


def test_a_payload_that_cannot_be_sent_starts_nothing(tmp_path):
    marker = tmp_path / "ran.txt"
    command = _script(tmp_path, "open(%r, 'w').write('ran')\n" % str(marker))
    hooks = HookManager(str(tmp_path), [_config("PostToolUse", command, "bash")])
    result = hooks.dispatch("PostToolUse", {"tool_name": "bash", "bad": object()},
                            subject="bash")
    assert "hook failed" in result.receipts[0]["reason"]
    import time
    time.sleep(2)                     # long enough for a hook that did start to have written
    assert not marker.exists(), "the hook ran although its payload could not be sent"


def test_hook_output_is_read_in_the_encoding_it_was_written_in(tmp_path, monkeypatch):
    # jq, node and Git Bash's tools print UTF-8; read in the code page a Chinese reason reached
    # the model as mojibake ("涓嶅厑璁" for "不允许" under 936).
    import locale
    from harness import tool_process
    monkeypatch.setattr(locale, "getencoding", lambda: "cp936")      # the pipes' default
    monkeypatch.setattr(tool_process, "_ansi", lambda: "gbk")
    monkeypatch.setattr(tool_process, "_output_encoding", lambda: tool_process.OUTPUT_CODEC)
    command = _script(tmp_path,
        "import sys\n"
        "sys.stdout.buffer.write('{\"decision\":\"deny\",\"reason\":\"不允许: 生产分支\"}'"
        ".encode('utf-8'))\n")
    hooks = HookManager(str(tmp_path), [_config("PreToolUse", command, "bash")])
    result = hooks.dispatch("PreToolUse", {"tool_name": "bash"}, subject="bash")
    assert result.reason == "不允许: 生产分支"
