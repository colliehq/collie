import json
import os
import time
from pathlib import Path

import pytest


def _meeting(root: Path, *, status="recording", started=None, segments=None):
    started = int(started or time.time() * 1000)
    meeting_id = "%013d-123-1" % started
    folder = root / "meetings" / meeting_id
    folder.mkdir(parents=True)
    rows = segments or [
        {"id": 1, "start_ms": 100, "end_ms": 800, "speaker_id": "speaker-1",
         "source": "system", "text": "Design a URL shortener for one billion redirects."},
        {"id": 2, "start_ms": 900, "end_ms": 1600, "speaker_id": "you",
         "source": "microphone", "text": "I would clarify read and write traffic first."},
    ]
    meta = {"schema_version": 1, "id": meeting_id, "title": "System design",
            "created_at_ms": started, "updated_at_ms": int(time.time() * 1000),
            "started_at_ms": started, "ended_at_ms": None,
            "duration_ms": 1600, "segment_count": len(rows), "status": status,
            "source": {"kind": "live", "microphone": True, "system_audio": True},
            "language": "en", "audio_retention": "delete_after_transcription",
            "speakers": [{"id": "you", "label": "You", "source": "microphone"},
                         {"id": "speaker-1", "label": "Interviewer", "source": "system"}],
            "bookmarks": [], "summary": None, "error": None}
    (folder / "meeting.json").write_text(json.dumps(meta), encoding="utf-8")
    (folder / "transcript.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return meeting_id


def test_board_profiles_cover_mainstream_services():
    from harness.interview_assist import detect_board, public_board_profiles

    assert detect_board("https://miro.com/app/board/uX")['id'] == "miro"
    assert detect_board("https://www.figma.com/board/abc/FigJam")['id'] == "figjam"
    assert detect_board("https://app.eraser.io/workspace/abc")['mode'] == "mcp"
    assert detect_board("https://app.diagrams.net/")['id'] == "diagrams-net"
    assert detect_board("https://unknown.example/board")['id'] == "generic"
    ids = {row["id"] for row in public_board_profiles()}
    assert {"miro", "figjam", "lucid", "excalidraw", "tldraw", "eraser",
            "whimsical", "microsoft-whiteboard", "canva", "diagrams-net"} <= ids


def test_vocalcode_bridge_reads_only_current_bounded_transcript(monkeypatch, tmp_path):
    from harness import interview_assist as ia

    vocal = tmp_path / "VocalCode"
    old = int(time.time() * 1000) - 900_000
    _meeting(vocal, status="completed", started=old)
    current = _meeting(vocal, started=int(time.time() * 1000))
    monkeypatch.setenv("VOCALCODE_DATA_DIR", str(vocal))
    monkeypatch.setattr(ia, "_vocalcode_executable", lambda: "C:/fake/VocalCode.exe")

    snap = ia.vocalcode_snapshot(int(time.time() * 1000) - 10_000)
    assert snap["recording"] is True and snap["meeting"]["id"] == current
    assert [row["speaker"] for row in snap["segments"]] == ["Interviewer", "You"]
    assert all("audio" not in row for row in snap["segments"])

    # Starting Collie after an already-running VocalCode meeting is still a current live session,
    # but the transcript file is not opened when sharing is disabled.
    earlier_vocal = tmp_path / "VocalCode-earlier"
    earlier = int(time.time() * 1000) - 900_000
    live = _meeting(earlier_vocal, started=earlier)
    monkeypatch.setenv("VOCALCODE_DATA_DIR", str(earlier_vocal))
    monkeypatch.setattr(ia, "_segments", lambda *_args, **_kwargs: pytest.fail(
        "transcript parser must not run while sharing is disabled"))
    snap = ia.vocalcode_snapshot(int(time.time() * 1000), include_segments=False)
    assert snap["meeting"]["id"] == live and "segments" in snap and snap["segments"] == []


def test_interview_session_consent_context_and_authority_clear(monkeypatch, tmp_path):
    from harness import interview_assist as ia

    vocal = tmp_path / "VocalCode"
    _meeting(vocal)
    monkeypatch.setenv("VOCALCODE_DATA_DIR", str(vocal))
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "collie"))
    monkeypatch.setattr(ia, "_vocalcode_executable", lambda: "C:/fake/VocalCode.exe")
    store = ia.InterviewStore(tmp_path / "collie")

    with pytest.raises(ia.InterviewError, match="consent"):
        store.start(consent=False)
    started = store.start(consent=True, share_transcript=True, board_edit=True)
    assert started["active"] and started["share_transcript"] and started["board_edit"]
    assert started["consent_version"] == "interview-assist-v1" and started["consent_at_ms"]
    context = ia.model_context()
    assert "LIVE VOCALCODE TRANSCRIPT" in context and "URL shortener" in context
    assert "untrusted conversation data" in context
    stopped = store.stop()
    assert not stopped["active"] and not stopped["share_transcript"] and not stopped["board_edit"]
    assert ia.model_context() == ""


