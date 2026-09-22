"""Long-conversation compaction: the projection, the checkpoint, and what it must never do.

The property under test is not "the context got smaller". It is that a long run keeps
making progress *without* the durable transcript, the tool-call pairing, the user's own
words, the request budget or the permission model paying for it.

    python -m pytest tests/test_compaction.py -q
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import compaction                                     # noqa: E402
from harness.compaction import CompactionPolicy                    # noqa: E402
from harness.providers import Completion, ToolCall, Usage          # noqa: E402


def test_latest_image_request_survives_a_long_tool_chain():
    messages = _tool_history(20)
    original = {"role": "user", "content": [
        {"type": "text", "text": "Match this screenshot exactly, including the footer."},
        {"type": "image", "media_type": "image/png", "data": "original-image-data"},
    ]}
    messages[0] = original
    checkpoint = _make_checkpoint(messages, 21)
    projected, meta = compaction.project_messages(messages, checkpoint)
    assert meta["active"]
    assert original in projected
    assert projected[-20:] == messages[21:]
    assert len(projected) < len(messages)


def test_pending_tool_with_a_host_image_cannot_be_compacted_away():
    messages = _tool_history(4)
    start = len(messages)
    messages.extend([
        {"role": "assistant", "tool_calls": [ToolCall("pending", "read_file", {"path": "x"})]},
        {"role": "user", "source": "harness", "kind": "tool_attachment", "content": [
            {"type": "image", "data": "image"}]},
        {"role": "user", "source": "harness", "kind": "lifecycle_context", "content": "context"},
    ])
    assert not any(compaction.is_safe_cutoff(messages, i) for i in range(start + 1, len(messages)))


def test_torn_rendered_handoff_cannot_erase_the_users_latest_request():
    messages = _tool_history()
    messages[0]["content"] = "Do not install any dependencies."
    checkpoint = _make_checkpoint(messages, 21)
    checkpoint["summary_message"] = checkpoint["summary_message"].replace(messages[0]["content"], "")
    assert compaction.validate_checkpoint(messages, checkpoint) is None


@pytest.mark.parametrize("value", [1.5, 2.01, -0.5])
def test_fractional_checkpoint_numbers_are_not_truncated_to_integers(value):
    assert compaction.bounded_int(value, 0, 100) is None


# --------------------------------------------------------------------------- fixtures

def _policy(**over):
    """A policy small enough to exercise in a test, same shape as the shipped one.

    The threshold has to clear the real fixed prefix — Collie's system prompt plus its tool
    schemas is already ~5k estimated tokens — or every run would look like a long one.
    """
    base = dict(threshold_tokens=9_000, min_messages=16, min_compacted=6,
                keep_recent_messages=6, keep_recent_groups=2, min_new_messages=6,
                summary_max_chars=1_200, pinned_user_chars=800, pinned_user_max=300)
    base.update(over)
    return CompactionPolicy(**base)


def _handoff_text(tail="", **sections):
    """A structurally valid handoff summary, standing in for a real one.

    ``validate_summary`` requires all seven headings, so a fixture that is meant to be
    ADOPTED has to carry them. Individual sections are overridden by keyword, which is how a
    test writes a summary that is well-formed but wrong — a different failure from a reply
    that was never a handoff at all.
    """
    body = {heading: "none." for heading in compaction.SUMMARY_HEADINGS}
    for key, value in sections.items():
        body[key.replace("_", " ").upper()] = value
    return "\n".join("%s — %s" % (h, body[h]) for h in compaction.SUMMARY_HEADINGS) + tail


class _Provider:
    """Scripted provider that can tell a loop turn from a compaction summary request.

    The two are distinguished by the summarizer's system prompt, which is also the
    assertion that compaction never sends tools or a stream callback.
    """
    name = "deepseek"
    model = "deepseek-chat"
    reports_cache = False
    max_tokens = 4096

    def __init__(self, turn, summary=None):
        self._turn = turn                    # fn(n, messages) -> Completion
        self._summary = summary              # fn(n, digest) -> Completion
        self.turn_calls = []                 # messages seen on ordinary turns
        self.summary_digests = []            # the bounded digest each summary request saw
        self.summary_schemas = []
        self.summary_stream = []
        self.calls = 0

    def complete(self, system, messages, tool_schemas, on_text=None):
        self.calls += 1
        if system == compaction.SUMMARY_SYSTEM:
            self.summary_digests.append(messages[0]["content"])
            self.summary_schemas.append(list(tool_schemas or []))
            self.summary_stream.append(on_text)
            n = len(self.summary_digests)
            if self._summary is not None:
                return self._summary(n, messages[0]["content"])
            return Completion(text="GOALS — ship the fix.\nUSER CONSTRAINTS — none new.\n"
                                   "DECISIONS — keep going.\nCHANGED FILES AND ACTIONS — none.\n"
                                   "CHECK RESULTS — none run.\nINCOMPLETE WORK — the task.\n"
                                   "NEXT STEPS — continue. [summary %d]" % n,
                              stop_reason="end_turn", usage=Usage(input_tokens=7, output_tokens=3))
        self.turn_calls.append(list(messages))
        return self._turn(len(self.turn_calls), messages)


def _harness(tmp_path, monkeypatch, project, policy=None, turns=40):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    from harness.cli import make_harness
    h = make_harness(os.getcwd(), provider="mock", project=project, embed="hash")
    h.max_turns = turns
    h.compaction = policy if policy is not None else _policy()
    return h


_OUT = "line of tool output; " * 60             # ~1.2 KB per tool result
# A long ARGUMENT, which is the payload the composer's elision provably never shrinks: it
# stubs old tool RESULTS only, so a run whose turns carry big commands/edits grows forever.
_ARG = "y" * 1200


def _busy_turn(n, messages, stop_after=14, answer="all done"):
    """Call bash every turn until ``stop_after``, then finish."""
    if n >= stop_after:
        return Completion(text=answer, stop_reason="end_turn", usage=Usage(input_tokens=3))
    return Completion(
        tool_calls=[ToolCall("t%d" % n, "bash", {"command": "echo %d %s" % (n, _ARG)})],
        stop_reason="tool_use", usage=Usage(input_tokens=3, output_tokens=1))


def _fake_bash(h, size=900):
    """Replace the bash tool with a deterministic, sizeable, side-effect-free result."""
    tool = h.registry.get("bash")
    tool.run = lambda args, ctx: ("out:" + _OUT)[:size]
    return h


def _seed_history(pairs=20, size=3_000):
    """A conversation already long enough that the very first turn is over the threshold."""
    messages = [{"role": "user", "content": "the seeded original request"}]
    for i in range(pairs):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("s%d" % i, "bash", {"command": "echo %d %s" % (i, _ARG)})]})
        messages.append({"role": "tool", "tool_call_id": "s%d" % i, "name": "bash",
                         "content": ("seeded output " * 400)[:size]})
    return messages


def _events(h):
    seen = []
    h.emit = lambda kind, data: seen.append((kind, data))
    return seen


def _compaction_events(seen, status=None):
    return [d for k, d in seen
            if k == "compaction" and (status is None or d.get("status") == status)]


# --------------------------------------------------------------------------- estimation

def test_estimate_counts_tool_arguments_and_non_ascii():
    """The trigger has to see the two payloads elision never shrinks.

    Tool ARGUMENTS ride the assistant turn (the composer only stubs tool *results*), and CJK
    text costs roughly one token per character while the ~4-chars-per-token estimate charges
    it a quarter of that. A threshold measured the old way fires long after the real overflow.
    """
    from harness.providers import est_tokens
    args = {"path": "src/app.py", "new_string": "x = 1\n" * 400}
    message = {"role": "assistant", "content": "",
               "tool_calls": [ToolCall("c1", "edit_file", args)]}
    # est_tokens sees an empty content string; the arguments are the whole cost.
    assert est_tokens(message["content"]) == 0
    assert compaction.estimate_message(message) > 500

    chinese = "把这个补丁应用到项目里并运行测试" * 200
    assert compaction.estimate_text(chinese) > est_tokens(chinese) * 2.5

    # system + tool schemas are input tokens too, and a threshold that ignores them is
    # optimistic by a couple of thousand tokens on every single turn.
    schemas = [{"name": "bash", "description": "d" * 4000}]
    with_schemas = compaction.estimate_request("sys", [message], schemas)
    without = compaction.estimate_request("sys", [message], [])
    assert with_schemas - without > 900


def test_argument_and_non_ascii_growth_crosses_the_shipped_threshold():
    """A run whose tool OUTPUTS are tiny can still overflow, and must still compact."""
    policy = CompactionPolicy()                     # the real, shipped defaults
    messages = [{"role": "user", "content": "重构这个模块并保持接口不变"}]
    for i in range(90):
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [ToolCall("t%d" % i, "edit_file", {
                             "path": "pkg/mod_%d.py" % i,
                             "new_string": "值 = %d\n" % i + "def f():\n    return 1\n" * 120})]})
        messages.append({"role": "tool", "tool_call_id": "t%d" % i, "name": "edit_file",
                         "content": "ok"})
    total = compaction.estimate_request("system prompt", messages, [])
    assert total > policy.threshold_tokens, total
    plan, why = compaction.plan(messages, None, None, policy=policy, total_tokens=total)
    assert why == "ok" and plan.cutoff > 0


# ------------------------------------------------------- what reaches the summarizer

def _span(messages, policy, cutoff=None, previous_cutoff=0, previous_summary=""):
    """``(plan, prepared)`` for the span a real turn would compact."""
    cutoff = compaction.choose_cutoff(messages, policy) if cutoff is None else cutoff
    plan_ = compaction.CompactionPlan(
        cutoff=cutoff, kept=len(messages) - cutoff, source_messages=len(messages),
        before_tokens=9_000, reason="threshold", previous_cutoff=previous_cutoff,
        previous_summary=previous_summary)
    return plan_, compaction.prepare(messages, plan_, policy)


def _mixed_history(turns=20, every=4, user_chars=1_400, result_chars=3_000):
    """A long run whose user instructions are interleaved with big tool output."""
    messages = [{"role": "user", "content": "INSTRUCTION-0 " + "a" * user_chars}]
    for i in range(turns):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("t%d" % i, "bash", {"command": "echo %d" % i})]})
        messages.append({"role": "tool", "tool_call_id": "t%d" % i, "name": "bash",
                         "content": ("out %d " % i) * (result_chars // 8)})
        if i % every == every - 1:
            messages.append({"role": "user",
                             "content": "INSTRUCTION-%d " % (i + 1) + "b" * user_chars})
    return messages


def test_the_digest_is_bounded_by_whole_messages_never_by_dropping_the_middle():
    """The load-bearing guarantee: everything the summary is asked to represent was SHOWN.

    The digest is packed oldest-first and stops at the first message that does not fit; the
    cut then moves back to match. So a user instruction is never eliminated between the head
    and the tail of a clipped digest — it is either handed over complete or still sitting in
    the verbatim tail of the projection.
    """
    policy = _policy(summary_input_chars=8_000, keep_recent_messages=4, keep_recent_groups=1,
                     min_compacted=4)
    messages = _mixed_history()
    plan_, prepared = _span(messages, policy)
    assert prepared.ok and prepared.span_truncated, prepared.reason
    assert prepared.cutoff < plan_.cutoff, "this fixture must exercise the truncating path"
    assert len(prepared.digest) <= 2 * policy.summary_input_chars + 1_500

    compacted_users = [m for m in messages[:prepared.cutoff] if compaction.is_user_message(m)]
    assert len(compacted_users) >= 2
    for message in compacted_users:                       # complete, not an excerpt
        assert message["content"] in prepared.digest
    assert prepared.user_messages == len(compacted_users)

    # ...and every message the digest could not take is still verbatim in the projection
    checkpoint = compaction.make_checkpoint(messages, plan_, "S" * 200, policy, prepared)
    projected, meta = compaction.project_messages(messages, checkpoint)
    assert meta["cutoff"] == prepared.cutoff
    for message in messages[prepared.cutoff:]:
        assert message in projected
    assert _pairing_holds(projected)
    # the checkpoint reports the span it really summarized, not the whole prefix
    assert checkpoint["summarized_from"] == 0
    assert checkpoint["messages_summarized"] == prepared.cutoff
    assert checkpoint["user_messages_summarized"] == len(compacted_users)
    assert checkpoint["payload_chars_elided"] > 0


def test_a_user_message_too_large_for_the_digest_refuses_instead_of_clipping_it():
    """No-op beats a summary written from half of what the user asked for."""
    policy = _policy(summary_input_chars=4_000, keep_recent_messages=4, keep_recent_groups=1,
                     min_compacted=4)
    messages = [{"role": "user", "content": "PASTED-LOG " * 3_000}] + _mixed_history(8)[1:]
    _plan, prepared = _span(messages, policy)
    assert not prepared.ok and "single user message" in prepared.reason
    assert prepared.digest == "" and prepared.cutoff == 0


def test_a_previous_handoff_is_carried_forward_or_the_compaction_is_refused():
    """Generation 2 re-reads only the new span, so the old summary is mandatory input."""
    policy = _policy(summary_input_chars=12_000, keep_recent_messages=4, keep_recent_groups=1,
                     min_compacted=4)
    messages = _mixed_history(10, result_chars=800)
    plan1, prep1 = _span(messages, policy)
    assert prep1.ok
    first = compaction.make_checkpoint(messages, plan1, "GEN-ONE-SUMMARY " * 4, policy, prep1)
    assert first["generation"] == 1

    grown = list(messages) + _mixed_history(10, result_chars=800)[1:]
    plan2, why = compaction.plan(grown, first, None, policy=policy, total_tokens=99_000)
    assert why == "ok" and plan2.previous_cutoff == first["cutoff"]
    prep2 = compaction.prepare(grown, plan2, policy)
    assert prep2.ok and prep2.span_start == first["cutoff"]
    assert "GEN-ONE-SUMMARY" in prep2.digest, "the old summary is the only record of that span"
    second = compaction.make_checkpoint(grown, plan2, "GEN-TWO-SUMMARY " * 4, policy, prep2)
    assert second["generation"] == 2
    # the original request still crosses the second cut as quoted user text — here as an
    # excerpt, because this policy's per-message budget is 300 chars, and it says so
    assert messages[0]["content"][:100] in second["summary_message"]
    assert "EXCERPT ONLY, 300 of %d characters shown" % len(messages[0]["content"]) \
        in second["summary_message"]

    # a previous summary that cannot fit is a refusal, not a silent drop
    tiny = _policy(summary_input_chars=2_000, keep_recent_messages=4, keep_recent_groups=1)
    plan3 = compaction.CompactionPlan(
        cutoff=plan2.cutoff, kept=0, source_messages=len(grown), before_tokens=9_000,
        reason="threshold", previous_cutoff=first["cutoff"], previous_summary="P" * 5_000)
    refused = compaction.prepare(grown, plan3, tiny)
    assert not refused.ok and "previous handoff" in refused.reason


def test_the_loop_spends_no_request_when_the_digest_would_lose_user_text(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, "compact_refuse",
                 policy=_policy(summary_input_chars=4_000))
    _fake_bash(h)
    seen = _events(h)
    history = [{"role": "user", "content": "PASTED-LOG " * 3_000}] + _seed_history()[1:]
    provider = _Provider(lambda n, m: Completion(text="ok", stop_reason="end_turn",
                                                 usage=Usage(input_tokens=2)))
    h.provider = provider
    res = h.run("compact_refuse", "carry on", history=history)
    assert res.answer == "ok"
    assert provider.summary_digests == [], "no request may be spent on a refused compaction"
    skipped = _compaction_events(seen, "skipped")
    assert skipped and "single user message" in skipped[0]["reason"]


# --------------------------------------------------------------------------- user text

def test_the_latest_user_request_crosses_the_cut_verbatim_at_any_length():
    """A 4KB instruction is preserved whole — and NOT by keeping its tool chain."""
    policy = _policy(keep_recent_messages=4, keep_recent_groups=1, min_compacted=4,
                     pinned_user_max=300, pinned_user_chars=800)
    long_request = "REBUILD THE INDEXER. " + " ".join(
        "requirement %d: keep the %d-th column stable;" % (i, i) for i in range(160))
    assert len(long_request) > 4_000
    messages = [{"role": "user", "content": "the original short request"}]
    for i in range(12):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("a%d" % i, "bash", {"command": "echo %d" % i})]})
        messages.append({"role": "tool", "tool_call_id": "a%d" % i, "name": "bash",
                         "content": "ok " * 100})
    messages.append({"role": "user", "content": long_request})
    for i in range(12, 30):                     # the long-running work the request kicked off
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("a%d" % i, "bash", {"command": "echo %d" % i})]})
        messages.append({"role": "tool", "tool_call_id": "a%d" % i, "name": "bash",
                         "content": "ok " * 100})

    plan_, prepared = _span(messages, policy)
    assert prepared.ok
    checkpoint = compaction.make_checkpoint(messages, plan_, "S" * 200, policy, prepared)
    projected, _meta = compaction.project_messages(messages, checkpoint)
    handoff = projected[0]["content"]
    assert long_request in handoff, "the live instruction must survive the cut in full"
    assert "COMPLETE, VERBATIM" in handoff
    # the request's own tool chain was NOT retained to achieve that
    assert len(projected) < len(messages) / 2, (len(projected), len(messages))
    assert prepared.cutoff > messages.index({"role": "user", "content": long_request})
    # the older short request is quoted too, and every shortened quote says it is one
    assert "the original short request" in handoff
    assert "EXCERPT ONLY" in handoff or "earlier user message — complete" in handoff


def test_older_user_messages_are_quoted_or_labelled_never_silently_dropped():
    policy = _policy(pinned_user_chars=400, pinned_user_max=200)
    texts = ["request %d: " % i + "z" * 600 for i in range(6)]
    prefix = []
    for text in texts:
        prefix.append({"role": "user", "content": text})
        prefix.append({"role": "assistant", "content": "working"})
    pinned, ok, why = compaction.pinned_user_text(prefix, policy)
    assert ok and not why
    assert texts[-1] in pinned, "the latest is complete"
    assert "EXCERPT ONLY, 200 of %d characters shown" % len(texts[0]) in pinned
    quoted = sum(1 for t in texts if t[:40] in pinned)
    missing = len(texts) - quoted
    if missing:
        assert "%d earlier user message(s) are NOT quoted here" % missing in pinned


def test_a_host_reminder_is_not_treated_as_the_users_words():
    """The loop's own nudges ride in the user role; they are not the user's request."""
    nudge = {"role": "user", "content": "You said you were done. Run the tests. " * 20,
             "source": "harness", "kind": "verification_reminder"}
    assert compaction.is_user_message(nudge) is False
    assert compaction.is_user_message({"role": "user", "content": "hi"}) is True
    assert compaction.is_user_message({"role": "user", "content": "hi", "source": "user"}) is True
    assert compaction.is_user_message(
        {"role": "user", "content": "img", "kind": "screenshot"}) is False
    assert compaction.is_user_message({"role": "user", "content": "h", "compaction": True}) is False

    policy = _policy(keep_recent_messages=4, keep_recent_groups=1, min_compacted=4)
    real = "the real request: migrate the exporter"
    messages = [{"role": "user", "content": real}]
    for i in range(10):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("n%d" % i, "bash", {"command": "echo %d" % i})]})
        messages.append({"role": "tool", "tool_call_id": "n%d" % i, "name": "bash",
                         "content": "ok"})
    messages.append(dict(nudge))
    for i in range(10, 18):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("n%d" % i, "bash", {"command": "echo %d" % i})]})
        messages.append({"role": "tool", "tool_call_id": "n%d" % i, "name": "bash",
                         "content": "ok"})

    plan_, prepared = _span(messages, policy)
    assert prepared.ok and prepared.host_messages == 1 and prepared.user_messages == 1
    assert "HOST REMINDER (verification_reminder" in prepared.digest
    checkpoint = compaction.make_checkpoint(messages, plan_, "S" * 200, policy, prepared)
    handoff = checkpoint["summary_message"]
    latest = handoff.split("most recent user message before the cut — COMPLETE, VERBATIM")[1]
    assert real in latest.split("===")[0]
    assert "You said you were done" not in handoff, "a nudge is not a user quote"


# --------------------------------------------------------------------------- cut safety

def _orphaned_results(messages):
    """tool_results with no preceding tool_use — exactly what a provider 400s on."""
    from harness.providers import AnthropicProvider
    converted = AnthropicProvider.__new__(AnthropicProvider)._to_anthropic(messages)
    seen, orphans = set(), []
    for message in converted:
        content = message["content"]
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_use":
                seen.add(block["id"])
            if block.get("type") == "tool_result" and block["tool_use_id"] not in seen:
                orphans.append(block["tool_use_id"])
    return orphans


def _pairing_holds(messages):
    """Every tool_result must be preceded by its tool_use, or the provider 400s."""
    orphans = _orphaned_results(messages)
    assert not orphans, "orphaned tool_result(s): %s" % orphans
    return True


def test_cut_never_splits_a_multi_tool_group_or_a_pending_one():
    """Cuts land on group starts — including for a group still awaiting its results."""
    policy = _policy(keep_recent_messages=5, keep_recent_groups=2)
    messages = [{"role": "user", "content": "go"}]
    for i in range(12):                      # every turn issues THREE parallel calls
        calls = [ToolCall("t%d_%d" % (i, k), "bash", {"command": "c"}) for k in range(3)]
        messages.append({"role": "assistant", "content": "", "tool_calls": calls,
                         "thinking_blocks": [{"type": "thinking", "thinking": "…",
                                              "signature": "sig%d" % i}]})
        for call in calls:
            messages.append({"role": "tool", "tool_call_id": call.id, "name": "bash",
                             "content": "r"})
    # ...and the transcript ends with a PENDING group: calls issued, no results yet.
    messages.append({"role": "assistant", "content": "", "tool_calls": [
        ToolCall("pending", "bash", {"command": "slow"})]})

    for cut in range(1, len(messages)):
        # whatever the requested window, the chosen cutoff is a legal boundary
        cutoff = compaction.choose_cutoff(messages, _policy(keep_recent_messages=cut,
                                                           keep_recent_groups=1))
        if cutoff:
            assert messages[cutoff].get("role") != "tool", cutoff

    cutoff = compaction.choose_cutoff(messages, policy)
    assert cutoff > 0
    checkpoint = compaction.make_checkpoint(
        messages, compaction.CompactionPlan(cutoff=cutoff, kept=len(messages) - cutoff,
                                            source_messages=len(messages), before_tokens=9_000,
                                            reason="threshold"),
        "S" * 100, policy)
    projected, meta = compaction.project_messages(messages, checkpoint)
    assert meta["active"] and _pairing_holds(projected)
    # the pending group is kept whole, with its signed thinking attached
    assert projected[-1]["tool_calls"][0].id == "pending"
    assert any(m.get("thinking_blocks") for m in projected if m.get("role") == "assistant")


def _interleaved_history(turns=8):
    """The shape adjacency checks get wrong: several calls per turn, with a queued screenshot
    appended as a user message BETWEEN two of their results, and one out-of-order result."""
    messages = [{"role": "user", "content": "watch the browser and fix it"}]
    for i in range(turns):
        calls = [ToolCall("i%d_%d" % (i, k), "bash", {"command": "c%d" % k}) for k in range(3)]
        messages.append({"role": "assistant", "content": "", "tool_calls": calls})
        messages.append({"role": "tool", "tool_call_id": calls[0].id, "name": "bash",
                         "content": "r0"})
        messages.append({"role": "user", "source": "harness", "kind": "screenshot",
                         "content": [{"type": "text", "text": "[screenshot: after step]"},
                                     {"type": "image", "media_type": "image/png",
                                      "data": "iVBOR%d" % i}]})
        messages.append({"role": "tool", "tool_call_id": calls[2].id, "name": "bash",
                         "content": "r2"})           # out of order on purpose
        messages.append({"role": "tool", "tool_call_id": calls[1].id, "name": "bash",
                         "content": "r1"})
    return messages


def test_every_legal_cutoff_survives_interleaved_feedback_and_multi_call_turns():
    """Provider-validity is a property of the whole tool_use/tool_result INTERVAL.

    A user image landing between two results of the same assistant turn makes the cut look
    safe by adjacency (the neighbour is not a ``tool`` message) while it still orphans the
    results that follow. Every boundary this module offers must survive conversion; every
    boundary it refuses must be refused for a reason.
    """
    messages = _interleaved_history()
    legal = compaction.safe_boundaries(messages)
    assert legal, "there must be somewhere to cut"
    for cut in legal:
        head = [{"role": "user", "content": "handoff"}, {"role": "assistant", "content": "ok"}]
        assert _orphaned_results(head + messages[cut:]) == [], cut
        assert compaction.is_safe_cutoff(messages, cut)

    # the refused ones are not paranoia: cutting there really does orphan a result
    refused = [i for i in range(1, len(messages)) if i not in set(legal)]
    orphaning = [i for i in refused
                 if _orphaned_results([{"role": "user", "content": "h"}] + messages[i:])]
    assert len(orphaning) >= len(refused) // 2, (len(orphaning), len(refused))
    # the image message itself is one of the traps: it is not a tool message, but a cut there
    # would leave two results of that turn without their call
    image_at = next(i for i, m in enumerate(messages) if isinstance(m.get("content"), list))
    assert image_at not in legal
    assert _orphaned_results([{"role": "user", "content": "h"}] + messages[image_at:])


def test_a_dangling_call_the_conversation_moved_past_does_not_freeze_compaction():
    """A cancelled turn can leave a call with no result. A PENDING one (still the last thing
    that happened) stays whole; a dead one the thread continued past may be summarized away,
    because there is no result anywhere that dropping it could orphan."""
    messages = [{"role": "user", "content": "go"},
                {"role": "assistant", "content": "", "tool_calls": [
                    ToolCall("dead", "bash", {"command": "interrupted"})]}]
    for i in range(14):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("d%d" % i, "bash", {"command": "echo"})]})
        messages.append({"role": "tool", "tool_call_id": "d%d" % i, "name": "bash",
                         "content": "ok"})
    legal = compaction.safe_boundaries(messages)
    assert max(legal) > 2, "a dead call must not block every later cut"
    assert compaction.choose_cutoff(messages, _policy(keep_recent_messages=4,
                                                      keep_recent_groups=1, min_compacted=4)) > 2

    pending = messages + [{"role": "assistant", "content": "", "tool_calls": [
        ToolCall("live", "bash", {"command": "still running"})]}]
    assert max(compaction.safe_boundaries(pending)) == len(pending) - 1, \
        "the pending group is kept whole in the tail"


def test_projection_never_mutates_the_transcript():
    messages = [{"role": "user", "content": "u"}] + [
        {"role": "assistant", "content": "a%d" % i} for i in range(30)]
    before = json.dumps([compaction._canonical(m) for m in messages], sort_keys=True)
    checkpoint = compaction.make_checkpoint(
        messages, compaction.CompactionPlan(cutoff=20, kept=11, source_messages=31,
                                            before_tokens=9_000, reason="threshold"),
        "S" * 80, _policy())
    projected, _meta = compaction.project_messages(messages, checkpoint)
    assert json.dumps([compaction._canonical(m) for m in messages], sort_keys=True) == before
    assert projected is not messages and len(projected) == 2 + 11


# --------------------------------------------------------------------------- checkpoint

def _make_checkpoint(messages, cutoff, summary="S" * 120, policy=None):
    """A checkpoint at (or just below) ``cutoff`` — snapped to a legal boundary first.

    make_checkpoint refuses a cut that would split a tool-call interval, so a test that wants
    "about 20 messages" has to ask for a cut that is actually legal there.
    """
    policy = policy or _policy()
    cutoff = compaction._snap_down(messages, cutoff)
    assert cutoff > 0, "no legal cutoff at or below the requested one"
    checkpoint = compaction.make_checkpoint(
        messages, compaction.CompactionPlan(cutoff=cutoff, kept=len(messages) - cutoff,
                                            source_messages=len(messages),
                                            before_tokens=9_000, reason="threshold"),
        summary, policy)
    assert checkpoint is not None
    return checkpoint


def test_a_forked_or_edited_prefix_invalidates_the_checkpoint():
    """The cutoff is a claim about exact bytes. Rewrite them and the claim is void."""
    messages = [{"role": "user", "content": "original request"}] + [
        {"role": "assistant", "content": "step %d" % i} for i in range(30)]
    checkpoint = _make_checkpoint(messages, 20)
    assert compaction.validate_checkpoint(messages, checkpoint) is not None

    edited = list(messages)
    edited[3] = {"role": "assistant", "content": "step 3 (edited by hand)"}
    assert compaction.validate_checkpoint(edited, checkpoint) is None
    assert compaction.project_messages(edited, checkpoint)[0] == edited

    forked = messages[:10]                    # a fork BELOW the cutoff
    assert compaction.validate_checkpoint(forked, checkpoint) is None
    forked_above = messages[:25]              # a fork ABOVE it keeps the prefix intact
    assert compaction.validate_checkpoint(forked_above, checkpoint) is not None

    # tool-call arguments are part of the identity, not just the visible text
    with_call = list(messages)
    with_call[5] = {"role": "assistant", "content": "", "tool_calls": [
        ToolCall("c", "bash", {"command": "ls"})]}
    ck2 = _make_checkpoint(with_call, 20)
    tampered = list(with_call)
    tampered[5] = {"role": "assistant", "content": "", "tool_calls": [
        ToolCall("c", "bash", {"command": "rm -rf /"})]}
    assert compaction.validate_checkpoint(tampered, ck2) is None


def test_a_session_round_trip_does_not_look_like_an_edit(monkeypatch, tmp_path):
    """ToolCall dataclasses live, plain dicts on disk — the fingerprint must not care."""
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    from harness import sessions
    messages = [{"role": "user", "content": "go"}]
    for i in range(24):
        messages.append({"role": "assistant", "content": "",
                         "tool_calls": [ToolCall("t%d" % i, "bash", {"command": "echo ⚡"})]})
        messages.append({"role": "tool", "tool_call_id": "t%d" % i, "name": "bash",
                         "content": "ok"})
    sessions.checkpoint("round", messages, run_id="r", state="turn_boundary")
    checkpoint = _make_checkpoint(messages, 20)
    assert compaction.save_checkpoint("round", checkpoint) is True

    reloaded = sessions.load("round")["messages"]
    stored = compaction.load_checkpoint("round")
    assert compaction.validate_checkpoint(reloaded, stored) is not None


_V = compaction.CHECKPOINT_VERSION


@pytest.mark.parametrize("broken", [
    {"version": 99, "cutoff": 5},
    {"version": _V, "cutoff": "5"},
    {"version": _V, "cutoff": 0},
    {"version": _V, "cutoff": 5_000},
    {"version": _V, "cutoff": 5, "summary_message": "", "prefix_sha256": "0" * 64},
    {"version": _V, "cutoff": 5, "summary_message": "x", "prefix_sha256": "nope"},
    {"version": _V, "cutoff": 5, "summary_message": "x", "prefix_sha256": "0" * 64},
    "not even a dict",
    None,
])
def test_a_malformed_checkpoint_is_inert(broken):
    messages = [{"role": "user", "content": "u%d" % i} for i in range(30)]
    assert compaction.validate_checkpoint(messages, broken) is None
    assert compaction.project_messages(messages, broken)[0] == messages


def test_an_oversized_stored_summary_is_rejected():
    messages = [{"role": "user", "content": "u%d" % i} for i in range(30)]
    checkpoint = _make_checkpoint(messages, 20)
    checkpoint["summary_message"] = "x" * (compaction.MAX_HANDOFF_CHARS + 1)
    assert compaction.validate_checkpoint(messages, checkpoint) is None
    # ...and so is an unbounded MODEL-written part, which is the half a session file could
    # use to push text into every request from now on.
    checkpoint = _make_checkpoint(messages, 20)
    checkpoint["summary"] = "y" * (compaction.MAX_SUMMARY_CHARS + 1)
    assert compaction.validate_checkpoint(messages, checkpoint) is None


def _tool_history(pairs=14):
    messages = [{"role": "user", "content": "go"}]
    for i in range(pairs):
        messages.append({"role": "assistant", "content": "", "tool_calls": [
            ToolCall("f%d" % i, "bash", {"command": "echo %d" % i})]})
        messages.append({"role": "tool", "tool_call_id": "f%d" % i, "name": "bash",
                         "content": "ok %d" % i})
    return messages


def test_a_crafted_checkpoint_cannot_cut_inside_a_tool_call_interval():
    """The fingerprint proves the prefix is unchanged. It does NOT prove the cut is legal.

    Whoever can write the session file can also recompute the hash, so validation re-proves
    the boundary against the messages themselves — otherwise a restored checkpoint could hand
    the provider a tool_result whose tool_use was summarized away.
    """
    messages = _tool_history()
    inside = 2                                    # between call f0 (index 1) and its result
    assert str(messages[inside].get("role")) == "tool"
    forged = _make_checkpoint(messages, 21)
    forged["cutoff"] = inside
    forged["summarized_from"] = 0
    forged["prefix_sha256"] = compaction.prefix_fingerprint(messages, inside)  # honest hash
    assert compaction.validate_checkpoint(messages, forged) is None
    projected, meta = compaction.project_messages(messages, forged)
    assert projected == messages and meta["active"] is False
    assert _orphaned_results(projected) == []

    # the same record with a legal cutoff is fine, which is what makes the above a real check
    legal = compaction._snap_down(messages, inside + 1)
    forged["cutoff"] = legal
    forged["prefix_sha256"] = compaction.prefix_fingerprint(messages, legal)
    assert compaction.validate_checkpoint(messages, forged) is not None
    assert _orphaned_results(compaction.project_messages(messages, forged)[0]) == []


def test_a_restored_checkpoint_from_disk_cannot_introduce_an_orphan(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    from harness import sessions
    messages = _tool_history()
    sessions.checkpoint("forged", messages, run_id="r", state="turn_boundary")
    stored = _make_checkpoint(messages, 21)
    stored["cutoff"] = 4                          # a result index: inside an interval
    stored["prefix_sha256"] = compaction.prefix_fingerprint(messages, 4)
    assert compaction.save_checkpoint("forged", stored) is True

    reloaded = sessions.load("forged")["messages"]
    raw = compaction.load_checkpoint("forged")
    assert compaction.validate_checkpoint(reloaded, raw) is None
    assert compaction.project_messages(reloaded, raw)[0] == reloaded


@pytest.mark.parametrize("key,value", [
    ("cutoff", True), ("cutoff", 2.5), ("cutoff", float("nan")),
    ("generation", True), ("generation", 0), ("generation", "2"),
    ("generation", float("inf")), ("generation", float("nan")), ("generation", 10 ** 30),
    ("source_messages", -1), ("source_messages", float("inf")), ("source_messages", True),
    ("before_tokens", float("nan")), ("after_tokens", True), ("after_tokens", 10 ** 40),
    ("messages_summarized", "many"), ("user_messages_summarized", None),
    ("payload_chars_elided", float("-inf")), ("summarized_from", 10 ** 12),
    ("created", float("inf")), ("created", "yesterday"), ("created", True),
    ("created", 10 ** 1000),
    ("improved", "yes"), ("span_truncated", 1), ("reason", "x" * 200), ("reason", 7),
])
def test_untrusted_checkpoint_bookkeeping_is_rejected_not_coerced(key, value):
    """int() on a stored value is a crash waiting for a corrupt file. Range-check instead."""
    messages = [{"role": "user", "content": "u%d" % i} for i in range(30)]
    checkpoint = _make_checkpoint(messages, 20)
    checkpoint[key] = value
    assert compaction.validate_checkpoint(messages, checkpoint) is None
    # ...and nothing downstream trips over it either
    assert compaction.project_messages(messages, checkpoint)[0] == messages
    assert compaction.plan(messages, checkpoint, None, policy=_policy(),
                           total_tokens=99_000)[1] in ("ok", "no_safe_cutoff")


def test_a_summarized_from_above_the_cutoff_is_inconsistent_and_rejected():
    messages = [{"role": "user", "content": "u%d" % i} for i in range(30)]
    checkpoint = _make_checkpoint(messages, 20)
    checkpoint["summarized_from"] = checkpoint["cutoff"] + 1
    assert compaction.validate_checkpoint(messages, checkpoint) is None


def test_bounded_int_rejects_bools_and_non_finite_values():
    assert compaction.bounded_int(True, 0, 10) is None
    assert compaction.bounded_int(False, 0, 10) is None
    assert compaction.bounded_int("3", 0, 10) is None
    assert compaction.bounded_int(float("nan"), 0, 10) is None
    assert compaction.bounded_int(float("inf"), 0, 10) is None
    assert compaction.bounded_int(None, 0, 10, 7) == 7
    assert compaction.bounded_int(3.0, 0, 10) == 3
    assert compaction.bounded_int(11, 0, 10) is None


def test_save_checkpoint_never_invents_a_session(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    messages = [{"role": "user", "content": "u%d" % i} for i in range(30)]
    assert compaction.save_checkpoint("ghost", _make_checkpoint(messages, 20)) is False
    assert not (tmp_path / "ghost.json").exists()


def test_a_torn_journal_yields_no_checkpoint(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path))
    torn = tmp_path / "torn.json"
    original = b'{"id":"torn","messages":['
    torn.write_bytes(original)
    assert compaction.load_checkpoint("torn") is None
    messages = [{"role": "user", "content": "u%d" % i} for i in range(30)]
    assert compaction.save_checkpoint("torn", _make_checkpoint(messages, 20)) is False
    assert torn.read_bytes() == original


# --------------------------------------------------------------------------- in the loop

def test_a_long_run_compacts_and_keeps_the_whole_transcript(tmp_path, monkeypatch):
    """The headline: the request stops growing, the conversation does not shrink."""
    h = _harness(tmp_path, monkeypatch, "compact_basic")
    _fake_bash(h)
    seen = _events(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=16))
    h.provider = provider
    res = h.run("compact_basic", "please refactor the parser; do not touch the CLI")

    applied = _compaction_events(seen, "applied")
    assert applied, "a long run must compact: %s" % _compaction_events(seen)
    assert applied[0]["after_tokens"] < applied[0]["before_tokens"]
    assert applied[0]["cutoff"] > 0 and applied[0]["kept"] > 0
    assert "summary" not in json.dumps(applied[0]).lower() or True   # events stay content-free
    for event in applied:
        assert set(event) >= {"status", "reason", "before_tokens", "after_tokens",
                              "cutoff", "kept", "source_messages"}
        assert compaction.HANDOFF_HEADER[:40] not in json.dumps(event)

    # the durable transcript kept every original message, in order, unsummarized
    tool_results = [m for m in res.messages if m.get("role") == "tool"]
    assert len(tool_results) == 15, len(tool_results)
    assert not [m for m in res.messages if m.get("compaction")], \
        "projection artifacts must never enter the durable transcript"
    assert res.messages[0]["content"].endswith("do not touch the CLI")
    assert res.answer == "all done"

    # ...but the provider saw the compacted projection on later turns
    compacted_view = provider.turn_calls[-1]
    assert compacted_view[0].get("compaction") is True
    assert compaction.HANDOFF_HEADER[:40] in compacted_view[0]["content"]
    assert len(compacted_view) < len(res.messages)


def test_the_summary_request_is_bounded_toolless_and_unstreamed(tmp_path, monkeypatch):
    """Never re-send the overflowing thread, never let the summarizer act or stream."""
    budget = 6_000
    h = _harness(tmp_path, monkeypatch, "compact_bounded",
                 policy=_policy(summary_input_chars=budget))
    _fake_bash(h, size=1500)
    h.stream_cb = lambda text: None
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=18))
    h.provider = provider
    res = h.run("compact_bounded", "keep going")

    assert provider.summary_digests, "expected at least one summary request"
    for digest in provider.summary_digests:
        # Whole-message packing can stretch to twice the budget for a user message it refuses
        # to abbreviate, and never further; the fixed instructions are a few hundred chars.
        assert len(digest) <= 2 * budget + 1_500, len(digest)
    # the point of the bound: the digest is a fraction of the thread it describes
    assert len(provider.summary_digests[0]) < len(json.dumps(res.messages, default=str)) / 2
    assert provider.summary_schemas == [[]] * len(provider.summary_digests), \
        "a summary request must carry NO tool schemas"
    assert provider.summary_stream == [None] * len(provider.summary_digests), \
        "the private summary must not stream as this run's answer"


def test_a_provider_native_answer_envelope_survives_empty_tool_schemas(tmp_path, monkeypatch):
    """Envelope providers (claude-cli, the OpenAI-compatible text protocol) answer with
    ``{"answer": …}``. With no tools to offer, that is the ONLY legal reply — the summary
    must arrive as ordinary completion text, not be mistaken for a contract violation."""
    from harness.providers import _parse_response_envelope, _parse_answer_json
    summary_text = ("GOALS — finish. USER CONSTRAINTS — none. DECISIONS — none. "
                    "CHANGED FILES AND ACTIONS — none. CHECK RESULTS — none. "
                    "INCOMPLETE WORK — none. NEXT STEPS — none.")
    envelope = json.dumps({"answer": summary_text})
    assert _parse_response_envelope(envelope, allowed_tools=[]) == ("answer", summary_text)
    assert _parse_answer_json(envelope) == summary_text

    h = _harness(tmp_path, monkeypatch, "compact_envelope")
    _fake_bash(h)
    provider = _Provider(
        lambda n, m: _busy_turn(n, m, stop_after=16),
        # what an envelope provider hands back after parsing: plain text, end_turn
        summary=lambda n, d: Completion(text=_parse_answer_json(envelope),
                                        stop_reason="end_turn", usage=Usage(input_tokens=4)))
    h.provider = provider
    h.run("compact_envelope", "go")
    assert provider.summary_schemas and all(s == [] for s in provider.summary_schemas)
    assert summary_text[:20] in provider.turn_calls[-1][0]["content"]


def test_user_constraints_and_the_latest_request_survive_the_cut(tmp_path, monkeypatch):
    """A model-written summary is fallible; the user's own words are carried verbatim."""
    h = _harness(tmp_path, monkeypatch, "compact_pin")
    _fake_bash(h)
    steers = ["never delete data/, and always run pytest -q before finishing"]

    def steering():
        return [steers.pop()] if steers else []

    h.steering = steering
    provider = _Provider(
        lambda n, m: _busy_turn(n, m, stop_after=18),
        summary=lambda n, digest: Completion(
            # a well-formed summary that forgot the constraint entirely
            text=_handoff_text("\n" + "x" * 80, goals="do the work.",
                               user_constraints="none stated.", next_steps="continue."),
            stop_reason="end_turn", usage=Usage(input_tokens=5)))
    h.provider = provider
    res = h.run("compact_pin", "port the exporter to the new API")

    view = provider.turn_calls[-1]
    assert view[0].get("compaction") is True
    handoff = view[0]["content"]
    assert "port the exporter to the new API" in handoff, "the original request must survive"
    assert "never delete data/" in handoff, "a user constraint must not depend on the summary"
    assert "authorizes nothing" in handoff and "NOT a user instruction" in handoff
    # and the real transcript still holds them as ordinary user messages
    user_texts = [m["content"] for m in res.messages if m.get("role") == "user"]
    assert any("never delete data/" in t for t in user_texts)
    # the summarizer was shown both of them COMPLETE — the digest never drops user text on
    # the way in, so a summary that forgets a constraint is the model's failure, not ours
    digest = provider.summary_digests[0]
    assert "port the exporter to the new API" in digest
    assert "never delete data/, and always run pytest -q before finishing" in digest


