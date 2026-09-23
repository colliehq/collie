"""A response from a closed panel must not erase text typed on the next visit."""
import pytest
from playwright.sync_api import expect
from test_web_ui_run_status import browser, server, ui  # noqa: F401
from test_web_control_panel_races import Held, SPECS_A, _open_panel


@pytest.mark.parametrize("status", [200, 500])
def test_closing_a_panel_retires_a_refresh_before_the_next_visit(ui, status):
    page = ui.page
    listing = Held(page, "**/api/automations?*", SPECS_A, later=SPECS_A)
    _open_panel(page)
    page.locator('[data-control-tab="automations"]').click()
    listing.wait(); listing.release()
    page.get_by_role("button", name="New automation", exact=True).click()
    page.fill("#autoTask", "first visit")
    page.click("#activityRefresh")
    listing.wait()
    page.click("#activityClose")
    _open_panel(page)
    page.fill("#autoTask", "new text on the next visit")
    listing.release(SPECS_A if status == 200 else {"error": "old visit failed"}, status=status)
    page.wait_for_timeout(150)
    expect(page.locator("#autoTask")).to_have_value("new text on the next visit")
    assert "old visit failed" not in page.inner_text("#activityNotice")
