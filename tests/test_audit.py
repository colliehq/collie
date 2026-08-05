"""The audit trail — built to answer one question: why was I not asked about that?"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from harness.audit import AuditLog, sanitize


@pytest.fixture()
def log(tmp_path):
    a = AuditLog(str(tmp_path / "audit.db"))
    yield a
    a.close()


def test_records_and_reads_back(log):
    log.record(tool="browser_click", risk="external", stage="approved",
               outcome="allow_once", reason="answered by the user",
               target="https://x.test", args={"ref": "e1"})
    rows = log.list()
    assert len(rows) == 1
    assert rows[0]["tool"] == "browser_click" and rows[0]["args"] == {"ref": "e1"}


def test_every_silent_call_can_say_why(log):
    """The invariant. A row that ran without a prompt and cannot explain itself is the one
    row anybody would actually need."""
    log.record(tool="write_file", risk="write_local", stage="auto", outcome="allowed",
               reason="project mode: writes inside /repo")
    log.record(tool="browser_click", risk="external", stage="auto", outcome="allowed",
               rule="browser_click → http://localhost:5173", reason="allowed by rule")
    assert log.unexplained() == []


def test_unexplained_catches_a_reasonless_auto_allow(log):
    log.record(tool="browser_click", risk="external", stage="auto", outcome="allowed")
    assert [r["tool"] for r in log.unexplained()] == ["browser_click"]


def test_filters(log):
    log.record(tool="bash", stage="auto", reason="x")
    log.record(tool="browser_click", stage="denied", reason="y")
    assert [r["tool"] for r in log.list(stage="denied")] == ["browser_click"]
    assert [r["tool"] for r in log.list(tool="bash")] == ["bash"]


def test_a_broken_write_never_reaches_the_run(log):
    log.close()                      # the db is gone; record must still be harmless
    log.record(tool="bash", stage="auto", reason="x")


def test_survives_a_reopen(tmp_path):
    p = str(tmp_path / "a.db")
    a = AuditLog(p)
    a.record(tool="bash", stage="auto", reason="x")
    a.close()
    b = AuditLog(p)
    try:
        assert len(b.list()) == 1
    finally:
        b.close()


# -- sanitising -------------------------------------------------------------
@pytest.mark.parametrize("key", ["token", "api_key", "PASSWORD", "access_token",
                                 "bot_token", "auth", "cookie", "credential"])
def test_credential_shaped_keys_are_dropped(key):
    assert sanitize("x", {key: "sk-live-REAL"})[key] == "[redacted]"


def test_typed_input_keeps_the_length_not_the_value():
    """A keystroke tool's audit row should say something was typed and roughly how much.
    What was typed is the user's, and an audit db is exactly the file people mail around
    when something has gone wrong."""
    out = sanitize("browser_type", {"text": "my bank password"})
    assert out["text"] == "[16 chars]"


def test_message_bodies_keep_the_length_not_the_value():
    assert sanitize("x", {"body": "dear bob, "})["body"] == "[10 chars]"
    assert sanitize("x", {"message_body": "hello"})["message_body"] == "[5 chars]"


def test_ordinary_args_survive_so_the_row_is_useful():
    out = sanitize("browser_click", {"ref": "e1", "selector": "#send"})
    assert out == {"ref": "e1", "selector": "#send"}


def test_long_values_are_shortened():
    assert len(sanitize("x", {"path": "p" * 5000})["path"]) <= 200


def test_placeholders_are_kept_as_placeholders():
    """The loop hands the gate PRE-restore args, so a secret arrives already masked. The
    audit must not undo that, and must not need to."""
    assert sanitize("bash", {"command": "curl -H {{SECRET:deadbeef}} x"})["command"] \
        == "curl -H {{SECRET:deadbeef}} x"


def test_non_dict_args_are_not_an_error():
    assert sanitize("x", None) == {}
    assert sanitize("x", "nope") == {}


# -- through the loop -------------------------------------------------------
def test_the_loop_records_what_it_let_through(tmp_path):
    """End to end: a project-mode write runs with no prompt, and the log can say why."""
    from _util import _ScriptProvider
    from harness.cli import make_harness
    from harness.gate import Gate
    from harness.providers import Completion, ToolCall

    log = AuditLog(str(tmp_path / "a.db"))
    h = make_harness(str(tmp_path), provider="mock", project="audit", embed="hash",
                     gate=Gate(cwd=tmp_path))
    h.audit = log
    h.max_turns = 3
    h.provider = _ScriptProvider([
        Completion(text="", tool_calls=[ToolCall("c1", "write_file",
                                                 {"path": "a.py", "content": "x"}),
                                        ToolCall("c2", "browser_click", {"ref": "e1"})]),
        Completion(text="done", stop_reason="end_turn")])
    h.run("audit", "do it", consolidate=False)

    rows = {r["tool"]: r for r in log.list()}
    assert rows["write_file"]["stage"] == "auto"
    assert "project mode" in rows["write_file"]["reason"]
    assert rows["browser_click"]["stage"] == "denied"
    assert log.unexplained() == []
    log.close()


def test_reads_are_not_recorded(tmp_path):
    """A read has no side effect to account for, and burying the log in them is how an
    audit trail stops being read."""
    from _util import _ScriptProvider
    from harness.cli import make_harness
    from harness.gate import Gate
    from harness.providers import Completion, ToolCall

    log = AuditLog(str(tmp_path / "a.db"))
    (tmp_path / "a.py").write_text("x = 1", encoding="utf-8")
    h = make_harness(str(tmp_path), provider="mock", project="audit", embed="hash",
                     gate=Gate(cwd=tmp_path))
    h.audit = log
    h.max_turns = 3
    h.provider = _ScriptProvider([
        Completion(text="", tool_calls=[ToolCall("c1", "read_file", {"path": "a.py"})]),
        Completion(text="done", stop_reason="end_turn")])
    h.run("audit", "read it", consolidate=False)
    assert log.list() == []
    log.close()
