"""Editing an automation in Automation Studio must not rewrite what the form does not show.

`upsert` replaces the whole stored record, so everything the editor omits — the execution mode, a
continued session, notification choices, the tool allowlist, write roots, the rest of the budget —
is decided by what the Save button posts. tests/automation_budget_test.js proves that against the
extracted functions; this suite proves it in a real Chromium against the real page and the real
store: a stored plan-mode automation is edited through the UI and must still be a plan run in the
database afterwards.

It also covers the one thing a single-node stub cannot model: opening a second editor used to leave
two copies of every `id` on the page, so `getElementById` answered for the newest one and the older,
still visible form saved the other automation's fields.

No automation is ever run here. Both fixtures stay disabled; only the spec store is touched.

    python3 tests/browser_suite.py automation_editor_ui_check
"""
import os
import sys

from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8996")
TOKEN = os.environ.get("COLLIE_TOKEN", "")
STATE = os.environ.get("COLLIE_STATE_DIR", "")

# A plan-mode automation someone configured outside this panel: read-only execution on a continued
# session, a deliberate turn cap, a tightened action cap, a narrow tool allowlist and write roots.
PLAN_SPEC = {
    "automation_id": "nightly-notes", "task": "keep the release notes current", "enabled": False,
    "trigger": {"provider": "timer", "every_s": 3600, "catch_up": False},
    "context": {"policy": "continued", "session_id": "s-1104"},
    "workspace": {"mode": "isolated"},
    "execution": {"mode": "plan", "model": "mock"},
    "notifications": ["success"],
    "permissions": {"read_roots": ["."], "write_roots": ["."], "tools": ["read_file", "grep"],
                    "desktop_targets": ["notes.app"]},
    "budget": {"max_turns": 400, "max_actions": 7, "max_retries": 4, "max_cost_usd": 25},
}
OTHER_SPEC = {
    "automation_id": "second-watch", "task": "watch the changelog", "enabled": False,
    "trigger": {"provider": "timer", "every_s": 7200}, "workspace": {"mode": "isolated"},
    "permissions": {"read_roots": ["."]}, "budget": {"max_cost_usd": 3},
}

_fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        _fails.append(what)


def _store():
    from harness.automations import AutomationStore
    return AutomationStore(os.path.join(STATE, "automations.db"))


def seed():
    os.makedirs(STATE, exist_ok=True)
    with _store() as store:
        store.upsert(PLAN_SPEC)
        store.upsert(OTHER_SPEC)


def stored(automation_id):
    with _store() as store:
        spec = store.spec(automation_id)
    return spec.as_dict() if spec else {}


def open_automations(pg):
    pg.goto(BASE + "/?token=" + TOKEN, wait_until="load")
    try:                                   # mock provider opens onboarding over everything
        pg.wait_for_selector("#obOverlay.open", timeout=15000)
        pg.click("#obSkip")
        pg.wait_for_selector("#obOverlay.open", state="detached", timeout=3000)
    except Exception:
        pass                               # already authed, or it never comes: carry on
    # System activity lives under the sidebar's "More" disclosure.
    pg.eval_on_selector("#navActivity", "el => { const d = el.closest('details'); if (d) d.open = true; }")
    pg.click("#navActivity")
    pg.click('[data-control-tab="automations"]')
    pg.wait_for_selector('.activity-row:has-text("nightly-notes")', timeout=15000)


def edit(pg, automation_id):
    pg.locator('.activity-row:has-text("%s")' % automation_id).first \
      .get_by_role("button", name="Edit").click()
    pg.wait_for_selector("#autoSave", timeout=5000)


def save(pg, answers):
    """Press Save and report whether the store accepted the upsert.

    The panel's "Automation saved." notice is not the signal to wait on: the reload it triggers
    clears the notice again within the same tick. The upsert response is the durable answer.
    """
    before = len(answers)
    pg.click("#autoSave")
    for _ in range(40):
        if len(answers) > before:
            return answers[-1] == 200
        pg.wait_for_timeout(250)
    print("    no upsert response; panel says: " + (pg.text_content("#activityNotice") or "(silent)"))
    return False


