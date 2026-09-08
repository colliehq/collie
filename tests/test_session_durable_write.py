"""What a long, mostly unattended run needs from the durable write itself.

These are the promises a person notices when an agent works for an hour without
supervision: the conversation it stops on is the one it can pick up, an action
whose outcome nobody has inspected stays fenced across a restart, an instruction
that was accepted is delivered exactly once, and a message the store can read
back is a message the store agreed to write. They are written against
``sessions``/``task_inbox``/``input_assets`` behaviour, not against how
``_atomic_dump`` happens to encode.
"""
import json
import math
import os
import subprocess
import sys

import pytest

from harness import input_assets, plat, session_owner, sessions, task_inbox

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    directory.mkdir()
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    return str(directory)


def _in_another_process(store, body, timeout=120):
    env = dict(os.environ)
    env["COLLIE_SESSIONS_DIR"] = store
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run([sys.executable, "-c", body], cwd=ROOT, env=env,
                            capture_output=True, text=True, encoding="utf-8",
                            timeout=timeout, **plat.no_window_kwargs())
    assert result.returncode == 0, (result.stdout, result.stderr)
    return result.stdout.strip()


def _nested(depth):
    """Structured data of the shape an MCP/custom tool can legitimately return."""
    node = {"leaf": "value"}
    for level in range(depth):
        node = {"level": level, "child": node}
    return node


# --------------------------------------------------------------- the journal