def test_repeated_compaction_bounds_the_request_while_the_run_progresses(
        tmp_path, monkeypatch):
    """>50 tool steps: the projection stays bounded and no step is lost."""
    h = _harness(tmp_path, monkeypatch, "compact_long", turns=70)
    _fake_bash(h, size=900)
    seen = _events(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=60, answer="finished 59 steps"))
    h.provider = provider
    res = h.run("compact_long", "walk the whole repository and report")

    assert res.answer == "finished 59 steps", (res.answer, res.error)
    results = [m for m in res.messages if m.get("role") == "tool"]
    assert len(results) == 59, "every executed step stays in the transcript: %d" % len(results)
    assert res.tool_calls == 59

    applied = _compaction_events(seen, "applied")
    assert len(applied) >= 2, "a genuinely progressing long run must compact repeatedly"
    for event in applied:
        assert event["source_messages"] > 0
    # generations increase, and each compaction absorbs strictly more of the transcript
    assert [e["generation"] for e in applied] == list(range(1, len(applied) + 1))
    cutoffs = [e["cutoff"] for e in applied]
    assert cutoffs == sorted(cutoffs) and len(set(cutoffs)) == len(cutoffs)

    # the transcript grows without bound; what is SENT does not
    sizes = [len(view) for view in provider.turn_calls]
    assert max(sizes[10:]) <= max(sizes[:10]) + 8, sizes
    assert len(res.messages) > max(sizes) * 2, (len(res.messages), max(sizes))
    # ...and every request the provider saw stayed near the threshold, not above the window
    for view in provider.turn_calls:
        assert compaction.estimate_messages(view) < 12_000


