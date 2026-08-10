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