def main():
    if not TOKEN:
        print("  COLLIE_TOKEN not set — run this through tests/browser_suite.py")
        return 2
    if not STATE:
        print("  COLLIE_STATE_DIR not set — the suite must own the automation store it seeds")
        return 2
    seed()

    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1280, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        posted, answers = [], []
        # Capture what Save posts, then let it reach the real store: the payload and what the
        # database ends up holding are two different claims and both are worth making.
        pg.route("**/api/automations/upsert*", lambda route: (
            posted.append(route.request.post_data_json), route.continue_()))
        pg.on("response", lambda r: "/api/automations/upsert" in r.url and answers.append(r.status))

        open_automations(pg)
        edit(pg, "nightly-notes")
        check(pg.input_value("#autoId") == "nightly-notes", "Edit opens the chosen automation")
        check(pg.input_value("#autoActions") == "7",
              "the stored tool-action budget is an editable field")
        help_text = pg.text_content("#autoBudgetHelp") or ""
        check("tool-action" in help_text, "the help names the tool-action ceiling")
        check("400" in help_text, "the help names the turn cap this automation carries")

        # Opening a second editor must replace the first, not duplicate every id on the page.
        edit(pg, "second-watch")
        check(pg.eval_on_selector_all(".control-editor", "els => els.length") == 1,
              "a second Edit replaces the open editor")
        check(pg.eval_on_selector_all("#autoId", "els => els.length") == 1,
              "no duplicate element ids are left behind")
        check(pg.input_value("#autoId") == "second-watch",
              "and the visible form is the automation that was just opened")

        edit(pg, "nightly-notes")
        pg.fill("#autoCost", "40")
        check(save(pg, answers), "the edit saves without an error from the store")

        sent = posted[0] if posted else {}
        sent_spec = (sent or {}).get("spec") or {}
        check((sent_spec.get("execution") or {}).get("mode") == "plan",
              "the saved payload is still a plan run")
        check((sent_spec.get("permissions") or {}).get("tools") == ["grep", "read_file"],
              "the saved payload keeps the tool allowlist")   # the store canonicalizes the order

        after = stored("nightly-notes")
        check((after.get("execution") or {}).get("mode") == "plan",
              "the stored automation is still read-only after an edit")
        check((after.get("execution") or {}).get("model") == "mock",
              "an execution option the form does not show survives the edit")
        check(after.get("context") == {"policy": "continued", "session_id": "s-1104"},
              "the continued session survives the edit")
        check(after.get("notifications") == ["success"],
              "the notification choice survives the edit")
        permissions = after.get("permissions") or {}
        check(list(permissions.get("tools") or ()) == ["grep", "read_file"],
              "the tool allowlist survives the edit")
        check(bool(permissions.get("write_roots")), "write roots survive the edit")
        check(list(permissions.get("desktop_targets") or ()) == ["notes.app"],
              "desktop targets survive the edit")
        check(permissions.get("current_workspace") is False and
              permissions.get("webhook_ingest") is False,
              "no authority is widened by saving")
        budget = after.get("budget") or {}
        check(budget.get("max_turns") == 400, "the stored turn cap survives the edit")
        check(budget.get("max_actions") == 7, "the untouched action budget survives the edit")
        check(budget.get("max_retries") == 4, "the stored retry count survives the edit")
        check(budget.get("max_cost_usd") == 40, "the edited cost budget is what changed")
        check(after.get("trigger") == {"provider": "timer", "every_s": 3600, "catch_up": False},
              "trigger fields the form does not show survive the edit")

        # Saving reloads the panel, which closes the editor. Reopen it to edit the new field.
        # save() returns on the upsert response and the reload comes after it, so wait for the
        # close: counting at once failed on a slow Ubuntu runner (PR #16) with nothing wrong.
        try:
            pg.wait_for_function("document.querySelectorAll('.control-editor').length === 0",
                                 timeout=10000)
            closed = True
        except Exception:
            closed = False
        check(closed, "a saved editor closes with the panel reload")
        edit(pg, "nightly-notes")
        pg.fill("#autoActions", "250")
        check(save(pg, answers), "an edited action budget is accepted by the store")
        check((stored("nightly-notes").get("budget") or {}).get("max_actions") == 250,
              "an edited tool-action budget is stored as written")

        edit(pg, "nightly-notes")
        pg.click("#autoClose")
        check(pg.eval_on_selector_all(".control-editor", "els => els.length") == 0,
              "Close takes the editor off the page")

        check(not errs, "no page errors: " + "; ".join(errs[:3]))
        br.close()

    print(("FAILED " if _fails else "ok ") + str(len(_fails)) + " failed")
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