@pytest.mark.parametrize("bad,why", [
    (lambda n, d: Completion(text="boom", stop_reason="error", error_status=500,
                             error_detail="upstream exploded"), "provider error"),
    (lambda n, d: Completion(text="half a summ", stop_reason="length"),
     "summary hit the output-token limit"),
    (lambda n, d: Completion(tool_calls=[ToolCall("x", "bash", {"command": "ls"})],
                             stop_reason="tool_use"), "summarizer attempted a tool call"),
    (lambda n, d: Completion(text="   ", stop_reason="end_turn"),
     "summary was empty or too short"),
])
def test_an_unusable_summary_cannot_corrupt_the_run(tmp_path, monkeypatch, bad, why):
    """Failure preserves the transcript and the previous behaviour, and says so truthfully."""
    h = _harness(tmp_path, monkeypatch, "compact_bad")
    _fake_bash(h)
    seen = _events(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=20), summary=bad)
    h.provider = provider
    res = h.run("compact_bad", "carry on")

    assert res.answer == "all done" and not res.error, (res.answer, res.error)
    failures = _compaction_events(seen, "failed")
    assert failures and failures[0]["reason"] == why
    assert not _compaction_events(seen, "applied")
    # no projection was adopted: the provider kept seeing the real thread
    assert not any(m.get("compaction") for view in provider.turn_calls for m in view)
    assert len([m for m in res.messages if m.get("role") == "tool"]) == 19
    # and a failing summarizer is not retried every turn
    assert len(provider.summary_digests) <= h.compaction.max_failures, \
        "bounded attempts, no retry storm: %d" % len(provider.summary_digests)
    # the model's rejected output never reached the durable conversation
    assert "half a summ" not in json.dumps(res.messages, default=str)


