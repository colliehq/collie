"""Loop-level regression for the same-turn repro-then-edit freshness bug.

Run: python tests/test_gate_freshness.py   (exit 0 = all green)

The audit found: tool calls in one turn share the turn index as the gate's
freshness key, so edit1 -> passing+assert repro -> breaking edit2 (all in ONE
completion) left the repro looking fresh for edit2 and stamped the run VERIFIED
even though the last edit was never reproduced. The fix invalidates repro
evidence on every landed edit. This drives the real Harness loop through exactly
that sequence and asserts the run is NOT verified.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.cli import make_harness  # noqa: E402
from harness.providers import Completion, ToolCall  # noqa: E402


class _ScriptProvider:
    reports_cache = False

    def __init__(self, script, name="deepseek", model="deepseek-chat"):
        self.name, self.model, self.max_tokens = name, model, 4096
        self._script = list(script); self._i = 0; self.calls = 0

    def complete(self, system, messages, tool_schemas, on_text=None):
        self.calls += 1
        item = self._script[min(self._i, len(self._script) - 1)]; self._i += 1
        return item(messages) if callable(item) else item


def main():
    d = tempfile.mkdtemp(prefix="collie-gatefresh-")
    fp = os.path.join(d, "f.py")
    with open(fp, "w") as f:
        f.write("x = 1\ny = 1\n")

    h = make_harness(d, provider="mock", project="gatefresh", embed="hash")
    h.max_turns = 6
    h.self_verify = True
    h.verify_gate = True
    h.require_assert = True
    # ONE turn: land a fix, run a passing assert-repro, then land a BREAKING edit.
    # edit1 arms did_edit so the repro is recorded; edit2 must invalidate it.
    h.provider = _ScriptProvider([
        Completion(tool_calls=[
            ToolCall("e1", "edit_file", {"path": fp, "old_string": "x = 1", "new_string": "x = 2"}),
            ToolCall("r1", "bash", {"command": 'python3 -c "assert True"'}),
            ToolCall("e2", "edit_file", {"path": fp, "old_string": "y = 1", "new_string": "y = 2"}),
        ], stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ])
    res = h.run("gatefresh", "fix it")

    ok = (res.verified is False)
    if not ok:
        print("  FAIL: a finish after an unreproduced edit must NOT be verified "
              "(res.verified=%r)" % res.verified)
        print("\n== GATE-FRESHNESS: 1 FAILED ==")
        sys.exit(1)
    print("test_same_turn_repro_then_edit_not_verified OK")
    print("\n== GATE-FRESHNESS: passed ==")


if __name__ == "__main__":
    main()
