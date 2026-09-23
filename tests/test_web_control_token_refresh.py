"""A long-lived operations panel recovers its process token before retrying an action."""
import json
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import TOKEN, browser, server, ui  # noqa: F401


@pytest.mark.parametrize("first_status,accept_retry", [(403, True), (403, False), (503, False)])
def test_control_action_refreshes_only_a_refused_token_and_retries_once(ui, first_status, accept_retry):
    page = ui.page
    attempts, accepted, refreshes = [], [], []
    fresh_token = "replacement-process-token"
    spec = {"automation_id": "daily-review", "task": "Review the project",
            "trigger": {"provider": "timer"}, "enabled": False}

    def automation(route, request):
        if request.method == "GET":
            return route.fulfill(content_type="application/json", body=json.dumps(
                {"specs": [spec], "recipes": [], "executions": []}))
        assert urlparse(request.url).path == "/api/automations/enabled"
        token = parse_qs(urlparse(request.url).query).get("token", [""])[0]
        payload = request.post_data_json
        attempts.append({"token": token, "payload": payload})
        if len(attempts) == 1 or not accept_retry or token != fresh_token:
            return route.fulfill(status=first_status, content_type="application/json",
                                 body=json.dumps({"error": "process token refused" if first_status == 403
                                                  else "service unavailable"}))
        accepted.append(payload)
        spec["enabled"] = payload["enabled"]
        return route.fulfill(content_type="application/json", body='{"ok": true}')

    def token(route):
        if attempts:
            refreshes.append(True)
        return route.fulfill(content_type="application/json", body=json.dumps(
            {"token": fresh_token if attempts else TOKEN}))

    page.route("**/api/automations**", automation)
    page.route("**/api/session-token*", token)
    page.click("#topbarMore > summary")
    page.click("#activityBtn")
    page.locator('[data-control-tab="automations"]').click()
    page.get_by_role("button", name="Enable", exact=True).click()

    payload = {"automation_id": "daily-review", "enabled": True}
    if accept_retry:
        expect(page.get_by_role("button", name="Disable", exact=True)).to_be_visible()
        assert accepted == [payload], "one click must perform one authorized mutation"
        expect(page.locator("#activityNotice")).not_to_have_class("bad")
    else:
        expect(page.locator("#activityNotice")).to_contain_text(
            "process token refused" if first_status == 403 else "service unavailable")
        expect(page.get_by_role("button", name="Enable", exact=True)).to_be_enabled()
        assert accepted == []
    assert len(attempts) == (2 if first_status == 403 else 1)
    assert all(row["payload"] == payload for row in attempts)
    if first_status == 403:
        assert refreshes
        assert attempts[0]["token"] != fresh_token
        assert attempts[1]["token"] == fresh_token