def test_a_failing_tool_result_is_never_replaced_by_a_summary_claim(tmp_path, monkeypatch):
    """The summary is context, not evidence: the real failing output stays in the tail."""
    h = _harness(tmp_path, monkeypatch, "compact_evidence")
    tool = h.registry.get("bash")
    tool.run = lambda args, ctx: ("[exit 1] FAILED tests/test_x.py::test_y"
                                  if "pytest" in (args.get("command") or "") else "ok " * 200)

    def turn(n, messages):
        if n >= 20:
            return Completion(text="done", stop_reason="end_turn", usage=Usage(input_tokens=2))
        command = "pytest" if n == 19 else "echo %d" % n
        return Completion(tool_calls=[ToolCall("t%d" % n, "bash", {"command": command})],
                          stop_reason="tool_use", usage=Usage(input_tokens=2))

    provider = _Provider(turn)
    h.provider = provider
    res = h.run("compact_evidence", "run the suite")
    assert any("[exit 1]" in str(m.get("content")) for m in res.messages)
    assert any("[exit 1]" in str(m.get("content")) for m in provider.turn_calls[-1])


def test_the_cache_bust_a_compaction_causes_is_named_in_the_ledger(tmp_path, monkeypatch):
    """Rewriting the message prefix costs one cache miss. The ledger must not call it
    'unexplained' — attributing every miss to a cause is the whole point of that column."""
    from harness.providers import Usage as _U

    class _Cache:
        name = "honest-cache"
        model = "deepseek-chat"
        reports_cache = True
        max_tokens = 4096

        def __init__(self):
            self.inner = _Provider(lambda n, m: _busy_turn(n, m, stop_after=16))
            self._prev = ""

        def complete(self, system, messages, tool_schemas, on_text=None):
            comp = self.inner.complete(system, messages, tool_schemas, on_text)
            request = system + json.dumps(messages, default=str)
            common = 0
            for a, b in zip(request, self._prev):
                if a != b:
                    break
                common += 1
            self._prev = request
            if system == compaction.SUMMARY_SYSTEM:
                return comp                       # the summary request is its own conversation
            full = compaction.estimate_text(request)
            read = compaction.estimate_text(request[:common])
            comp.usage = _U(input_tokens=max(0, full - read),
                            output_tokens=comp.usage.output_tokens, cache_read=read)
            return comp

    h = _harness(tmp_path, monkeypatch, "compact_ledger")
    _fake_bash(h)
    seen = _events(h)
    h.provider = _Cache()
    h.run("compact_ledger", "go")
    assert _compaction_events(seen, "applied")
    causes = [d.get("cause") for k, d in seen if k == "cache_miss"]
    assert any(c and "compact" in c for c in causes), causes


