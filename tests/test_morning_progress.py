"""The morning scene's progress: what the person marks done, and what resolves by itself.

Done marks live beside the snapshot in <date>.state.json. The resolve pass re-checks three
cheap facts (a reply draft no longer a draft, a pull request merged, an approval no longer
waiting); every check is replaced here, so nothing reaches Gmail or GitHub.
"""
import json
from pathlib import Path

import pytest

from harness import morning_desktop as md
from harness import morning_report as mr
from _morning_fixture import APPROVAL, AZURE_LINK, DAY, at, report, save
from test_morning_desktop import call, keys_of, root, web  # noqa: F401 - fixtures


def done(root, *titles, now=None):
    keys = keys_of(md.today(root, now=now or at(8)))
    return md.act(root, {"action": "done", "keys": [keys[t] for t in titles]},
                  now=now or at(8))


def item(today, title):
    return next(i for i in today["items"] if i["title"] == title)


# ---------------------------------------------------------------- marked by the person


def test_marking_done_moves_the_dots_and_the_sentence_and_undo_puts_it_back(root):
    save(root, report())
    first = done(root, "Review Ana's pull request")
    assert first["ok"] is True
    today = first["today"]
    assert (today["done"], today["dots_label"]) == (1, "1 of 5 done")
    assert today["sentence"] == "One down, four to go."
    assert today["summary"].startswith("Collie 0.31.0 shipped")
    assert "Review Ana's pull request" not in [p["label"] for p in today["pills"]]
    assert (item(today, "Review Ana's pull request")["state"],
            item(today, "Review Ana's pull request")["done_by"]) == ("done", "person")
    key = keys_of(today)["Review Ana's pull request"]
    back = md.act(root, {"action": "undo", "keys": [key]}, now=at(8, 5))["today"]
    assert back["done"] == 0 and back["sentence"] == "Five quick ones and you're clear."


def test_doing_the_first_ones_brings_the_next_thing_up(root):
    save(root, report())
    after = done(root, "Review Ana's pull request", "Reply to Ana about Thursday",
                 "Reply to Ben about the invoice")["today"]
    assert [(p["label"], p["href"]) for p in after["pills"]] == [
        ("Answer Collie's question", "/report"), ("Pay the Azure invoice", AZURE_LINK)]
    assert after["pills"][0]["primary"] is True and after["pills"][1]["primary"] is False
    one_draft = md.act(root, {"action": "undo", "keys": [
        keys_of(after)["Reply to Ben about the invoice"]]}, now=at(8))["today"]
    assert one_draft["pills"][0]["label"] == "Review the draft"


def test_the_last_one_and_then_all_clear(root):
    save(root, report())
    four = done(root, "Review Ana's pull request", "Answer Collie's question",
                "Reply to Ana about Thursday", "Reply to Ben about the invoice")["today"]
    assert four["sentence"] == "One to go. Almost there."
    clear = done(root, "Pay the Azure invoice")["today"]
    assert clear["all_done"] is True and clear["show"] is True
    assert clear["sentence"] == "All clear for today."
    assert clear["summary"] == "Nice work. I'll keep an eye on things for the rest of the day."
    assert clear["pills"] == [] and clear["dots_label"] == "5 of 5 done"
    md.act(root, {"action": "dismiss", "reason": "all_clear"}, now=at(9))
    assert md.today(root, now=at(9))["why"] == "dismissed"


def test_a_chinese_report_cheers_in_chinese(root):
    save(root, report(language="zh"))
    assert done(root, "Review Ana's pull request")["today"]["sentence"] == "完成 1 件，还剩 4 件。"
    last = done(root, "Answer Collie's question", "Reply to Ana about Thursday",
                "Reply to Ben about the invoice")["today"]
    assert last["sentence"] == "只剩最后一件了。"
    assert done(root, "Pay the Azure invoice")["today"]["sentence"] == "今天的事都搞定了。"


def test_progress_lives_beside_the_snapshot_and_survives_a_rebuild(root):
    path = Path(save(root, report()))
    before = path.read_bytes()
    done(root, "Review Ana's pull request")
    assert path.read_bytes() == before                       # the report itself is untouched
    state = Path(mr.report_dir(root)) / ("%s.state.json" % DAY)
    assert json.loads(state.read_text(encoding="utf-8"))["schema"] == md.STATE_SCHEMA
    save(root, report())                                     # built again the same morning
    assert md.today(root, now=at(9))["done"] == 1
    assert mr.earlier_report(root, "2026-09-30")["date"] == DAY
    assert mr.load_day(root, DAY)["schema"] == mr.SCHEMA


