"""Optional provider-neutral work surfaces for Live Copilot.

Surfaces are deliberately secondary. They expose one visible, attached browser tab and a bounded
diagram contract; they do not define the Live session and never grant background authority.
"""
from __future__ import annotations

import re
import time
import urllib.parse


MAX_NODES = 24
MAX_EDGES = 48
BOARD_SPACE = "live-work-surface"
# Text inserted into a canvas should arrive as individual input events.  This keeps the
# visible collaboration behaviour close to a person typing, avoids clipboard-style bulk
# insertion, and lets the authority check interrupt a long label promptly.
MANUAL_TYPING_DELAY_S = 0.018
_NODE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")


class SurfaceError(RuntimeError):
    pass


def _text(value, limit=500):
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


BOARD_PROFILES = (
    {"id": "miro", "name": "Miro", "hosts": ("miro.com",), "mode": "shortcut",
     "shape_key": "r", "edge_key": "l", "integration": "REST API / Web SDK"},
    {"id": "figjam", "name": "FigJam", "hosts": ("figma.com",), "mode": "shortcut",
     "shape_key": "r", "edge_key": "l", "integration": "FigJam Plugin API"},
    {"id": "excalidraw", "name": "Excalidraw", "hosts": ("excalidraw.com",),
     "mode": "shortcut", "shape_key": "r", "edge_key": "a",
     "integration": "browser canvas"},
    {"id": "tldraw", "name": "tldraw", "hosts": ("tldraw.com",), "mode": "shortcut",
     "shape_key": "r", "edge_key": "a", "integration": "tldraw Editor SDK"},
    {"id": "eraser", "name": "Eraser", "hosts": ("eraser.io",), "mode": "mcp",
     "integration": "official Eraser MCP", "mcp": "eraser"},
    {"id": "lucid", "name": "Lucidchart / Lucidspark", "hosts": ("lucid.app", "lucid.co"),
     "mode": "guided", "integration": "Lucid Extension API / REST API"},
    {"id": "whimsical", "name": "Whimsical", "hosts": ("whimsical.com",),
     "mode": "guided", "integration": "browser canvas"},
    {"id": "microsoft-whiteboard", "name": "Microsoft Whiteboard",
     "hosts": ("whiteboard.office.com", "whiteboard.microsoft.com"), "mode": "guided",
     "integration": "browser canvas"},
    {"id": "canva", "name": "Canva Whiteboards", "hosts": ("canva.com",),
     "mode": "guided", "integration": "browser canvas"},
    {"id": "diagrams-net", "name": "diagrams.net",
     "hosts": ("app.diagrams.net", "draw.io"), "mode": "guided",
     "integration": "browser canvas"},
    {"id": "coderpad", "name": "CoderPad", "hosts": ("coderpad.io",),
     "mode": "guided", "integration": "collaborative canvas"},
    {"id": "hackerrank", "name": "HackerRank", "hosts": ("hackerrank.com",),
     "mode": "guided", "integration": "collaborative canvas"},
)


def detect_board(url="", title=""):
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        host = (parsed.hostname or "").casefold().rstrip(".")
    except ValueError:
        host = ""
    folded_title = str(title or "").casefold()
    for profile in BOARD_PROFILES:
        if any(host == item or host.endswith("." + item) for item in profile["hosts"]):
            value = dict(profile)
            if profile["id"] == "figjam" and "figjam" not in (str(url).casefold() + folded_title):
                value["name"] = "Figma / FigJam"
            return value
    return {"id": "generic", "name": "Web work surface", "hosts": (), "mode": "guided",
            "integration": "browser accessibility and pointer controls"}


def public_board_profiles():
    return [{k: v for k, v in row.items() if k not in ("hosts", "shape_key", "edge_key")}
            for row in BOARD_PROFILES]


def safe_board_url(url):
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
    except ValueError:
        return False
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password


def safe_display_url(url):
    try:
        parsed = urllib.parse.urlsplit(str(url or ""))
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:2_000]
    except ValueError:
        return ""