def test_transcript_is_not_exposed_to_the_tool_when_session_sharing_is_off(
        monkeypatch, tmp_path):
    from harness import interview_assist as ia

    vocal = tmp_path / "VocalCode"
    _meeting(vocal)
    monkeypatch.setenv("VOCALCODE_DATA_DIR", str(vocal))
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path / "collie"))
    monkeypatch.setattr(ia, "_vocalcode_executable", lambda: "C:/fake/VocalCode.exe")
    store = ia.InterviewStore(tmp_path / "collie")
    store.start(consent=True, share_transcript=False)

    snap = store.snapshot(include_transcript=True)
    assert "segments" not in snap["vocalcode"]
    output = ia.InterviewAssistTool().run({"action": "status"}, None)
    assert "URL shortener" not in output and "segments" not in json.loads(output)["vocalcode"]


def test_diagram_preview_is_bounded_and_browser_apply_uses_shortcuts(monkeypatch, tmp_path):
    from harness import browserbridge
    from harness import interview_assist as ia

    store = ia.InterviewStore(tmp_path)
    store.start(consent=True, board_edit=True)
    state = store._read()
    state["board"] = {"url": "https://miro.com/app/board/test", "title": "Architecture",
                      "service": "miro", "service_name": "Miro", "mode": "shortcut", "tab_id": 3}
    store._write(state)
    plan = store.preview_diagram(
        [{"id": "client", "label": "Client", "x": .2, "y": .4},
         {"id": "api", "label": "API Gateway", "x": .65, "y": .4}],
        [{"from": "client", "to": "api", "label": "HTTPS"}])

    calls = []
    monkeypatch.setattr(browserbridge, "_bridge_live", lambda: True)

    def call(cmd, timeout=60):
        calls.append(dict(cmd))
        if cmd["action"] == "spaces":
            return {"ok": True, "data": {"spaces": [{"space": ia.BOARD_SPACE, "tab_id": 3,
                    "title": "Architecture", "url": "https://miro.com/app/board/test"}]}}
        if cmd["action"] == "screenshot":
            return {"ok": True, "data": {"css_width": 1200, "css_height": 800}}
        return {"ok": True, "data": {"inserted": True}}

    monkeypatch.setattr(browserbridge, "_call", call)
    result = store.apply_diagram(plan["id"])
    assert result["ok"] and result["nodes"] == 2 and result["edges"] == 1
    assert sum(row["action"] == "insert_text" for row in calls) == 2
    assert any(row["action"] == "press" and row.get("key") == "l" for row in calls)
    assert store._read()["pending_diagram"] is None


def test_diagram_apply_refuses_without_session_board_authority(tmp_path):
    from harness import interview_assist as ia

    store = ia.InterviewStore(tmp_path)
    store.start(consent=True, board_edit=False)
    state = store._read()
    state["board"] = {"url": "https://miro.com/app/board/test", "title": "Architecture"}
    store._write(state)
    plan = store.preview_diagram([{"id": "api", "label": "API"}], [])
    with pytest.raises(ia.InterviewError, match="not allowed"):
        store.apply_diagram(plan["id"])


def test_diagram_apply_stops_when_session_authority_is_revoked(monkeypatch, tmp_path):
    from harness import browserbridge
    from harness import interview_assist as ia

    store = ia.InterviewStore(tmp_path)
    store.start(consent=True, board_edit=True)
    state = store._read()
    state["board"] = {"url": "https://miro.com/app/board/test", "title": "Architecture",
                      "service": "miro", "service_name": "Miro", "mode": "shortcut", "tab_id": 3}
    store._write(state)
    plan = store.preview_diagram([{"id": "api", "label": "API"}], [])
    monkeypatch.setattr(browserbridge, "_bridge_live", lambda: True)

    def call(cmd, timeout=60):
        if cmd["action"] == "spaces":
            return {"ok": True, "data": {"spaces": [{"space": ia.BOARD_SPACE, "tab_id": 3,
                    "title": "Architecture", "url": "https://miro.com/app/board/test"}]}}
        if cmd["action"] == "screenshot":
            store.update_permissions(board_edit=False)
            return {"ok": True, "data": {"css_width": 1200, "css_height": 800}}
        pytest.fail("no drawing action is allowed after board authority is revoked")

    monkeypatch.setattr(browserbridge, "_call", call)
    with pytest.raises(ia.InterviewError, match="authority changed"):
        store.apply_diagram(plan["id"])


def test_default_registry_exposes_interview_tool(monkeypatch):
    from harness.tools import default_registry

    monkeypatch.setenv("COLLIE_BROWSER_BRIDGE", "0")
    registry = default_registry()
    assert "interview_assist" in registry.names()
