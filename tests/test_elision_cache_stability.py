"""The history the model already saw stays byte-stable for several turns at a time.

Stubbing an old tool output rewrites a message the provider has cached, and everything after it is
read again. The elision boundary used to move with every new message, so once a run passed the
recent window each turn re-read the whole window: 1651 of 4467 turns in the developer's run log
missed the cache for that reason, 11.3M tokens, about 6.9k a turn.
"""
import json
import os

from harness.context import ELIDE_STEP


def _history(turns):
    from harness.providers import ToolCall
    msgs = [{"role": "user", "content": "fix the bug"}]
    for i in range(turns):
        msgs.append({"role": "assistant", "tool_calls": [ToolCall("tc%d" % i, "read_file",
                                                                  {"path": "/x%d" % i})]})
        msgs.append({"role": "tool", "tool_call_id": "tc%d" % i, "name": "read_file",
                     "content": ("line %d of a file\n" % i) * 60})
    return msgs


def _builds(turns):
    from harness.cli import make_harness
    h = make_harness(os.getcwd(), provider="mock", project="elide-cache", embed="hash")
    out = []
    for k in range(1, turns + 1):
        _system, pmsgs, _meta = h.composer.build({"messages": _history(k)}, "next", os.getcwd(),
                                                 "elide-cache")
        out.append([json.dumps(m, sort_keys=True, default=str) for m in pmsgs])
    return out


def _prefix_breaks(builds):
    """Turns whose request rewrote part of what the previous request had already sent."""
    return sum(1 for prev, cur in zip(builds, builds[1:]) if cur[:len(prev)] != prev)


def test_the_sent_history_is_rewritten_once_per_step_not_every_turn():
    builds = _builds(40)
    breaks = _prefix_breaks(builds)
    # 40 turns add 80 messages; past the 14-message window the boundary moves ELIDE_STEP at a time
    assert breaks <= (80 - 14) // ELIDE_STEP + 1, breaks
    assert breaks >= 5, "elision still happens: %d" % breaks


def test_the_recent_window_stays_full_and_older_outputs_are_still_stubbed():
    builds = _builds(40)
    last = [json.loads(m) for m in builds[-1]]
    tools = [(i, m) for i, m in enumerate(last) if m.get("role") == "tool"]
    for i, m in tools:
        if i >= len(last) - 14:
            assert "elided" not in m["content"], i
        if i < len(last) - 14 - (ELIDE_STEP - 1):
            assert "elided" in m["content"], i