def test_a_damaged_state_file_is_read_as_no_progress(root):
    save(root, report())
    state = Path(mr.report_dir(root)) / ("%s.state.json" % DAY)
    state.write_text('{"schema": "collie.morning_report.state/1", "items": ', encoding="utf-8")
    assert md.today(root, now=at(8))["done"] == 0
    state.write_text(json.dumps({"schema": md.STATE_SCHEMA, "date": DAY, "items": {
        "kzzzz": {"state": "done", "by": "person"}, "k0123456789abcdef": "done"}}),
        encoding="utf-8")
    assert md.today(root, now=at(8))["done"] == 0
    assert done(root, "Review Ana's pull request")["today"]["done"] == 1


def test_bad_marks_are_refused(root):
    save(root, report())
    for body in ({"action": "done", "keys": "abc"}, {"action": "done", "keys": ["not-a-key"]},
                 {"action": "undo", "keys": [7]}, {"action": "done", "keys": ["k"] * 51}):
        with pytest.raises(ValueError):
            md.act(root, body, now=at(8))
    assert md.act(root, {"action": "done", "keys": []}, now=at(8))["today"]["done"] == 0


# ---------------------------------------------------------------- the resolve pass


def pending(*natives):
    return lambda: [{"id": native, "tool": "bash", "session": "s1"} for native in natives]


def test_a_draft_that_was_sent_or_deleted_is_done(root, monkeypatch):
    save(root, report())
    asked = []
    monkeypatch.setattr(md, "_google_ready", lambda _root: True)
    monkeypatch.setattr(md, "_draft_exists", lambda draft_id, _root: asked.append(draft_id)
                        or draft_id != "r111")
    monkeypatch.setattr(md, "_pr_merged", lambda slug, number: False)
    result = md.resolve(root, now=at(8), approvals=pending(APPROVAL))
    assert sorted(asked) == ["r111", "r222"]
    today = md.today(root, now=at(8))
    assert (item(today, "Reply to Ana about Thursday")["state"],
            item(today, "Reply to Ana about Thursday")["done_by"]) == ("done", "draft_gone")
    assert item(today, "Reply to Ben about the invoice")["state"] == "open"
    assert list(result["done"].values()) == ["draft_gone"]
    assert today["sentence"] == "One down, four to go."


def test_a_merged_pull_request_is_done(root, monkeypatch):
    save(root, report())
    asked = []
    monkeypatch.setattr(md, "_google_ready", lambda _root: False)
    monkeypatch.setattr(md, "_pr_merged", lambda slug, number: asked.append((slug, number))
                        or True)
    md.resolve(root, now=at(8), approvals=pending(APPROVAL))
    assert asked == [("colliehq/collie", "31")]
    got = item(md.today(root, now=at(8)), "Review Ana's pull request")
    assert (got["state"], got["done_by"]) == ("done", "pr_merged")


def test_an_answered_approval_is_done_and_an_unknown_one_is_not(root, monkeypatch):
    save(root, report())
    monkeypatch.setattr(md, "_google_ready", lambda _root: False)
    monkeypatch.setattr(md, "_pr_merged", lambda slug, number: False)

    def boom():
        raise RuntimeError("the registry is not here")

    for approvals in (pending(APPROVAL), boom, None, lambda: "not a list"):
        md.resolve(root, now=at(8), approvals=approvals)
        assert md.today(root, now=at(8))["done"] == 0, approvals
    md.resolve(root, now=at(8), approvals=pending("some-other-question"))
    got = item(md.today(root, now=at(8)), "Answer Collie's question")
    assert (got["state"], got["done_by"]) == ("done", "approval_answered")


def test_a_check_that_fails_leaves_the_item_open(root, monkeypatch):
    save(root, report())

    def fails(*_args):
        raise OSError("offline")

    monkeypatch.setattr(md, "_google_ready", lambda _root: True)
    monkeypatch.setattr(md, "_draft_exists", fails)
    monkeypatch.setattr(md, "_pr_merged", fails)
    result = md.resolve(root, now=at(8), approvals=pending(APPROVAL))
    assert result["done"] == {} and md.today(root, now=at(8))["done"] == 0


def test_google_not_connected_means_drafts_are_not_asked_about(root, monkeypatch):
    save(root, report())
    asked = []
    monkeypatch.setattr(md, "_google_ready", lambda _root: False)
    monkeypatch.setattr(md, "_draft_exists", lambda draft_id, _root: asked.append(draft_id))
    monkeypatch.setattr(md, "_pr_merged", lambda slug, number: False)
    md.resolve(root, now=at(8), approvals=pending(APPROVAL))
    assert asked == [] and md.today(root, now=at(8))["done"] == 0


