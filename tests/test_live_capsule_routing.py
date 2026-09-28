"""A capsule command is routed by what the person said, not by the capsule's framing.

The capsule sends its exact words as ``authority_text`` and a longer model prompt as ``q``. The
prompt names purchases, secrets and security changes (to say they are NOT authorized), and the
router's hard-task keywords match those words, so routing on ``q`` sent every capsule command to
the slowest model and effort. This drives /api/stream through the real handler with the fake
harness from test_web_execution_manager.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_web_execution_manager import _stream, lab  # noqa: E402,F401


def _capsule_prompt(command):
    """Build q exactly as live_capsule.html's taskPrompt does."""
    page = (Path(__file__).parents[1] / "harness" / "webui" / "live_capsule.html").read_text(
        encoding="utf-8")
    body = page.split("function taskPrompt(command){", 1)[1].split("\n", 1)[0]
    head, tail = re.search(r'return "(.*?)"\+target\+"(.*?)"\+command\}', body).groups()
    target = json.dumps({"process": "code", "pid": 7, "hwnd": 9, "title": "notes.md"},
                        separators=(",", ":"))
    return json.loads('"%s"' % head) + target + json.loads('"%s"' % tail) + command


def test_capsule_command_is_routed_by_its_own_words(lab, monkeypatch):
    from harness import router

    command = "open my notes"
    prompt = _capsule_prompt(command)
    assert prompt.endswith("User command: open my notes") and "security changes" in prompt
    decisions = []
    real = router.resolve_run_decision

    def spy(text, *args, **kwargs):
        decision = real(text, *args, **kwargs)
        decisions.append((text, decision))
        return decision

    monkeypatch.setattr(router, "resolve_run_decision", spy)
    events = _stream(lab, q=prompt, authority_text=command, session="capsule-routing-1",
                     intent="build", quality="balanced", verification="auto",
                     workspace="current", strategy="single", explicit_axes="none",
                     route_kind="code", runner="collie")
    assert events[-1][0] == "done", events[-1]
    text, decision = decisions[-1]
    assert text == command
    assert decision.complexity != "hard"
    # The model still receives the whole framed prompt, and the grant is the exact words.
    assert lab.calls[-1]["message"] == prompt
    assert lab.calls[-1]["authority"] == command


def test_a_request_without_authority_text_is_still_routed_by_its_prompt(lab, monkeypatch):
    from harness import router

    seen = []
    real = router.resolve_run_decision
    monkeypatch.setattr(router, "resolve_run_decision",
                        lambda text, *a, **k: seen.append(text) or real(text, *a, **k))
    events = _stream(lab, q="fix the flaky race condition in the scheduler",
                     session="plain-routing-1")
    assert events[-1][0] == "done"
    assert seen[-1] == "fix the flaky race condition in the scheduler"
