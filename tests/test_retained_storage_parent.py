"""A failed retained-row write must preserve the already recoverable text draft."""
from playwright.sync_api import expect
from test_web_ui_run_status import ui, server, browser, _Fixture  # noqa: F401
from test_web_busy_send_ui import ASK, open_read_thread, send, retained_rows


def test_reload_preserves_draft_when_retained_storage_is_unavailable(ui):
    page = ui.page
    fail_retained_only = """(() => {
      const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function(key, value) {
        if (String(key).startsWith('collie.retained.v1:')) {
          throw new DOMException('injected retained-record storage quota failure', 'QuotaExceededError');
        }
        return original.call(this, key, value);
      };
    })();"""
    page.add_init_script(fail_retained_only)
    page.evaluate(fail_retained_only)
    _Fixture.queue_ack.clear()
    _Fixture.queue_fail_all = True
    try:
        open_read_thread(page)
        send(page, ASK)
        expect(page.locator('#input')).to_have_value(ASK)
        assert _Fixture.queue_seen.wait(3), 'must reach actual in-flight queue save'
        page.reload(wait_until='load')
        page.wait_for_selector('#input')
    finally:
        _Fixture.queue_ack.set()
    expect(retained_rows(page)).to_have_count(0)
    expect(page.locator('#input')).to_have_value(ASK)
