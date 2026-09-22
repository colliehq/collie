import json
import os

import pytest

from harness import runner_env
from harness.agent_runners import ProcessOutcome
from harness.codex_sdk_runner import CodexSdkRunner
from harness.runner_specs import RunInput


class FakeProcess:
    def __init__(self, stdout):
        self.stdout = stdout
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        kwargs["on_process"](object())
        if kwargs.get("on_stdout"):
            kwargs["on_stdout"](self.stdout)
        return ProcessOutcome(stdout=self.stdout, exit_code=0)


def _snap(_root):
    return {"tree_digest": "same", "snapshot_complete": True}


def test_sdk_sidecar_keeps_prompt_off_argv_and_accepts_multimodal(tmp_path, monkeypatch):
    stdout = "\n".join([
        json.dumps({"type": "session.started", "thread_id": "thr_123"}),
        json.dumps({"type": "turn.started", "thread_id": "thr_123"}),
        json.dumps({"type": "usage.updated", "usage": {"total": {
            "input_tokens": 12, "cached_input_tokens": 2, "output_tokens": 3}}}),
        json.dumps({"type": "turn.completed", "thread_id": "thr_123",
                    "status": "completed"}),
        json.dumps({"type": "collie.sdk.result", "action": "run",
                    "thread_id": "thr_123", "status": "completed",
                    "final_output": "done", "usage": {"total": {
                        "input_tokens": 12, "cached_input_tokens": 2,
                        "output_tokens": 3}}}),
    ]) + "\n"
    process = FakeProcess(stdout)
    monkeypatch.setattr(runner_env, "assert_no_billing_override", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_env, "child_env", lambda *_a, **_k:
                        ({"PATH": os.environ.get("PATH", "")},
                         {"allowed": ["PATH"], "stripped": []}))
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")
    runner = CodexSdkRunner(process_runner=process, snapshotter=_snap)

    result = runner.start(RunInput("private prompt", image_files=(str(image),)),
                          str(tmp_path))

    argv, call = process.calls[0]
    assert "private prompt" not in " ".join(argv)
    request = json.loads(call["stdin_text"])
    assert request["input"]["text"] == "private prompt"
    assert request["input"]["image_files"] == [str(image)]
    assert result.settled and result.thread_id == "thr_123"
    assert result.final_output == "done"
    assert result.usage["input_tokens"] == 12
    assert result.terminal_state == "completed"


def test_sdk_probe_is_explicit_when_optional_dependency_is_missing(monkeypatch):
    monkeypatch.setattr("harness.codex_sdk_runner.importlib.util.find_spec",
                        lambda _name: None)
    row = CodexSdkRunner().probe(now=10)
    assert not row.installed and not row.usable()
    assert "collie-harness[codex]" in row.detail


def test_sdk_refuses_images_outside_workspace(tmp_path, monkeypatch):
    outside = tmp_path.parent / "outside-image.png"
    outside.write_bytes(b"png")
    runner = CodexSdkRunner(process_runner=FakeProcess(""), snapshotter=_snap)
    monkeypatch.setattr(runner, "handshake", lambda: type("Handshake", (), {
        "require": lambda self, **kwargs: None})())

    with pytest.raises(ValueError, match="inside the workspace"):
        runner.start(RunInput("inspect", image_files=(str(outside),)),
                     str(tmp_path))
