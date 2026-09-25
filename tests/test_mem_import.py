"""Importing past Claude Code / Codex sessions into memory, without a model (--no-llm).

The module had 10% line coverage: its parsers, chunking and the import driver ran only by hand.
What must hold: harness noise never becomes a fact, secrets are redacted before they are stored,
every fact carries provenance that says where it came from, a session is imported once, and a
session still being written is left alone.
"""
import json
import os
import time

import pytest

from harness import mem_import as mi
from harness.memory import SqliteMemory

SECRET = "sk-ant-api03-" + "Q" * 40


@pytest.fixture
def roots(tmp_path, monkeypatch):
    cc = tmp_path / "home" / ".claude" / "projects"
    codex = tmp_path / "home" / ".codex" / "sessions"
    cc.mkdir(parents=True)
    codex.mkdir(parents=True)
    monkeypatch.setattr(mi, "CC_ROOT", cc)
    monkeypatch.setattr(mi, "CODEX_ROOT", codex)
    monkeypatch.setattr(mi, "STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(mi, "FAMILY_REG_PATH", tmp_path / "families.json")
    return cc, codex


def _jsonl(path, rows, *, age_s=3600):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write((row if isinstance(row, str) else json.dumps(row)) + "\n")
    old = time.time() - age_s
    os.utime(path, (old, old))
    return path


def _cc_session(cc, name="abcdef1234567890", *, age_s=3600, extra=()):
    padding = "context " * 300                       # past MIN_SESSION_BYTES
    rows = [
        {"type": "ai-title", "aiTitle": "Deploy the relay"},
        {"type": "user", "message": {"content": "<system-reminder>ignore me</system-reminder>"}},
        {"type": "user", "message": {"content": "Deploy the relay; the key is %s. %s"
                                                % (SECRET, padding)}},
        {"type": "assistant", "isSidechain": True, "message": {"content": "sidechain chatter"}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Checking the config."},
            {"type": "tool_result", "is_error": False, "content": "a huge ls listing"},
            {"type": "tool_result", "is_error": True, "content": "port 8787 already in use"},
        ]}},
        "not json at all",
        {"type": "assistant", "message": {"content": "Deployed on port 8799 instead."}},
        *extra,
    ]
    return _jsonl(cc / "proj" / (name + ".jsonl"), rows, age_s=age_s)


def test_a_claude_code_session_keeps_the_conversation_and_drops_harness_noise(roots):
    sess = mi.parse_cc_session(_cc_session(roots[0], extra=[
        {"type": "attachment", "attachment": {"type": "max_turns_reached"}}]))
    assert sess["title"] == "Deploy the relay"
    texts = [t for _r, t in sess["turns"]]
    assert not any("ignore me" in t or "sidechain" in t or "huge ls" in t for t in texts)
    assert any("[tool-error] port 8787 already in use" in t for t in texts)
    assert sess["turns"][-1] == ("user", "[run-outcome] max_turns_reached — turn budget exhausted")
    assert mi.parse_cc_session(_jsonl(roots[0] / "p" / "empty.jsonl", ["{}", "nope"])) is None


def test_a_codex_rollout_reads_its_own_session_id(roots):
    path = _jsonl(roots[1] / "2026" / "rollout-x.jsonl", [
        {"type": "session_meta", "payload": {"session_id": "codex-sid-42"}},
        {"type": "response_item", "payload": {"type": "message", "role": "user",
                                              "content": [{"type": "input_text", "text": "hi"}]}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "shell"}},
        {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                              "content": [{"type": "output_text", "text": "ok"}]}},
    ])
    sess = mi.parse_codex_session(path)
    assert sess["sid"] == "codex-sid-42"
    assert sess["turns"] == [("user", "hi"), ("assistant", "ok")]


def test_chunks_keep_order_and_a_giant_keeps_its_first_and_last(monkeypatch):
    turns = [("user", "u%d " % i + "x" * 2000) for i in range(40)]
    chunks = mi.chunk_turns(turns, max_chunks=0)
    assert all(len(c) <= mi.MAX_CHUNK_CHARS for c in chunks)
    assert chunks[0].startswith("U: u0 ") and "U: u39 " in chunks[-1]
    sampled = mi.chunk_turns(turns, max_chunks=4)
    assert len(sampled) == 4 and sampled[0] == chunks[0] and sampled[-1] == chunks[-1]
    assert len(mi.chunk_turns([("user", "y" * 5000)], 0)[0]) == len("U: ") + mi.MAX_USER_CHARS


def test_import_stores_redacted_facts_with_their_source_once_and_purge_reverts(roots, tmp_path):
    _cc_session(roots[0])
    mem = SqliteMemory(str(tmp_path / "mem.db"))
    try:
        stats = mi.run_import(mem, source="all", no_llm=True, log=lambda *_: None)
        assert stats["sessions"] == 1 and stats["facts"] >= 1
        rows = mem.db.execute("SELECT text, keys FROM facts WHERE keys LIKE 'import %'").fetchall()
        texts = " ".join(r[0] for r in rows)
        # The fact that mentioned the key is redacted, and then refused by the memory store, which
        # keeps no fact carrying a secret, not even as a placeholder; the outcome fact is kept.
        assert stats["redacted"] >= 1
        assert SECRET not in texts and "{{SECRET:" not in texts
        assert "Deployed on port 8799" in texts
        # provenance: which agent's history, which session
        assert all(r[1].startswith("import src:cc sid:abcdef12") for r in rows), rows

        again = mi.run_import(mem, source="all", no_llm=True, log=lambda *_: None)
        assert again["sessions"] == 0 and again["skipped"] == 1, "a session is imported once"

        assert mi.purge(mem) == len(rows)
        assert mem.db.execute("SELECT COUNT(*) FROM facts WHERE keys LIKE 'import %'"
                              ).fetchone()[0] == 0
    finally:
        mem.close()


def test_a_session_still_being_written_is_left_for_later(roots, tmp_path):
    _cc_session(roots[0], age_s=5)                   # a live agent is writing it right now
    mem = SqliteMemory(str(tmp_path / "mem.db"))
    try:
        stats = mi.run_import(mem, source="cc", no_llm=True, log=lambda *_: None)
        assert stats["scanned"] == 1 and stats["sessions"] == 0
        assert not mi.STATE_PATH.exists() or str(next(roots[0].rglob("*.jsonl"))) not in \
            json.loads(mi.STATE_PATH.read_text())
    finally:
        mem.close()