def validate_diagram(nodes, edges):
    if not isinstance(nodes, list) or not (1 <= len(nodes) <= MAX_NODES):
        raise SurfaceError("a diagram needs 1 to %d nodes" % MAX_NODES)
    if not isinstance(edges, list) or len(edges) > MAX_EDGES:
        raise SurfaceError("a diagram supports at most %d edges per batch" % MAX_EDGES)
    clean_nodes, ids = [], set()
    for index, raw in enumerate(nodes):
        if not isinstance(raw, dict):
            raise SurfaceError("each diagram node must be an object")
        node_id, label = str(raw.get("id") or ""), _text(raw.get("label"), 160)
        if not _NODE_ID.fullmatch(node_id) or node_id in ids or not label:
            raise SurfaceError("diagram nodes need unique safe ids and non-empty labels")
        ids.add(node_id)
        try:
            x = float(raw.get("x", .15 + .34 * (index % 3)))
            y = float(raw.get("y", .16 + .22 * (index // 3)))
            width, height = float(raw.get("width", .18)), float(raw.get("height", .10))
        except (TypeError, ValueError):
            raise SurfaceError("node positions and sizes must be numbers")
        if not (0 <= x <= 1 and 0 <= y <= 1 and .05 <= width <= .35 and .04 <= height <= .24):
            raise SurfaceError("node coordinates must be normalized and sizes must stay bounded")
        clean_nodes.append({"id": node_id, "label": label,
                            "kind": _text(raw.get("kind"), 40) or "service",
                            "x": round(x, 4), "y": round(y, 4),
                            "width": round(width, 4), "height": round(height, 4)})
    clean_edges = []
    for raw in edges:
        if not isinstance(raw, dict):
            raise SurfaceError("each diagram edge must be an object")
        source, target = str(raw.get("from") or ""), str(raw.get("to") or "")
        if source not in ids or target not in ids or source == target:
            raise SurfaceError("every edge must connect two different known node ids")
        clean_edges.append({"from": source, "to": target, "label": _text(raw.get("label"), 120)})
    return {"nodes": clean_nodes, "edges": clean_edges}


def _bridge_data(response):
    data = response.get("data", response) if isinstance(response, dict) else {}
    if not isinstance(data, dict):
        return {}
    error = data.get("error")
    if not error and isinstance(data.get("click"), dict):
        error = data["click"].get("error")
    if error:
        raise SurfaceError("work-surface browser action failed: %s" % error)
    return data


def _type_text_character_by_character(text, *, authority=None):
    """Write focused canvas text through one trusted input action per character.

    Whiteboard editors generally do not offer a stable DOM input to target after a shape has
    been drawn.  Keeping the text path focus-relative also means a user revoking Live-session
    editing authority can stop the writer between individual characters.
    """
    from . import browserbridge as bb

    characters = list(str(text or ""))
    if not characters:
        return 0
    for index, character in enumerate(characters):
        if authority:
            authority()
        _bridge_data(bb._call({"action": "insert_text", "text": character}))
        if index + 1 < len(characters):
            time.sleep(MANUAL_TYPING_DELAY_S)
    return len(characters)


def draw_with_shortcuts(profile, plan, *, authority=None, expected_tab_id=None,
                        expected_url=""):
    from . import browserbridge as bb
    if not bb._bridge_live():
        raise SurfaceError("Collie Browser Bridge disconnected before the diagram was applied")
    with bb.browser_space(BOARD_SPACE):
        if authority:
            authority()
        identity = bb.space_identity(BOARD_SPACE)
        current = detect_board(identity.get("url"), identity.get("title"))
        if ((expected_tab_id is not None and identity.get("tab_id") != expected_tab_id) or
                (expected_url and safe_display_url(identity.get("url")) != expected_url) or
                current["id"] != profile["id"]):
            raise SurfaceError("the attached tab changed; attach the intended work surface again")
        shot = _bridge_data(bb._call({"action": "screenshot", "full_page": False,
                                     "max_dim": 1000}))
        width, height = max(720, int(shot.get("css_width") or 1280)), \
                        max(520, int(shot.get("css_height") or 720))
        left, top, usable_w, usable_h = 120, 100, max(400, width - 210), max(300, height - 170)
        points, created, typed_characters = {}, 0, 0
        for node in plan["nodes"]:
            if authority:
                authority()
            cx, cy = left + node["x"] * usable_w, top + node["y"] * usable_h
            nw, nh = node["width"] * usable_w, node["height"] * usable_h
            points[node["id"]] = (cx, cy)
            _bridge_data(bb._call({"action": "press", "key": profile["shape_key"]}))
            _bridge_data(bb._call({"action": "drag", "from": {"x": cx - nw / 2, "y": cy - nh / 2},
                                  "to": {"x": cx + nw / 2, "y": cy + nh / 2}, "steps": 8}))
            _bridge_data(bb._call({"action": "press", "key": "Enter"}))
            typed_characters += _type_text_character_by_character(
                node["label"], authority=authority)
            _bridge_data(bb._call({"action": "press", "key": "Escape"}))
            created += 1
        for edge in plan["edges"]:
            if authority:
                authority()
            a, b = points[edge["from"]], points[edge["to"]]
            _bridge_data(bb._call({"action": "press", "key": profile["edge_key"]}))
            _bridge_data(bb._call({"action": "drag", "from": {"x": a[0], "y": a[1]},
                                  "to": {"x": b[0], "y": b[1]}, "steps": 10}))
            _bridge_data(bb._call({"action": "press", "key": "Escape"}))
            created += 1
        if authority:
            authority()
        _bridge_data(bb._call({"action": "press", "key": "v"}))
    return {"ok": True, "service": profile["id"], "nodes": len(plan["nodes"]),
            "edges": len(plan["edges"]), "actions": created,
            "typed_characters": typed_characters, "input_mode": "character_by_character",
            "verification": "Inspect the visible surface; use its Undo if placement is wrong."}
