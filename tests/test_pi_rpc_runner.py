import os
from collections import deque

from harness import runner_env
from harness.pi_rpc_runner import PiRpcRunner


class FakeTransport:
    def __init__(self):
        self.messages = []
        self.queue = deque()
        self.returncode = 0
        self.stderr = ""

    def send(self, message):
        message = dict(message)
        self.messages.append(message)
        kind = message.get("type")
        if kind == "get_state":
            self.queue.append({"type": "response", "id": message["id"],
                               "command": "get_state", "success": True,
                               "data": {"sessionId": "pi_session_1"}})
        elif kind == "prompt":
            self.queue.extend([
                {"type": "response", "id": message["id"],
                 "command": "prompt", "success": True},
                {"type": "message_update", "text": "working"},
                {"type": "agent_settled"},
            ])
        elif kind == "get_session_stats":
            self.queue.append({"type": "response", "id": message["id"],
                               "command": kind, "success": True,
                               "data": {"tokens": {"input": 9, "output": 4,
                                                    "cacheRead": 2, "cacheWrite": 1},
                                        "cost": .02}})
        elif kind == "get_last_assistant_text":
            self.queue.append({"type": "response", "id": message["id"],
                               "command": kind, "success": True,
                               "data": {"text": "finished"}})

    def receive(self, timeout_s):
        return self.queue.popleft()

    def terminate(self, timeout_s=5):
        return True

    def close(self):
        return True


def _snap(_root):
    return {"tree_digest": "same", "snapshot_complete": True}


def test_pi_rpc_runs_with_no_shell_and_native_usage(tmp_path, monkeypatch):
    transport = FakeTransport()
    seen = {}

    def factory(argv, cwd, env):
        seen["argv"] = tuple(argv)
        return transport

    monkeypatch.setattr(runner_env, "assert_no_billing_override", lambda *_a, **_k: None)
    monkeypatch.setattr(runner_env, "child_env", lambda *_a, **_k:
                        ({"PATH": os.environ.get("PATH", "")},
                         {"allowed": ["PATH"], "stripped": []}))
    runner = PiRpcRunner(transport_factory=factory, snapshotter=_snap)
    result = runner.start("do the work", str(tmp_path))

    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == "read,edit,write,grep,find,ls"
    assert "bash" not in argv[argv.index("--tools") + 1].split(",")
    for flag in ("--no-extensions", "--no-skills", "--no-prompt-templates",
                 "--no-context-files", "--no-approve"):
        assert flag in argv
    assert result.settled and result.thread_id == "pi_session_1"
    assert result.final_output == "finished"
    assert result.usage == {"input_tokens": 9, "output_tokens": 4,
                            "cached_input_tokens": 2,
                            "cache_write_input_tokens": 1, "cost_usd": .02}


def test_pi_distinguishes_steer_from_follow_up():
    transport = FakeTransport()
    runner = PiRpcRunner(transport_factory=lambda *_a: transport)
    runner._active = transport
    runner._active_turn = True
    assert runner.steer_current("change course")
    assert runner.follow_up_current("afterward")
    assert [row["type"] for row in transport.messages[-2:]] == ["steer", "follow_up"]