def test_a_deeply_nested_tool_result_does_not_freeze_the_conversation(store):
    """A tool result the journal can read back must be one the journal accepts.

    ``loop._session_checkpoint`` returns False when a checkpoint raises, and the
    execution loop then refuses to run the *next* tool ("crash recovery could not
    be fenced").  So a transcript the writer rejects but the reader accepts does
    not merely lose one save: it stops the conversation from ever acting again,
    for as long as that message stays in the thread.
    """
    # CPython versions differ in the reader/encoder recursion limit. This case
    # specifically covers documents the current interpreter can already read.
    try:
        json.loads(json.dumps(_nested(1200)))
    except RecursionError:
        pytest.skip("this interpreter cannot read this deeply nested document")
    messages = [
        {"role": "user", "content": "inspect the deployment"},
        {"role": "assistant", "content": "reading the tree",
         "tool_calls": [{"id": "c1", "name": "mcp_inspect", "args": {"target": "tree"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "mcp_inspect",
         "content": _nested(1200)},
    ]
    sessions.checkpoint("deep", messages, run_id="r1", turn=1, state="tool_complete")

    reloaded = sessions.load("deep")
    assert reloaded is not None, "a journal we wrote must be a journal we can read"
    assert len(reloaded["messages"]) == 3
    probe = reloaded["messages"][2]["content"]
    for _ in range(1200):
        probe = probe["child"]
    assert probe == {"leaf": "value"}

    # ...and the run can still fence the next tool, which is what keeps it moving.
    messages.append({"role": "assistant", "content": "now applying the change"})
    sessions.checkpoint("deep", messages, run_id="r1", turn=2, state="executing_tool",
                        detail={"tool_name": "write_file", "tool_call_id": "c2"})
    assert sessions.recovery_state("deep")["recovery_required"] is True


def test_a_long_transcript_survives_repeated_checkpoints_unchanged(store):
    """Repeated tool checkpoints must not duplicate, reorder or mangle history."""
    messages = []
    for index in range(400):
        messages.append({"role": "user", "content": "step %d — naïve ✓ 日本語 🐕\r\n\t" % index})
        messages.append({"role": "assistant", "content": "on it",
                         "tool_calls": [{"id": "c%d" % index, "name": "read_file",
                                         "args": {"path": "sr\\c/ü%d.py" % index}}]})
        messages.append({"role": "tool", "tool_call_id": "c%d" % index,
                         "name": "read_file", "content": "line one\x07two"})
    original = json.loads(json.dumps(messages))

    sessions.append_run_receipt("long", {"kind": "verify", "ok": True})
    for turn in range(25):
        sessions.checkpoint("long", messages, run_id="r", turn=turn,
                            state="executing_tool",
                            detail={"tool_name": "edit_file", "tool_call_id": "c1"})

    loaded = sessions.load("long")
    assert len(loaded["messages"]) == len(original), "history grew without new content"
    assert [m["content"] for m in loaded["messages"]] == [m["content"] for m in original]
    assert loaded["messages"][1]["tool_calls"][0].args == original[1]["tool_calls"][0]["args"]
    assert loaded["run_receipts"][-1]["kind"] == "verify"
    assert sessions.recovery_state("long")["recovery_required"] is True


def test_an_old_session_file_still_loads_and_keeps_growing(store):
    """A journal written by an earlier build stays usable and is not rewritten away."""
    legacy = {
        "id": "old", "project": "web", "cwd": os.getcwd(),
        "updated": 1.0, "last_answer": "café",
        "title": "an older thread",
        "messages": [
            {"role": "user", "content": "ünicode from an old build"},
            # the pre-dataclass on-disk form: a ToolCall repr string
            {"role": "assistant", "content": "looking",
             "tool_calls": ["ToolCall(id='legacy-1', name='grep', args={'pattern': 'x'})"]},
            {"role": "tool", "tool_call_id": "legacy-1", "name": "grep", "content": "hit"},
        ],
    }
    with open(os.path.join(store, "old.json"), "w", encoding="utf-8") as fh:
        json.dump(legacy, fh, ensure_ascii=False)

    loaded = sessions.load("old")
    assert loaded["title"] == "an older thread"
    assert loaded["messages"][1]["tool_calls"][0].name == "grep"

    live = list(loaded["messages"]) + [{"role": "user", "content": "keep going"}]
    sessions.checkpoint("old", live, run_id="r2", turn=0, state="turn_boundary")

    after = sessions.load("old")
    assert [m["content"] for m in after["messages"]] == \
        ["ünicode from an old build", "looking", "hit", "keep going"]
    assert after["title"] == "an older thread"
    assert after["last_answer"] == "café"


@pytest.mark.parametrize("label,tool_call", [
    ("legacy repr string", "ToolCall(id='c1', name='grep', args={'pattern': 'x'})"),
    ("null args", {"id": "c1", "name": "grep", "args": None}),
    ("absent args", {"id": "c1", "name": "grep"}),
    ("unknown extra key", {"id": "c1", "name": "grep", "args": {"pattern": "x"},
                           "type": "function"}),
    ("current shape", {"id": "c1", "name": "grep", "args": {"pattern": "x"}}),
    ("unparseable legacy string", "ToolCall(id='c1', name='grep', args=<unset>)"),
])
def test_resuming_a_thread_never_appends_it_to_itself(store, label, tool_call):
    """Whatever shape a stored tool_call has, resuming must continue — not repeat.

    A journal is written by one build and resumed by another, so the stored form
    of a tool_call and the live form are not always identical objects.  When the
    merge mistakes that for a divergence it appends the entire live history onto
    the stored one, and does it again on every checkpoint after that: the person
    sees their conversation repeat itself and the model is handed the same tool
    calls and results many times over.
    """
    sid = "shape-%d" % abs(hash(label))
    stored = [{"role": "user", "content": "go"},
              {"role": "assistant", "content": "working", "tool_calls": [tool_call]},
              {"role": "tool", "tool_call_id": "c1", "name": "grep", "content": "hit"}]
    with open(os.path.join(store, sid + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"id": sid, "project": "web", "cwd": os.getcwd(), "updated": 1.0,
                   "messages": stored, "last_answer": ""}, fh, ensure_ascii=False)

    live = sessions.load(sid)["messages"]
    for turn in range(4):
        live.append({"role": "assistant", "content": "turn %d" % turn})
        sessions.checkpoint(sid, live, run_id="r", turn=turn, state="tool_complete")

    after = sessions.load(sid)["messages"]
    assert len(after) == len(live), "%s: the thread repeated itself" % label
    assert [m["content"] for m in after] == [m["content"] for m in live]
    assert sum(1 for m in after if m.get("role") == "tool") == 1


@pytest.mark.parametrize("label,tool_call", [
    # The reader drops what it cannot parse; the writer must not make that drop durable.
    ("unparseable legacy string", "ToolCall(id='c1', name='grep', args=<unset>)"),
    # An unknown key is exactly the field a future/other build would rely on.
    ("unknown tool field", {"id": "c1", "name": "grep", "args": {"pattern": "x"},
                            "unknown_effect": True}),
    # ``_msgs_in`` turns any falsy args into {} — a real value on disk, not a shape.
    ("false args", {"id": "c1", "name": "grep", "args": False}),
    ("zero args", {"id": "c1", "name": "grep", "args": 0}),
    ("arbitrary metadata", {"id": "c1", "name": "grep", "args": {"pattern": "x"},
                            "provenance": {"build": "0.19", "signature": "abc"}}),
])
def test_checkpointing_a_resumed_thread_keeps_the_stored_record_intact(store, label, tool_call):
    """Continuing a thread must not quietly rewrite what was already durable.

    Resuming projects the journal through the reader, and the reader is lossy: an
    element it could not parse is gone, an unknown key on a tool_call is gone, a
    falsy ``args`` has become ``{}``.  Writing that projection back over the
    stored record destroys evidence nobody chose to discard — the merge decides
    that two messages are the same position in one conversation, which is not a
    licence to replace the richer copy with the poorer one.
    """
    sid = "keep-%d" % abs(hash(label))
    stored = [{"role": "user", "content": "go"},
              {"role": "assistant", "content": "working", "tool_calls": [tool_call]},
              {"role": "tool", "tool_call_id": "c1", "name": "grep", "content": "hit"}]
    path = os.path.join(store, sid + ".json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"id": sid, "project": "web", "cwd": os.getcwd(), "updated": 1.0,
                   "messages": stored, "last_answer": ""}, fh, ensure_ascii=False)

    live = sessions.load(sid)["messages"]
    for turn in range(3):
        live.append({"role": "assistant", "content": "turn %d" % turn})
        sessions.checkpoint(sid, live, run_id="r", turn=turn, state="tool_complete")

    with open(path, encoding="utf-8") as fh:
        on_disk = json.load(fh)["messages"]
    assert on_disk[1]["tool_calls"] == [tool_call], \
        "%s: the stored tool_call was rewritten as its lossy projection" % label
    assert on_disk[:3] == stored, "%s: the stored prefix was rewritten" % label
    # and the thread still continued rather than repeating itself
    assert [m["content"] for m in on_disk] == \
        ["go", "working", "hit", "turn 0", "turn 1", "turn 2"]


def test_a_checkpoint_that_cannot_be_serialized_leaves_the_journal_untouched(store):
    """A refusal to write must not damage the progress already recorded."""
    good = [{"role": "user", "content": "the work so far"}]
    sessions.checkpoint("fragile", good, run_id="r", turn=1, state="turn_boundary")

    with pytest.raises(ValueError):
        sessions.checkpoint("fragile", good + [{"role": "tool", "tool_call_id": "c1",
                                                "name": "measure",
                                                "content": {"score": math.nan}}],
                            run_id="r", turn=2, state="tool_complete")

    assert [m["content"] for m in sessions.load("fragile")["messages"]] == ["the work so far"]
    assert sessions.recovery_state("fragile")["turn"] == 1
    assert [n for n in os.listdir(store) if n.endswith(".tmp")] == []


@pytest.mark.parametrize("new_call", [
    {"id": "c1", "name": "grep", "args": {}, "unknown_effect": True},
    {"id": "c1", "name": "grep", "args": {}, "provenance": {"build": "future"}},
    {"id": "c1", "name": "grep", "args": False},
    {"id": "c1", "name": "grep", "args": 0},
])
def test_incoming_tool_information_is_not_discarded_as_an_old_prefix(store, new_call):
    original = {"role": "assistant", "content": "working", "tool_calls": [
        {"id": "c1", "name": "grep", "args": {}}]}
    incoming = dict(original, tool_calls=[new_call])
    sessions.checkpoint("incoming", [original], run_id="r", turn=0, state="turn_boundary")
    sessions.checkpoint("incoming", [incoming], run_id="r", turn=1, state="turn_boundary")
    with open(os.path.join(store, "incoming.json"), encoding="utf-8") as fh:
        messages = json.load(fh)["messages"]
    assert messages == [original, incoming], "new evidence must not be projected away"


# ------------------------------------------------------- surviving a restart


def test_an_unknown_tool_effect_stays_fenced_after_a_restart(store):
    """The process that wrote the fence is gone; the fence is still there."""
    _in_another_process(store, """
from harness import sessions
sessions.checkpoint("fenced", [{"role": "user", "content": "publish the release"}],
                    run_id="r1", turn=4, state="executing_tool",
                    detail={"tool_name": "browser_click", "tool_call_id": "c9"})
print("wrote")
""")
    state = sessions.recovery_state("fenced")
    assert state["recovery_required"] is True
    assert state["auto_resumable"] is False
    assert state["detail"]["tool_call_id"] == "c9"

    # Saving whatever text the interrupted run produced must not retire the fence.
    sessions.save("fenced", [{"role": "user", "content": "publish the release"},
                             {"role": "assistant", "content": "I clicked publish"}],
                  answer="I clicked publish")
    assert sessions.recovery_state("fenced")["recovery_required"] is True

    resumed = sessions.resume_after_interrupt("fenced")
    assert resumed["blocked"] is True
    assert len(resumed["messages"]) == 2


def test_queued_input_survives_a_crash_between_journal_and_ack(store):
    """An accepted instruction reaches the model exactly once, restart or not."""
    task_inbox.enqueue("queued", "typed-1", "also update the changelog", mode="steer")

    # Run A: claims the entry, writes it into the journal, then dies before ack.
    lease = session_owner.acquire("queued", label="executor-a")
    entry = task_inbox.claim("queued", lease, modes=("steer",))[0]
    messages = [{"role": "user", "content": "fix the bug"},
                task_inbox.journal_message(entry)]
    sessions.checkpoint("queued", messages, run_id="rA", turn=1, state="turn_boundary")
    lease.release()          # the crash: no ack ever happened

    assert task_inbox.get("queued", "typed-1")["state"] == "claimed"

    # Run B: a new executor reconciles from what the transcript actually holds.
    with session_owner.own("queued", label="executor-b") as second:
        durable = sessions.load_checked("queued")
        assert durable["status"] == "ok"
        outcome = task_inbox.reconcile(
            "queued", second, task_inbox.delivered_ids(durable["session"]["messages"]))
        assert outcome["consumed"] == ["typed-1"]
        assert task_inbox.claim("queued", second, modes=("steer",)) == []

    final = sessions.load("queued")
    instructions = [m["content"] for m in final["messages"] if m.get("source") == "user"]
    assert instructions == ["also update the changelog"], "delivered once, not twice"
    assert task_inbox.get("queued", "typed-1")["state"] == "consumed"


def test_accepted_attachments_still_pass_their_integrity_check(store):
    """Attachments are digest-checked on load; the write must preserve them exactly."""
    reference = input_assets.save(
        "with-assets",
        images=[{"media_type": "image/png", "data": "iVBORw0KGgo="}],
        contexts=[{"kind": "file", "path": "sr\\c/ünïcode.py",
                   "content": "def f():\n    return '日本語 🐕'\n", "startLine": 1,
                   "endLine": 2}])
    bundle = input_assets.load("with-assets", reference)
    assert bundle["contexts"][0]["content"] == "def f():\n    return '日本語 🐕'\n"
    assert bundle["images"][0]["data"] == "iVBORw0KGgo="