def test_the_persons_word_wins_over_a_later_check(root, monkeypatch):
    save(root, report())
    monkeypatch.setattr(md, "_google_ready", lambda _root: False)
    monkeypatch.setattr(md, "_pr_merged", lambda slug, number: True)
    md.resolve(root, now=at(8), approvals=pending(APPROVAL))
    key = keys_of(md.today(root, now=at(8)))["Review Ana's pull request"]
    md.act(root, {"action": "undo", "keys": [key]}, now=at(8, 1))
    md.resolve(root, now=at(8, 30), approvals=pending(APPROVAL))
    assert item(md.today(root, now=at(9)), "Review Ana's pull request")["state"] == "open"


def test_no_pass_without_todays_report(root):
    assert md.resolve(root, now=at(8), approvals=pending()) == {"checked": 0, "done": {}}
    save(root, report("2026-09-28"))
    assert md.resolve(root, now=at(8), approvals=pending()) == {"checked": 0, "done": {}}


def test_the_pass_runs_at_most_every_15_minutes_and_only_while_the_morning_shows(root,
                                                                                monkeypatch):
    save(root, report())
    started = []
    monkeypatch.setattr(md, "_spawn", started.append)
    md.today(root, now=at(8))                                  # not asked to
    assert started == []
    md.today(root, now=at(8), resolve=True)
    md.today(root, now=at(8, 10), resolve=True)
    assert len(started) == 1
    md.today(root, now=at(8, 16), resolve=True)
    assert len(started) == 2
    md.today(root, now=at(12, 30), resolve=True)               # after noon: not showing
    md.act(root, {"action": "dismiss"}, now=at(9))
    md.today(root, now=at(9, 40), resolve=True)                # dismissed: not showing
    assert len(started) == 2


def test_the_pass_that_is_started_checks_with_the_live_approvals(root, monkeypatch):
    save(root, report())
    monkeypatch.setattr(md, "_google_ready", lambda _root: False)
    monkeypatch.setattr(md, "_pr_merged", lambda slug, number: False)
    monkeypatch.setattr(md, "_spawn", lambda fn: fn())
    monkeypatch.setattr(md, "_now", lambda: at(8, 2))
    today = md.today(root, now=at(8), resolve=True, approvals=pending())
    assert today["done"] == 0                                 # this answer was written first
    got = item(md.today(root, now=at(8, 3)), "Answer Collie's question")
    assert (got["state"], got["done_by"]) == ("done", "approval_answered")


def test_the_pull_request_check_asks_gh_about_that_one_pull_request(monkeypatch):
    from harness import report_sources
    calls = []

    def run(args, timeout):
        calls.append((list(args), timeout))
        return (0, json.dumps({"merged": True, "state": "closed"}), "")

    monkeypatch.setattr(report_sources, "_gh_runner", lambda _ctx: run)
    assert md._pr_merged("colliehq/collie", "31") is True
    assert calls[0][0] == ["api", "repos/colliehq/collie/pulls/31"]
    monkeypatch.setattr(report_sources, "_gh_runner", lambda _ctx: lambda args, timeout: (
        0, json.dumps({"merged": False, "state": "open"}), ""))
    assert md._pr_merged("colliehq/collie", "31") is False
    monkeypatch.setattr(report_sources, "_gh_runner",
                        lambda _ctx: lambda args, timeout: (1, "", "HTTP 404"))
    with pytest.raises(RuntimeError):
        md._pr_merged("colliehq/collie", "31")
    for slug, number in (("../../etc", "31"), ("colliehq/..", "31"), ("colliehq/collie", "3a")):
        with pytest.raises(ValueError):
            md._pr_merged(slug, number)


# ---------------------------------------------------------------- the route


def test_the_route_marks_done_with_the_token(web, root):
    base, token = web
    save(root, report())
    key = md.today(root, now=at(8))["items"][0]["key"]
    assert call(base + "/api/report/today", "POST", {"action": "done", "keys": [key]})[0] == 403
    assert md.today(root, now=at(8))["done"] == 0
    code, _headers, body = call(base + "/api/report/today?token=" + token, "POST",
                                {"action": "done", "keys": [key]})
    assert code == 200 and json.loads(body)["today"]["done"] == 1
    code, _headers, body = call(base + "/api/report/today?token=" + token, "POST",
                                {"action": "done", "keys": ["kffffffffffffffff"]})
    assert code == 400 and "error" in json.loads(body)
