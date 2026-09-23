"""The red badge a terminal automation outcome leaves behind must be answerable from the page.

A needs_you automation execution used to offer navigation and nothing else, so the recovery lane
stayed red with no gesture that could ever end it.  This drives the real page in Chromium against
the real durable store: the click posts one acknowledgement for that exact execution, the lane
clears, and the run keeps its state, its error and its receipt in the database afterwards.  No
run, enqueue or retry is allowed to happen on the way.
"""
import json

import pytest
from test_web_ui_run_status import TOKEN, _Fixture, browser, server, ui   # noqa: F401


def _seed(tmp_path):
    """One finished needs_you execution, written through the real claim/finish path."""
    from harness.automations import NEEDS_YOU, AutomationSpec, AutomationStore

    store = AutomationStore(str(tmp_path / "automations.db"))
    spec = store.upsert(AutomationSpec.from_dict({
        "automation_id": "daily-review", "task": "review this repository",
        "trigger": {"provider": "timer", "every_s": 3600},
        "workspace": {"mode": "isolated"}, "permissions": {}, "enabled": True}))
    execution_id = store.enqueue(spec, "day-1", {"kind": "timer"}, now=1000.0)
    row = store.claim(now=1000.0)
    store.finish(execution_id, NEEDS_YOU,
                 {"outcome": "needs_you", "session_saved": True, "session_id": "auto-1",
                  "answer": "private model answer"},
                 "stopped before finishing", lease_token=row["lease_token"], now=1000.0)
    store.close()
    return execution_id


@pytest.fixture
def state(tmp_path, monkeypatch):
    """A real, isolated durable state root behind the page's operations routes."""
    from harness.controlplane import health

    root = tmp_path / "state"
    root.mkdir()
    monkeypatch.setenv("COLLIE_STATE_DIR", str(root))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(root / "sessions"))
    monkeypatch.setenv("COLLIE_NOTES_DIR", str(root / "notes"))
    monkeypatch.setattr("harness.supervisor.query_windows",
                        lambda **_: {"installed": False, "mode": "none"})
    monkeypatch.setattr("harness.supervisor.remote_notifications_enabled",
                        lambda *_a, **_k: False)
    # Doctor's own checks are not under test here; the automation lane below is entirely real.
    monkeypatch.setattr("harness.doctor.report",
                        lambda path=None, **_k: {"health": health(str(root),
                                                                  probe_services=False),
                                                 "checks": []})
    return root


def _install_routes(page, root, calls):
    from harness.controlcenter import automation_review_execution, recovery_snapshot

    def snapshot(route):
        calls.append(("GET", "/api/recovery-center"))
        route.fulfill(content_type="application/json",
                      body=json.dumps(recovery_snapshot(str(root))))

    def review(route):
        body = json.loads(route.request.post_data or "{}")
        calls.append(("POST", "/api/automations/review", body.get("execution_id")))
        try:
            value = automation_review_execution(body.get("execution_id"), str(root))
        except (KeyError, ValueError) as exc:
            return route.fulfill(status=400, content_type="application/json",
                                 body=json.dumps({"error": str(exc)}))
        route.fulfill(content_type="application/json", body=json.dumps(value))

    page.route("**/api/recovery-center*", snapshot)
    page.route("**/api/automations/review*", review)
    page.on("request", lambda request: calls.append(("REQUEST", request.method,
                                                     request.url.split("?")[0])))


def _open_recovery_tab(page):
    page.click("#topbarMore > summary")
    page.click("#activityBtn")
    page.evaluate("() => { document.getElementById('topbarMore').open = false; }")
    page.wait_for_selector("#activityPanel:not([hidden])", timeout=8000)
    page.click('[data-control-tab="recovery"]')
    page.wait_for_function(
        "() => document.getElementById('controlSummary').textContent.includes('need a decision')",
        timeout=8000)
    page.wait_for_timeout(150)


def test_marking_a_terminal_needs_you_reviewed_clears_the_lane_and_keeps_the_record(
        ui, state, tmp_path):
    from harness.automations import AutomationStore

    execution_id = _seed(state)
    calls = []
    _install_routes(ui.page, state, calls)
    _open_recovery_tab(ui.page)

    page = ui.page
    row = page.locator(".activity-lane:not(.optional-lane) .activity-row").filter(
        has_text="Automation outcome needs review")
    assert row.count() == 1, "a finished needs_you run asks for a decision"
    badge = page.locator("#activityHealth")
    assert badge.inner_text() == "needs_you" and "bad" in (badge.get_attribute("class") or "")
    assert "1 need a decision" in page.inner_text("#controlSummary")
    assert page.inner_text("#activityGrid").find("private model answer") < 0

    row.get_by_role("button", name="Mark reviewed").click()
    page.wait_for_function(
        "() => document.getElementById('controlSummary').textContent.includes('0 need a decision')",
        timeout=8000)
    assert page.locator(".activity-lane:not(.optional-lane) .activity-row").filter(
        has_text="Automation outcome needs review").count() == 0
    badge = page.locator("#activityHealth")
    assert "bad" not in (badge.get_attribute("class") or ""), "the lane is answerable"

    posts = [call for call in calls if call[0] == "POST"]
    assert posts == [("POST", "/api/automations/review", execution_id)], \
        "exactly one acknowledgement, scoped to this execution"
    forbidden = [call for call in calls if call[0] == "REQUEST" and call[1] == "POST"
                 and not call[2].endswith("/api/automations/review")]
    assert forbidden == [], "acknowledging must not run, enqueue, retry or resume anything"

    store = AutomationStore(str(state / "automations.db"))
    rows = store.executions()
    store.close()
    assert len(rows) == 1 and rows[0]["execution_id"] == execution_id
    assert rows[0]["state"] == "needs_you", "the raw historical state is untouched"
    assert rows[0]["last_error"] == "stopped before finishing"
    assert json.loads(rows[0]["result_json"])["session_id"] == "auto-1"
    assert rows[0]["attention_reviewed_at"] > 0

    # Durable: the same page reloaded from the same store does not ask again.
    page.reload(wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    _install_routes(page, state, calls)
    _open_recovery_tab(page)
    assert page.locator(".activity-lane:not(.optional-lane) .activity-row").filter(
        has_text="Automation outcome needs review").count() == 0