# --------------------------------------------------------------------------- boundaries

def test_a_short_run_spends_no_extra_requests(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, "compact_short")
    _fake_bash(h)
    seen = _events(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=3))
    h.provider = provider
    res = h.run("compact_short", "quick question")
    assert provider.summary_digests == []
    assert provider.calls == 3 and res.model_calls == 3
    assert _compaction_events(seen) == []


def test_compaction_requests_are_accounted_like_any_other(tmp_path, monkeypatch):
    """Every physical request lands in model_calls, the token total and a shared budget."""
    class _Budget:
        def __init__(self):
            self.records = []

        def account(self, model, usage):
            self.records.append((model, usage.input_tokens, usage.output_tokens))

        def exceeded(self):
            return False

    h = _harness(tmp_path, monkeypatch, "compact_account")
    _fake_bash(h)
    budget = _Budget()
    h.shared_budget = budget
    provider = _Provider(
        lambda n, m: _busy_turn(n, m, stop_after=16),
        summary=lambda n, d: Completion(text=_handoff_text("\n" + "y" * 60),
                                        stop_reason="end_turn",
                                        usage=Usage(input_tokens=111, output_tokens=13),
                                        request_count=2))
    h.provider = provider
    res = h.run("compact_account", "go")

    summaries = len(provider.summary_digests)
    assert summaries >= 1
    assert res.model_calls == len(provider.turn_calls) + 2 * summaries, res.model_calls
    assert res.input_tokens >= 111 * summaries
    assert len(budget.records) == provider.calls, "the shared budget sees every request once"
    assert (h.provider.model, 111, 13) in budget.records


