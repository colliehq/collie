"""A saved Pack result remains actionable when the project changes after review."""
import json
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import ui, server, browser


@pytest.mark.parametrize("width", [1280, 390])
def test_apply_conflict_names_the_blocking_path_and_requires_a_fresh_review(ui, width):
    page = ui.page
    artifact_id = "saved-pack"
    state = {"conflict": True, "applied": False, "posts": []}
    changed = [{"path": "notes/summary.md", "action": "write"}]
    reason = "notes is a file, not a directory"
    page.route("**/api/session/s-read", lambda route: route.fulfill(
        content_type="application/json", body=json.dumps({
            "id": "s-read", "cwd": "/project", "messages": [],
            "run_receipts": [{"pack": True, "artifact": {"id": artifact_id}}],
        })))

    def artifact_request(route):
        if urlsplit(route.request.url).path.endswith("/apply"):
            state["posts"].append(route.request.post_data_json)
            if state["conflict"]:
                route.fulfill(status=409, content_type="application/json", body=json.dumps({
                    "ok": False, "applied": False, "code": "conflict", "changed": [],
                    "error": "1 path changed since this pack ran; nothing was applied",
                    "conflicts": [{"path": "notes/summary.md", "reason": reason}],
                }))
                return
            state["applied"] = True
            route.fulfill(content_type="application/json", body=json.dumps({
                "ok": True, "applied": True, "changed": changed,
            }))
            return
        route.fulfill(content_type="application/json", body=json.dumps({
            "check": {"ok": True, "changed": [] if state["applied"] else changed},
            "files": [{"path": "notes/summary.md", "action": "add", "diff": "+ summary"}],
        }))

    page.route("**/api/pack-artifact**", artifact_request)
    page.locator(".thread").filter(has_text="Read README.md").first.click()
    card = page.locator('[data-pack-artifact="saved-pack"]')
    expect(card).to_be_visible()
    page.set_viewport_size({"width": width, "height": 900})
    card.get_by_role("button", name="Review saved changes", exact=True).click()
    expect(card).to_have_attribute("data-state", "ready")
    apply = card.get_by_role("button", name="Apply saved changes", exact=True)
    apply.click()
    expect(card).to_have_attribute("data-state", "conflict")
    expect(card).to_contain_text("notes/summary.md: " + reason)
    expect(apply).to_be_hidden()
    assert state["posts"] == [{"session": "s-read", "id": artifact_id}]

    # Resolve the project conflict, review the saved changes, then apply without another model run.
    state["conflict"] = False
    card.get_by_role("button", name="Refresh review", exact=True).click()
    expect(card).to_have_attribute("data-state", "ready")
    expect(card).not_to_contain_text(reason)
    apply.click()
    expect(card).to_have_attribute("data-state", "applied")
    expect(apply).to_be_hidden()
    assert state["posts"] == [{"session": "s-read", "id": artifact_id}] * 2
