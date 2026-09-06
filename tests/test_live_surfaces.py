import pytest


def test_work_surface_detection_is_host_bounded_and_queries_are_not_displayed():
    from harness.live_surfaces import detect_board, safe_board_url, safe_display_url

    assert detect_board("https://miro.com/app/board/abc")['id'] == "miro"
    assert detect_board("https://evilmiro.com/app/board/abc")['id'] == "generic"
    assert detect_board("https://figma.com/design/abc", "Design")['name'] == "Figma / FigJam"
    assert safe_board_url("https://tldraw.com/r/abc")
    assert not safe_board_url("http://tldraw.com/r/abc")
    assert safe_display_url("https://miro.com/app/board/abc?secret=1#x") == \
           "https://miro.com/app/board/abc"


def test_provider_neutral_diagram_is_strictly_bounded():
    from harness.live_surfaces import SurfaceError, validate_diagram

    value = validate_diagram(
        [{"id": "client", "label": "Client"}, {"id": "api", "label": "API"}],
        [{"from": "client", "to": "api", "label": "HTTPS"}])
    assert len(value["nodes"]) == 2 and value["edges"][0]["from"] == "client"
    with pytest.raises(SurfaceError, match="unique safe ids"):
        validate_diagram([{"id": "bad id", "label": "Bad"}], [])
    with pytest.raises(SurfaceError, match="different known"):
        validate_diagram([{"id": "api", "label": "API"}],
                         [{"from": "api", "to": "api"}])


def test_shortcut_writer_types_each_canvas_label_character_by_character(monkeypatch):
    from harness import browserbridge, live_surfaces

    calls = []
    monkeypatch.setattr(browserbridge, "_bridge_live", lambda: True)
    monkeypatch.setattr(live_surfaces.time, "sleep", lambda _seconds: None)

    def call(command, timeout=60):
        calls.append(dict(command))
        if command["action"] == "spaces":
            return {"ok": True, "data": {"spaces": [{
                "space": live_surfaces.BOARD_SPACE, "tab_id": 7,
                "title": "System design", "url": "https://excalidraw.com/",
            }]}}
        if command["action"] == "screenshot":
            return {"ok": True, "data": {"css_width": 1200, "css_height": 800}}
        return {"ok": True, "data": {}}

    monkeypatch.setattr(browserbridge, "_call", call)
    profile = live_surfaces.detect_board("https://excalidraw.com/", "System design")
    plan = live_surfaces.validate_diagram([{"id": "api", "label": "API"}], [])

    result = live_surfaces.draw_with_shortcuts(
        profile, plan, expected_tab_id=7, expected_url="https://excalidraw.com/")

    typed = [row["text"] for row in calls if row.get("action") == "insert_text"]
    assert typed == ["A", "P", "I"]
    assert result["input_mode"] == "character_by_character"
    assert result["typed_characters"] == 3


def test_live_diagram_apply_stops_when_session_authority_is_revoked(monkeypatch, tmp_path):
    from harness import browserbridge
    from harness.live_copilot import LiveCopilotError, LiveSessionStore
    from harness.live_surfaces import BOARD_SPACE

    store = LiveSessionStore(tmp_path)
    store.start(listen=False, consent=False, board_edit=True, observe_apps=False)
    state = store._read()
    state["board"] = {"url": "https://miro.com/app/board/test", "title": "Architecture",
                      "service": "miro", "service_name": "Miro", "mode": "shortcut", "tab_id": 3}
    store._write(state)
    plan = store.preview_diagram([{"id": "api", "label": "API"}], [])
    monkeypatch.setattr(browserbridge, "_bridge_live", lambda: True)

    def call(command, timeout=60):
        if command["action"] == "spaces":
            return {"ok": True, "data": {"spaces": [{"space": BOARD_SPACE, "tab_id": 3,
                    "title": "Architecture", "url": "https://miro.com/app/board/test"}]}}
        if command["action"] == "screenshot":
            store.update_permissions(board_edit=False)
            return {"ok": True, "data": {"css_width": 1200, "css_height": 800}}
        pytest.fail("no drawing action is allowed after authority is revoked")

    monkeypatch.setattr(browserbridge, "_call", call)
    with pytest.raises(LiveCopilotError, match="authority changed"):
        store.apply_diagram(plan["id"])