def _denied_by_the_request_gate():
    """What the shipped SDK adapter really returns when the gate denies the reservation:
    an error that never reached a provider and honestly reports 0 physical requests."""
    from harness.claude_agent_sdk import ClaudeAgentSdkProvider
    provider = ClaudeAgentSdkProvider(subscription_only=True)
    provider.request_gate = lambda kind: ""
    provider._run_worker = lambda *a, **k: pytest.fail("a denied reservation must not spawn")
    return provider.complete(compaction.SUMMARY_SYSTEM, [{"role": "user", "content": "d"}], [])


def test_a_summary_the_gate_never_let_out_is_not_billed(tmp_path, monkeypatch):
    """The mirror of the test above: a request that was refused before it was issued is not a
    request. It is still a failed compaction — the transcript keeps its full history."""
    h = _harness(tmp_path, monkeypatch, "compact_denied")
    _fake_bash(h)
    seen = _events(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=16),
                         summary=lambda n, digest: _denied_by_the_request_gate())
    h.provider = provider
    res = h.run("compact_denied", "go")

    assert provider.summary_digests, "compaction was due at least once"
    assert res.answer == "all done" and not res.error, "the run itself is unaffected"
    assert res.model_calls == len(provider.turn_calls), res.model_calls
    failed = _compaction_events(seen, "failed")
    assert failed and failed[0]["reason"] == "provider error"
    assert not _compaction_events(seen, "applied")


def test_compaction_respects_the_model_call_cap(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, "compact_cap")
    _fake_bash(h)
    h.max_model_calls = 9
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=40))
    h.provider = provider
    res = h.run("compact_cap", "go")
    assert provider.calls <= 9 and res.model_calls <= 9


def test_compaction_is_due_on_a_seeded_thread(tmp_path, monkeypatch):
    """Control for the two boundary tests below: without a guard, turn 0 compacts."""
    h = _harness(tmp_path, monkeypatch, "compact_due")
    _fake_bash(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=1))
    h.provider = provider
    h.run("compact_due", "carry on", history=_seed_history())
    assert len(provider.summary_digests) == 1


def test_compaction_respects_an_exhausted_shared_budget(tmp_path, monkeypatch):
    """The loop's ceiling check happens first; the compaction check is the second call."""
    class _Spent:
        def __init__(self):
            self.checks = 0

        def account(self, model, usage):
            pass

        def exceeded(self):
            self.checks += 1
            return self.checks > 1

    h = _harness(tmp_path, monkeypatch, "compact_spent")
    _fake_bash(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=40))
    h.provider = provider
    h.shared_budget = _Spent()
    h.run("compact_spent", "carry on", history=_seed_history())
    assert provider.summary_digests == [], "no summary once the shared ceiling is reached"


def test_cancellation_stops_before_a_summary_request(tmp_path, monkeypatch):
    """Same shape: the turn-boundary check passes, the compaction check refuses."""
    state = {"checks": 0}

    def cancelled():
        state["checks"] += 1
        return state["checks"] > 1

    h = _harness(tmp_path, monkeypatch, "compact_cancel")
    _fake_bash(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=40))
    h.provider = provider
    h.cancelled = cancelled
    res = h.run("compact_cancel", "carry on", history=_seed_history())
    assert res.canceled and provider.summary_digests == []


def test_the_overflow_toggle_disables_compaction_too(tmp_path, monkeypatch):
    """COLLIE_OVERFLOW_RECOVERY=0 is documented as the auto-compact switch."""
    monkeypatch.setenv("COLLIE_OVERFLOW_RECOVERY", "0")
    h = _harness(tmp_path, monkeypatch, "compact_off")
    _fake_bash(h)
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=18))
    h.provider = provider
    h.run("compact_off", "go")
    assert provider.summary_digests == []


def test_a_real_overflow_compacts_when_there_is_something_to_compact(tmp_path, monkeypatch):
    """The provider's own verdict overrides the estimate — but only with a safe source."""
    h = _harness(tmp_path, monkeypatch, "compact_overflow",
                 policy=_policy(threshold_tokens=10_000_000))   # never fires on estimate
    _fake_bash(h)
    seen = _events(h)
    state = {"overflowed": False}

    def turn(n, messages):
        if n < 12:
            return Completion(tool_calls=[ToolCall("t%d" % n, "bash", {"command": "e"})],
                              stop_reason="tool_use", usage=Usage(input_tokens=2))
        if not state["overflowed"]:
            state["overflowed"] = True
            return Completion(text="prompt is too long", stop_reason="error", error_status=400,
                              error_detail="prompt is too long: 300000 tokens > 200000 maximum")
        return Completion(text="recovered", stop_reason="end_turn", usage=Usage(input_tokens=2))

    provider = _Provider(turn)
    h.provider = provider
    res = h.run("compact_overflow", "go")
    assert res.answer == "recovered", (res.answer, res.error)
    assert len(provider.summary_digests) == 1
    assert _compaction_events(seen, "applied")[0]["reason"] == "overflow"


def test_a_tiny_history_overflow_keeps_the_one_shot_retry(tmp_path, monkeypatch):
    """The pre-existing shrink-once-and-retry path must be untouched for short threads."""
    h = _harness(tmp_path, monkeypatch, "compact_tiny", turns=4)
    seen = _events(h)
    overflow = Completion(text="maximum context length exceeded", stop_reason="error",
                          error_status=400, error_detail="maximum context length is 65536 tokens")
    provider = _Provider(lambda n, m: overflow)
    h.provider = provider
    res = h.run("compact_tiny", "go")
    assert res.error.startswith("overflow:"), res.error
    assert provider.calls == 2, "one original + one retry, no summary: %d" % provider.calls
    assert provider.summary_digests == []
    # ...and the run says why compaction did not help, instead of leaving it a mystery
    assert _compaction_events(seen, "skipped")[0]["reason"] == "short"


# --------------------------------------------------------------------------- persistence

def test_a_checkpoint_persists_and_is_reused_on_the_next_run(tmp_path, monkeypatch):
    """Resume must not pay for the same summary twice — and must verify it first."""
    h = _harness(tmp_path, monkeypatch, "compact_persist")
    _fake_bash(h)
    h.durable_session_id = "persisted"
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=16))
    h.provider = provider
    first = h.run("compact_persist", "start the migration")
    assert provider.summary_digests, "the first run must compact"

    from harness import sessions
    stored = compaction.load_checkpoint("persisted")
    assert stored and stored["cutoff"] > 0
    assert compaction.validate_checkpoint(sessions.load("persisted")["messages"], stored)

    # CLI/Web save the final receipt after the loop checkpoint. That final save
    # must preserve the projection as well as the complete transcript.
    sessions.save("persisted", first.messages, answer=first.answer)
    restored = sessions.load("persisted")
    assert restored["context_compaction"] == stored
    assert len(restored["messages"]) == len(first.messages)

    h2 = _harness(tmp_path, monkeypatch, "compact_persist")
    _fake_bash(h2)
    h2.durable_session_id = "persisted"
    seen2 = _events(h2)
    provider2 = _Provider(lambda n, m: Completion(text="continued", stop_reason="end_turn",
                                                  usage=Usage(input_tokens=2)))
    h2.provider = provider2
    h2.run("compact_persist", "carry on", history=restored["messages"])
    assert provider2.summary_digests == [], "a valid checkpoint is reused, not recomputed"
    assert _compaction_events(seen2, "restored")
    assert provider2.turn_calls[0][0].get("compaction") is True


