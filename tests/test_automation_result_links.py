"""Reach an automation's saved work without replaying it or exposing it in health metadata."""
import json
import re

import pytest
from playwright.sync_api import expect

from harness.controlcenter import _public_execution
from test_web_controlplane_api import _json, web_server  # noqa: F401
from test_web_ui_run_status import _Fixture, browser, server, ui  # noqa: F401


def _execution(state="needs_you", **receipt):
    result = {"session_id": "s-read", "session_saved": True,
              "summary": "private answer", "messages": ["private transcript"],
              "token": "private credential"}
    result.update(receipt)
    return {"execution_id": "execution-1", "automation_id": "report",
            "state": state, "attempts": 1, "request_json": "private request",
            "result_json": json.dumps(result)}


def test_execution_projection_keeps_only_the_saved_conversation_identifier():
    public = _public_execution(_execution())
    assert public["session_id"] == "s-read"
    assert "private" not in json.dumps(public)
    assert "result_json" not in public and "request_json" not in public


def test_saved_result_link_reaches_only_the_authenticated_operations_snapshot(web_server):
    from harness.automations import AutomationStore, TriggerEngine

    base, token, state = web_server
    with AutomationStore(str(state / "automations.db")) as store:
        store.upsert({"automation_id": "report", "task": "prepare a report",
                      "trigger": {"provider": "timer", "every_s": 60,
                                  "fire_immediately": True}}, now=1)
        TriggerEngine(store).tick(1)
        claimed = store.claim(now=2)
        assert store.finish(claimed["execution_id"], "needs_you",
                            json.loads(_execution()["result_json"]), "budget reached",
                            lease_token=claimed["lease_token"], now=3)
    assert _json(base + "/api/automations")[0] == 403
    status, data = _json(base + "/api/automations?token=" + token)
    assert status == 200
    assert data["executions"][0]["session_id"] == "s-read"
    assert "private" not in json.dumps(data)


@pytest.mark.parametrize("receipt", [
    {"session_saved": False}, {"session_saved": "true"},
    {"session_id": "../outside"}, {"session_id": ".."},
    {"session_id": "bad:stream"}, {"session_id": ""}, {"session_id": None},
    {"session_id": "x" * 129},
])
def test_unconfirmed_or_invalid_conversation_is_not_advertised(receipt):
    assert "session_id" not in _public_execution(_execution(**receipt))


@pytest.mark.parametrize("raw", ["{", "[]", "null", "42", "{}"])
def test_older_or_unreadable_receipts_do_not_break_the_execution_list(raw):
    row = _execution()
    row["result_json"] = raw
    assert "session_id" not in _public_execution(row)


@pytest.mark.parametrize("state", ["succeeded", "needs_you"])
def test_opening_saved_automation_work_is_navigation_only(ui, state):
    page = ui.page
    row = _public_execution(_execution(state))
    mutations = []
    page.on("request", lambda request: mutations.append(request.url)
            if request.method not in ("GET", "HEAD", "OPTIONS") else None)
    page.route("**/api/automations*", lambda route: route.fulfill(
        content_type="application/json",
        body=json.dumps({"specs": [], "recipes": [], "executions": [row]})))
    page.click("#topbarMore > summary")
    page.click("#activityBtn")
    page.locator("#activityPanel").wait_for(state="visible")
    page.locator('[data-control-tab="automations"]').click()
    link = page.get_by_role("button", name="Open saved conversation", exact=True)
    expect(link).to_have_count(1)
    link.click()
    expect(page).to_have_url(re.compile(r"session=s-read"))
    expect(page.locator("#activityPanel")).to_be_hidden()
    expect(page.locator("#log")).to_contain_text("README")
    assert not mutations, mutations
    assert not _Fixture.stream_requests and not _Fixture.queue_posts and not _Fixture.queue_starts
