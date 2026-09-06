"""Mission input drafts, routing and acknowledgements in the actual task page."""
import json
from urllib.parse import urlsplit

from playwright.sync_api import expect
from test_web_ui_run_status import ui, server, browser


def stage(ui, *, fail_first=False, hold_post=False):
    state = {"mission_id":"mission-ui", "state":"running", "goal":"Refine the category filter",
             "summary":{"title":"Refine the category filter", "current":"Reading the parser"},
             "case":{}, "controls":["pause", "cancel"]}
    data = {"state":state, "notes":[], "posts":[], "held":[], "reads":0}

    def handle(route):
        path = urlsplit(route.request.url).path
        code = 200
        if path == "/api/missions":
            out = {"missions":[dict(state,title=state["goal"])]}
        elif path == "/api/mission":
            data["reads"] += 1
            out = state
        elif path == "/api/mission/notes":
            out = {"mission_id":"mission-ui", "notes":data["notes"]}
        elif path == "/api/mission/note":
            post = route.request.post_data_json
            data["posts"].append(post)
            if hold_post:
                data["held"].append(route)
                return
            if fail_first and len(data["posts"]) == 1:
                code, out = 503, {"error":"Connection interrupted; retry the saved request"}
            else:
                note = {"id":post["client_id"], "text":post["text"], "state":"pending"}
                data["notes"] = [note]
                out = {"accepted":True, "mission_id":"mission-ui", "note":note}
        else:
            out = {}
        route.fulfill(status=code, content_type="application/json", body=json.dumps(out))
    ui.page.route("**/api/mission**", handle)
    return data


def open_task(ui):
    ui.page.locator("#navMissions").click()
    ui.page.locator(".mission-list-card").click()
    ui.page.locator(".mission-updates textarea").wait_for()


def test_mission_has_its_own_restorable_input_surface(ui):
    stage(ui)
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    expect(ui.page.locator("#log")).to_contain_text("README")
    open_task(ui)
    expect(ui.page.locator("#composer")).to_be_hidden()
    expect(ui.page.locator("#log")).not_to_contain_text("README")
    expect(ui.page.locator("#pageTitle")).to_have_text("Refine the category filter")
    assert "mission=mission-ui" in ui.page.url and "session=" not in ui.page.url
    draft = "  Preserve exact whitespace\n\nand the final correction.  "
    ui.page.locator(".mission-updates textarea").fill(draft)
    ui.page.reload(wait_until="load")
    expect(ui.page.locator(".mission-updates textarea")).to_have_value(draft)
    expect(ui.page.locator("#composer")).to_be_hidden()
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    expect(ui.page.locator("#composer")).to_be_visible()
    assert "session=s-read" in ui.page.url and "mission=" not in ui.page.url


def test_poll_keeps_focus_and_the_unsent_mission_draft(ui):
    data = stage(ui)
    data["state"]["action_in_flight"] = {"action":"run", "state":"running"}
    open_task(ui)
    field = ui.page.locator(".mission-updates textarea")
    field.fill("Still typing the next requirement")
    field.focus()
    before = data["reads"]
    ui.page.wait_for_timeout(2400)
    assert data["reads"] > before, "a real status poll must have happened"
    expect(field).to_be_focused()
    expect(field).to_have_value("Still typing the next requirement")
    expect(ui.page.get_by_role("button", name="Save instruction", exact=True)).to_be_enabled()
    assert not data["posts"]


def test_retry_after_refresh_reuses_the_same_mission_note_identity(ui):
    data = stage(ui, fail_first=True)
    open_task(ui)
    text = "  Exact instruction\nSecond paragraph and final space. "
    ui.page.locator(".mission-updates textarea").fill(text)
    ui.page.get_by_role("button",name="Save instruction",exact=True).click()
    expect(ui.page.locator(".mission-updates")).to_contain_text("Connection interrupted")
    expect(ui.page.locator(".mission-updates textarea")).to_have_value(text)
    ui.page.reload(wait_until="load")
    ui.page.get_by_role("button",name="Save instruction",exact=True).click()
    expect(ui.page.locator(".mission-updates textarea")).to_have_value("")
    assert len(data["posts"]) == 2 and data["posts"][0] == data["posts"][1]
    assert data["posts"][0]["text"] == text
    ui.page.locator(".mission-updates summary").click()
    expect(ui.page.locator(".mission-note-entry p")).to_have_text(text)
    expect(ui.page.locator(".mission-note-entry small")).to_have_text("Saved · awaiting next boundary")


def test_acknowledging_one_note_never_erases_a_newer_draft(ui):
    data = stage(ui, hold_post=True)
    open_task(ui)
    field = ui.page.locator(".mission-updates textarea")
    field.fill("First instruction")
    ui.page.get_by_role("button",name="Save instruction",exact=True).click()
    field.fill("New instruction typed while saving")
    assert len(data["held"]) == 1
    data["held"][0].fulfill(status=200, content_type="application/json", body=json.dumps({
        "accepted":True, "mission_id":"mission-ui", "note":{"state":"pending"}}))
    expect(ui.page.locator(".mission-updates")).to_contain_text("Saved. Collie will read it")
    expect(field).to_have_value("New instruction typed while saving")
    ui.page.reload(wait_until="load")
    expect(ui.page.locator(".mission-updates textarea")).to_have_value("New instruction typed while saving")
