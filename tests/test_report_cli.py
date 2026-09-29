"""`collie report build` and `collie report preview`, and the three settings behind them.

The CLI is a thin door: build reads, composes, drafts and saves through :mod:`morning_report`
and prints a summary that names sources and counts but no item's words; preview renders the
latest saved report to an HTML file and prints its path without opening anything.
"""
import json
import os

import pytest

from harness import cli
from harness import morning_report as mr
from harness import settings

NOW = 1790690520.0


def canned(**over):
    base = {
        "schema": mr.SCHEMA, "date": "2026-09-29", "generated_at": NOW, "language": "en",
        "timezone": "PDT", "utc_offset_minutes": -420,
        "profile": {"name": "Daming", "companion": "Rowan"}, "weather": {"state": "off"},
        "greeting": "Good morning, Daming!", "headline": "One quick one and you're clear.",
        "summary": "Since yesterday: 1 win.", "things_today": 1,
        "sections": {"wins": {"items": [{"title": "SECRET-WIN-TITLE", "detail": "", "signal_ids": ["a"],
                                         "link": "", "sources": ["github"], "when": None}], "more": 0},
                     "yours": {"items": [{"title": "PRIVATE-BILL", "detail": "", "signal_ids": ["b"],
                                          "link": "", "sources": ["gmail"], "when": None}], "more": 2},
                     "ready": {"items": [], "more": 0}, "projects": {"items": [], "more": 0},
                     "reads": {"items": [], "more": 0}},
        "signals": [], "counters": {},
        "provenance": {"read_at": NOW,
                       "sources": [{"name": "github", "label": "GitHub", "state": "ok", "reason": "",
                                    "detail": "2 GitHub calls", "stats": {"repos": 20}},
                                   {"name": "gmail", "label": "Gmail", "state": "unavailable",
                                    "reason": "Google isn't connected yet", "detail": "", "stats": {}}],
                       "composer": {"mode": "model", "provider": "codex-oauth", "model": "gpt-x",
                                    "error": "", "dropped": [{"section": "wins", "reason": "cites no signal",
                                                              "signal_ids": []}]},
                       "drafts": {"requested": 0, "created": 0, "reused": 0, "failed": 0, "reason": ""}},
    }
    base.update(over)
    return base


@pytest.fixture
def state(tmp_path, monkeypatch):
    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(root))
    return root


@pytest.fixture
def built(monkeypatch):
    calls = []

    def fake_build(**kw):
        calls.append(kw)
        return canned()

    monkeypatch.setattr(mr, "build", fake_build)
    return calls


def test_build_passes_the_flags_and_prints_no_item_words(state, built, capsys):
    assert cli.main(["report", "build", "--no-drafts"]) == 0
    assert built[-1]["drafts"] is False and built[-1]["dry_run"] is False
    assert built[-1]["state_dir"] == str(state)
    out = capsys.readouterr().out
    assert "SECRET-WIN-TITLE" not in out and "PRIVATE-BILL" not in out
    assert "gpt-x" in out and "GitHub: ok" in out
    assert "Gmail: unavailable (Google isn't connected yet)" in out
    assert "1 win" in out and "1 for you (+2 more)" in out and "dropped 1" in out
    assert os.path.join("morning-report", "2026-09-29.json") in out


def test_a_dry_run_says_nothing_was_saved(state, built, capsys):
    assert cli.main(["report", "build", "--dry-run"]) == 0
    assert built[-1]["dry_run"] is True and built[-1]["drafts"] is True
    assert "nothing was saved" in capsys.readouterr().out


def test_build_json_prints_the_whole_report(state, built, capsys):
    assert cli.main(["report", "build", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["headline"] == "One quick one and you're clear."


def test_preview_writes_the_latest_report_as_html_and_opens_nothing(state, tmp_path, capsys, monkeypatch):
    import webbrowser
    from harness import avatar, plat
    monkeypatch.setattr(avatar, "png", lambda *a, **k: b"\x89PNG\r\n\x1a\nfake")

    def never(*args, **kwargs):
        raise AssertionError("preview must not open a browser")

    monkeypatch.setattr(webbrowser, "open", never)
    monkeypatch.setattr(plat, "open_with_default", never)
    mr.write_snapshot(canned(), str(state))
    target = tmp_path / "out" / "preview.html"
    assert cli.main(["report", "preview", "--out", str(target)]) == 0
    assert capsys.readouterr().out.strip() == str(target)
    page = target.read_text(encoding="utf-8")
    assert "Good morning, Daming!" in page and "data:image/png;base64," in page


def test_preview_defaults_next_to_the_snapshots(state, capsys, monkeypatch):
    from harness import avatar
    monkeypatch.setattr(avatar, "png", lambda *a, **k: b"")
    mr.write_snapshot(canned(), str(state))
    assert cli.main(["report", "preview"]) == 0
    path = capsys.readouterr().out.strip()
    assert path == os.path.join(str(state), "morning-report", "preview.html") and os.path.isfile(path)


def test_preview_without_a_report_says_how_to_make_one(state, capsys):
    assert cli.main(["report", "preview"]) == 1
    assert "collie report build" in capsys.readouterr().err


def test_the_report_settings_exist_with_safe_defaults():
    rows = {row["key"]: row for row in settings.SCHEMA if row["key"].startswith("REPORT_")}
    assert set(rows) == {"REPORT_NAME", "REPORT_PROJECT_ROOTS", "REPORT_GMAIL_DRAFTS",
                         "REPORT_MUTED"}
    assert rows["REPORT_MUTED"]["default"] == "" and "mute" in rows["REPORT_MUTED"]["hint"]
    assert rows["REPORT_GMAIL_DRAFTS"]["type"] == "bool" and rows["REPORT_GMAIL_DRAFTS"]["default"] == "on"
    assert rows["REPORT_NAME"]["default"] == "" and rows["REPORT_PROJECT_ROOTS"]["default"] == ""
    assert all(row.get("label_zh") and row.get("hint_zh") for row in rows.values())
    assert "never sends" in rows["REPORT_GMAIL_DRAFTS"]["hint"]
    assert settings.GROUPS_ZH.get(rows["REPORT_NAME"]["group"])


def test_project_roots_setting_reaches_the_local_scan(monkeypatch, tmp_path):
    import os as _os
    monkeypatch.setenv("COLLIE_REPORT_PROJECT_ROOTS", _os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")]))
    assert mr._options() == {"roots": [str(tmp_path / "a"), str(tmp_path / "b")]}
    monkeypatch.setenv("COLLIE_REPORT_PROJECT_ROOTS", "")
    assert mr._options() == {}


def test_the_name_setting_greets_the_person(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_REPORT_NAME", "Daming")
    monkeypatch.setenv("COLLIE_LANG", "zh")
    profile = mr.load_profile(str(tmp_path))
    assert profile["name"] == "Daming" and profile["language"] == "zh"
    monkeypatch.setenv("COLLIE_REPORT_NAME", "<script>")
    assert mr.load_profile(str(tmp_path))["name"] == ""