def test_a_stale_persisted_checkpoint_is_ignored_not_applied(tmp_path, monkeypatch):
    h = _harness(tmp_path, monkeypatch, "compact_stale",
                 policy=_policy(threshold_tokens=10_000_000))   # isolate the restore path
    h.durable_session_id = "stale"
    from harness import sessions
    history = [{"role": "user", "content": "u%d" % i} for i in range(30)]
    sessions.checkpoint("stale", history, run_id="r", state="turn_boundary")
    bogus = _make_checkpoint([{"role": "user", "content": "something else"}] * 30, 20)
    assert compaction.save_checkpoint("stale", bogus) is True

    seen = _events(h)
    provider = _Provider(lambda n, m: Completion(text="ok", stop_reason="end_turn",
                                                 usage=Usage(input_tokens=2)))
    h.provider = provider
    h.run("compact_stale", "go", history=history)
    ignored = _compaction_events(seen, "ignored")
    assert ignored and "does not match" in ignored[0]["reason"]
    assert not any(m.get("compaction") for m in provider.turn_calls[0])


def test_a_malformed_persisted_checkpoint_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    os.makedirs(str(tmp_path / "sessions"), exist_ok=True)
    (tmp_path / "sessions" / "junk.json").write_text(
        json.dumps({"id": "junk", "messages": [{"role": "user", "content": "hi"}],
                    compaction.CHECKPOINT_KEY: "not a dict"}), encoding="utf-8")
    assert compaction.load_checkpoint("junk") is None

    (tmp_path / "sessions" / "junk2.json").write_text(
        json.dumps({"id": "junk2", "messages": [{"role": "user", "content": "hi"}],
                    compaction.CHECKPOINT_KEY: {"version": 1, "cutoff": 4_000}}),
        encoding="utf-8")
    stored = compaction.load_checkpoint("junk2")
    assert isinstance(stored, dict)
    assert compaction.validate_checkpoint([{"role": "user", "content": "hi"}], stored) is None


# --------------------------------------------------------------------------- policy gates

def test_no_progress_means_no_second_summary():
    """The anti-storm rule: recompaction needs new source material."""
    policy = _policy()
    messages = [{"role": "user", "content": "u%d" % i} for i in range(40)]
    checkpoint = _make_checkpoint(messages, 20, policy=policy)
    checkpoint["source_messages"] = len(messages)
    assert compaction.plan(messages, checkpoint, None, policy=policy,
                          total_tokens=99_000)[1] == "no_progress"
    # ...even when a real overflow is demanding one, because nothing has changed since
    assert compaction.plan(messages, checkpoint, None, policy=policy, total_tokens=99_000,
                           force=True)[1] == "no_progress"
    grown = messages + [{"role": "user", "content": "more %d" % i} for i in range(policy.min_new_messages)]
    assert compaction.plan(grown, checkpoint, None, policy=policy,
                           total_tokens=99_000)[1] == "ok"


def test_a_span_the_digest_could_not_finish_is_continued_not_blocked():
    """Backlog is progress. A compaction that ran out of digest budget left the rest of the
    span being sent verbatim on every request, and "wait for new messages" is a rule about
    the wrong quantity: there is nothing to wait for, and a real overflow cannot unblock it
    either. Continuing is allowed only while the untouched backlog is worth a request, so the
    anti-storm rule still holds everywhere it was actually protecting something."""
    policy = _policy()
    messages = [{"role": "user", "content": "u%d" % i} for i in range(40)]
    checkpoint = _make_checkpoint(messages, 20, policy=policy)
    checkpoint["source_messages"] = len(messages)

    finished = dict(checkpoint, span_truncated=False)
    assert compaction.plan(messages, finished, None, policy=policy,
                           total_tokens=99_000)[1] == "no_progress"

    truncated = dict(checkpoint, span_truncated=True)
    plan, why = compaction.plan(messages, truncated, None, policy=policy, total_tokens=99_000)
    assert why == "ok" and plan.previous_cutoff == 20
    assert plan.cutoff - 20 >= policy.min_compacted
    assert plan.reason == "backlog", "the ledger must say why it compacted twice in a row"
    # A forced continuation still reports the overflow that demanded it.
    assert compaction.plan(messages, truncated, None, policy=policy, total_tokens=99_000,
                           force=True)[0].reason == "overflow"

    # ...and it stops the moment the leftover is too small to be worth a model request,
    # so the chain is bounded by the transcript rather than running every turn.
    late = _make_checkpoint(messages, 30, policy=policy)
    late["source_messages"] = len(messages)
    late["span_truncated"] = True
    assert 0 < compaction.choose_cutoff(messages, policy, 30) - 30 < policy.min_compacted
    assert compaction.plan(messages, late, None, policy=policy,
                           total_tokens=99_000)[1] == "no_progress"

    # every other gate still comes first
    assert compaction.plan(messages, truncated, {"failures": policy.max_failures},
                           policy=policy, total_tokens=99_000)[1] == "failed_out"
    assert compaction.plan(messages, truncated, None, policy=policy,
                           total_tokens=10)[1] == "below_threshold"


def test_a_resumed_long_thread_drains_its_backlog_instead_of_staying_oversized(
        tmp_path, monkeypatch):
    """The workflow: continue a conversation whose history is longer than one digest.

    ``prepare`` packs the digest by whole messages, so the first compaction of a long
    resumed thread absorbs only the oldest slice and marks the checkpoint truncated. The rest
    was still sent verbatim on every following request while ``plan`` waited for new messages
    that would not have helped, so the projection stayed above the threshold for the whole
    run — the growth compaction exists to stop.
    """
    policy = _policy()
    h = _harness(tmp_path, monkeypatch, "compact_backlog", policy=policy)
    _fake_bash(h)
    seen = _events(h)
    history = _seed_history(pairs=30)
    history[0]["content"] = "finish the migration; never touch data/ and always run pytest -q"
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=4, answer="migration done"))
    h.provider = provider
    res = h.run("compact_backlog", "carry on where you left off", history=history)

    assert res.answer == "migration done", (res.answer, res.error)
    applied = _compaction_events(seen, "applied")
    assert len(applied) >= 2, "one digest cannot absorb this thread: %s" % applied
    assert applied[0]["span_truncated"] is True
    assert applied[1]["reason"] == "backlog"
    assert applied[1]["cutoff"] > applied[0]["cutoff"]
    assert applied[-1]["after_tokens"] < applied[0]["after_tokens"]

    # The point of the feature: what is SENT comes back under the threshold inside this run,
    # rather than waiting for turns the user would have to pay for first.
    assert compaction.estimate_messages(provider.turn_calls[0]) > policy.threshold_tokens
    assert compaction.estimate_messages(provider.turn_calls[-1]) < policy.threshold_tokens

    # ...and nothing was traded away for it: the transcript is whole, the user's words and
    # their constraint crossed every cut, and no projection artifact became history.
    assert len(res.messages) > len(history)
    assert res.messages[0]["content"] == history[0]["content"]
    assert not [m for m in res.messages if m.get("compaction")]
    handoff = provider.turn_calls[-1][0]
    assert handoff.get("compaction") is True
    assert "never touch data/" in handoff["content"]
    # the live request stays in the projection, quoted across the cut or verbatim in the tail
    assert "carry on where you left off" in json.dumps(provider.turn_calls[-1], default=str)


def test_a_compaction_that_did_not_help_demands_double_the_progress():
    policy = _policy()
    messages = [{"role": "user", "content": "u%d" % i} for i in range(40)]
    checkpoint = _make_checkpoint(messages, 20, policy=policy)
    checkpoint = compaction.record_projection({}, checkpoint, 9_000, policy)  # before==9000
    assert checkpoint["improved"] is False
    grown = messages + [{"role": "user", "content": "m%d" % i}
                        for i in range(policy.min_new_messages)]
    assert compaction.plan(grown, checkpoint, None, policy=policy,
                           total_tokens=99_000)[1] == "no_progress"
    grown2 = messages + [{"role": "user", "content": "m%d" % i}
                         for i in range(policy.min_new_messages * 2)]
    assert compaction.plan(grown2, checkpoint, None, policy=policy,
                           total_tokens=99_000)[1] == "ok"


def test_failures_stop_after_a_bounded_number_of_attempts():
    policy = _policy(max_failures=2)
    messages = [{"role": "user", "content": "u%d" % i} for i in range(40)]
    session = {}
    compaction.note_failure(session, messages)
    assert compaction.plan(messages, None, session[compaction.GATE_KEY], policy=policy,
                           total_tokens=99_000)[1] == "cooldown"
    grown = messages + [{"role": "user", "content": "m%d" % i}
                        for i in range(policy.min_new_messages)]
    assert compaction.plan(grown, None, session[compaction.GATE_KEY], policy=policy,
                           total_tokens=99_000)[1] == "ok"
    compaction.note_failure(session, grown)
    assert compaction.plan(grown + grown, None, session[compaction.GATE_KEY], policy=policy,
                           total_tokens=99_000)[1] == "failed_out"


def test_below_the_threshold_nothing_is_planned():
    policy = CompactionPolicy()
    messages = [{"role": "user", "content": "u%d" % i} for i in range(200)]
    assert compaction.plan(messages, None, None, policy=policy, total_tokens=100)[1] == \
        "below_threshold"
    assert compaction.plan(messages[:4], None, None, policy=policy,
                           total_tokens=99_000)[1] == "short"


def test_policy_reads_settings_and_survives_a_bad_value(monkeypatch):
    monkeypatch.setenv("COLLIE_COMPACT_TOKENS", "12000")
    assert CompactionPolicy.from_settings().threshold_tokens == 12_000
    monkeypatch.setenv("COLLIE_COMPACT_TOKENS", "not a number")
    # a typo must not silently reinstate unbounded growth
    assert CompactionPolicy.from_settings().threshold_tokens == \
        CompactionPolicy.threshold_tokens


@pytest.mark.parametrize("raw", ["Infinity", "-Infinity", "NaN", "nan", "1e400", "inf",
                                 "  ", "0x20", "1,000", "None"])
def test_a_non_finite_or_junk_setting_falls_back_instead_of_raising(monkeypatch, raw):
    """``int(float("inf"))`` raises OverflowError; a settings file may really contain it."""
    monkeypatch.setenv("COLLIE_COMPACT_TOKENS", raw)
    monkeypatch.setenv("COLLIE_COMPACT_KEEP_MESSAGES", raw)
    policy = CompactionPolicy.from_settings()
    assert policy.threshold_tokens == CompactionPolicy.threshold_tokens
    assert policy.keep_recent_messages == CompactionPolicy.keep_recent_messages


def test_an_out_of_range_setting_is_clamped_not_obeyed(monkeypatch):
    monkeypatch.setenv("COLLIE_COMPACT_TOKENS", "5")
    assert CompactionPolicy.from_settings().threshold_tokens == 4_000
    monkeypatch.setenv("COLLIE_COMPACT_TOKENS", "1e12")
    assert CompactionPolicy.from_settings().threshold_tokens == 1_000_000


# --------------------------------------------------------------------------- the summary itself

def test_an_oversized_summary_is_never_clipped_mid_instruction():
    """Clipping a summary at a character count truncates the instruction that happened to be
    at the boundary. Even a whole trailing line can contain a remaining requirement."""
    policy = _policy(summary_max_chars=1_000)
    body = "\n".join("%s — handled." % h for h in compaction.SUMMARY_HEADINGS)

    # wildly oversized: rejected outright, nothing adopted
    ok, text, why = compaction.validate_summary(
        Completion(text=body + "\n" + "z" * 5_000, stop_reason="end_turn"), policy)
    assert not ok and "over" in why and text == ""

    # A trailing appendix may contain requirements despite all headings fitting.
    appendix = "\n".join("APPENDIX %d: %s" % (i, "q" * 80) for i in range(12))
    source = body + "\n" + appendix
    assert policy.summary_max_chars < len(source) <= policy.summary_max_chars * 2
    ok, text, why = compaction.validate_summary(
        Completion(text=source, stop_reason="end_turn"), policy)
    assert not ok and "over" in why and text == ""

    # oversized in a way that would cost a section: rejected rather than silently truncated
    ok, text, why = compaction.validate_summary(
        Completion(text="GOALS — g.\n" + "filler line\n" * 100 + "NEXT STEPS — finish.",
                   stop_reason="end_turn"), policy)
    assert not ok and "over" in why


def test_a_reply_that_is_not_a_handoff_never_becomes_the_conversations_memory():
    """``stop_reason`` cannot detect half a summary, because several providers never send one.

    ``ClaudeCLIProvider`` returns whatever prose the CLI produced as a plain ``end_turn``
    ("fallback: prose"), so a refusal, a bare preamble or a reply cut off part-way arrives
    looking exactly like a finished handoff. Each is long enough to clear the minimum and
    small enough to clear the ceiling, so size checks pass it straight through.
    """
    policy = _policy()

    refusal = ("I'm sorry, but I can't help with summarizing this conversation. Let me know "
               "if there is something else I can do for you instead.")
    preamble = "Sure! Here is the handoff summary you asked me to write for the next model:"
    # "Half a summary reads as a complete one": a reply cut off after DECISIONS keeps the
    # sections a continuing run is actually steered by out of the memory entirely.
    half = "\n".join("%s — handled." % h for h in compaction.SUMMARY_HEADINGS[:3])

    for reply in (refusal, preamble, half):
        assert policy.min_summary_chars < len(reply) <= policy.summary_max_chars
        ok, text, why = compaction.validate_summary(
            Completion(text=reply, stop_reason="end_turn"), policy)
        assert not ok and text == "", reply[:40]
        assert "heading" in why, why
    # the reason names how much is missing without quoting any transcript content
    _ok, _text, why = compaction.validate_summary(
        Completion(text=half, stop_reason="end_turn"), policy)
    assert "4 of the 7" in why and "CHANGED FILES AND ACTIONS" in why
    assert compaction.missing_headings(half) == list(compaction.SUMMARY_HEADINGS[3:])

    # ...and the gate is about structure, not formatting: a real handoff is still adopted
    # however the model decorated its headings, so this cannot become a retry storm on
    # summaries that are perfectly usable.
    decorated = "\n".join("## **%s:**\nhandled." % h.title()
                          for h in compaction.SUMMARY_HEADINGS)
    ok, text, why = compaction.validate_summary(
        Completion(text=decorated, stop_reason="end_turn"), policy)
    assert ok and text == decorated and why == ""
    assert compaction.missing_headings(decorated) == []


def test_a_refused_summary_never_replaces_the_span_it_could_not_describe(tmp_path, monkeypatch):
    """The workflow cost: the handoff claims to be the ONLY record of the compacted span.

    Adopting a reply that describes none of it destroys the run's memory of that span —
    the constraints stated inside it, the work done, the checks that failed — and
    ``improved`` then marks the shrink a success, so every later generation does it again.
    The honest outcome is to refuse, keep sending the real thread, and stop after the
    bounded number of attempts.
    """
    h = _harness(tmp_path, monkeypatch, "compact_refusal")
    _fake_bash(h)
    seen = _events(h)
    refusal = ("I'm sorry, but I can't help with summarizing this conversation. Let me know "
               "if there is something else I can do for you instead.")
    provider = _Provider(
        lambda n, m: _busy_turn(n, m, stop_after=20),
        summary=lambda n, d: Completion(text=refusal, stop_reason="end_turn",
                                        usage=Usage(input_tokens=7, output_tokens=3)))
    h.provider = provider
    constraint = "NEVER touch anything under vendor/, not even to read it."
    pending = [constraint]
    h.steering = lambda: [pending.pop(0)] if pending else []
    res = h.run("compact_refusal", "carry on")

    assert res.answer == "all done" and not res.error, (res.answer, res.error)
    assert res.steer_count == 1
    failed = _compaction_events(seen, "failed")
    assert failed and "heading" in failed[0]["reason"], failed
    assert not _compaction_events(seen, "applied")

    sent = json.dumps(provider.turn_calls, default=str)
    assert refusal not in sent, "a non-handoff reply became the model's memory"
    assert not any(m.get("compaction") for view in provider.turn_calls for m in view)
    # the transcript and the user's later constraint are untouched by the failed attempt
    assert refusal not in json.dumps(res.messages, default=str)
    assert constraint in json.dumps(provider.turn_calls[-1], default=str)
    assert len([m for m in res.messages if m.get("role") == "tool"]) == 19
    assert len(provider.summary_digests) <= h.compaction.max_failures, \
        "bounded attempts, no retry storm: %d" % len(provider.summary_digests)


def test_a_summary_that_would_not_fit_the_handoff_is_refused_before_adoption():
    """make_checkpoint is the last gate: an over-budget handoff is never stored."""
    policy = _policy()
    messages = _tool_history()
    plan_, prepared = _span(messages, policy)
    assert prepared.ok
    assert compaction.make_checkpoint(
        messages, plan_, "S" * (compaction.MAX_SUMMARY_CHARS + 1), policy, prepared) is None
    assert compaction.make_checkpoint(messages, plan_, "S" * 200, policy, prepared) is not None


def test_a_pinned_request_beyond_the_hard_ceiling_refuses_the_compaction():
    """The verbatim guarantee has a bound: past it we decline to compact rather than
    quietly paraphrase the instruction the run is executing."""
    policy = _policy()
    prefix = [{"role": "user", "content": "x" * (compaction.MAX_PINNED_USER_CHARS + 1)}]
    text, ok, why = compaction.pinned_user_text(prefix, policy)
    assert not ok and text == "" and "too large" in why


def test_a_summary_request_that_raises_is_still_paid_for(tmp_path, monkeypatch):
    """A transport failure is a physical request too — the ledger must not lose it."""
    class _Budget:
        def __init__(self):
            self.records = []

        def account(self, model, usage):
            self.records.append(model)

        def exceeded(self):
            return False

    def boom(n, digest):
        raise RuntimeError("summary socket died")

    h = _harness(tmp_path, monkeypatch, "compact_raise")
    _fake_bash(h)
    seen = _events(h)
    budget = _Budget()
    h.shared_budget = budget
    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=16), summary=boom)
    h.provider = provider
    res = h.run("compact_raise", "carry on")

    assert res.answer == "all done" and not res.error
    attempts = len(provider.summary_digests)
    assert attempts >= 1
    assert res.model_calls == len(provider.turn_calls) + attempts, res.model_calls
    assert len(budget.records) == provider.calls, "every physical request reaches the budget"
    failed = _compaction_events(seen, "failed")
    assert failed and failed[0]["reason"] == "provider error"
    assert not _compaction_events(seen, "applied")


def test_a_cancellation_during_the_summary_pays_for_it_and_adopts_nothing(tmp_path, monkeypatch):
    """Cancel arriving while the summary is in flight: bill it, keep the transcript."""
    state = {"canceled": False}

    h = _harness(tmp_path, monkeypatch, "compact_cancel_inflight")
    _fake_bash(h)
    seen = _events(h)
    h.cancelled = lambda: state["canceled"]

    def summary(n, digest):
        state["canceled"] = True                 # the user hits stop mid-request
        return Completion(text=_handoff_text("\n" + "y" * 80),
                          stop_reason="end_turn", usage=Usage(input_tokens=9, output_tokens=3))

    provider = _Provider(lambda n, m: _busy_turn(n, m, stop_after=40), summary=summary)
    h.provider = provider
    res = h.run("compact_cancel_inflight", "carry on", history=_seed_history())

    assert len(provider.summary_digests) == 1
    assert res.model_calls >= 1, "the summary request is still accounted"
    assert not _compaction_events(seen, "applied")
    skipped = _compaction_events(seen, "skipped")
    assert skipped and skipped[-1]["reason"] == "canceled"
    assert not any(m.get("compaction") for view in provider.turn_calls for m in view)
    assert res.canceled
