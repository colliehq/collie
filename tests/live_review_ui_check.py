"""Exercise stop -> retained review -> real Markdown download in an isolated browser."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from playwright.sync_api import expect, sync_playwright

    with tempfile.TemporaryDirectory(prefix="collie-live-review-ui-") as stage:
        os.environ.update({
            "COLLIE_STATE_DIR": stage,
            "COLLIE_SESSIONS_DIR": str(Path(stage) / "sessions"),
            "COLLIE_SETTINGS_PATH": str(Path(stage) / "settings.json"),
            "COLLIE_PROVIDER": "mock", "COLLIE_EMBED": "bm25", "COLLIE_LANG": "en",
        })
        Path(os.environ["COLLIE_SETTINGS_PATH"]).write_text(
            json.dumps({"LANG": "en", "PROVIDER": "mock"}), encoding="utf-8")
        from harness import live_copilot, webapp

        # No ticker, desktop observation, audio, provider, or external MCP probe in this server.
        live_copilot.capabilities = lambda: {"speech_ready": False}
        store = live_copilot.LiveSessionStore(stage)
        store.start(context="Customer launch", listen=False, observe_apps=False,
                    observe_ui=False, observe_input=False)
        value = store._read()
        value["summary"] = "Confirm support coverage before the release."
        value["suggestions"] = [{"id": "cue-review", "kind": "action", "urgency": "soon",
                                  "text": "Check the support rota.", "dismissed": False}]
        store._write(value)
        store.add_event(source="you", kind="speech", text="Private launch transcript.")
        session = value["session_id"]
        server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                try:
                    page = browser.new_page(viewport={"width": 1200, "height": 820})
                    errors = []
                    page.on("pageerror", lambda exc: errors.append(str(exc)))
                    page.goto("http://127.0.0.1:%d/live" % server.server_port)
                    expect(page.locator("#stop")).to_be_visible()
                    page.locator("#stop").click()
                    expect(page.locator("#summaryTitle")).to_have_text("Session review")
                    expect(page.locator("#summary")).to_contain_text(value["summary"])
                    expect(page.locator("#reviewHint")).to_be_visible()
                    expect(page.locator("#stop")).to_be_hidden()
                    expect(page.locator("#toggleUnderstand")).to_be_hidden()
                    assert not store.snapshot()["active"]
                    for button in page.locator("#suggestions button").all():
                        expect(button).to_be_disabled()
                    print("PASS stopped session keeps its review and disables live actions")

                    page.reload()
                    expect(page.locator("#summaryTitle")).to_have_text("Session review")
                    expect(page.locator("#exportContext")).not_to_be_checked()
                    with page.expect_download() as download_info:
                        page.locator("#exportSession").click()
                    download = download_info.value
                    assert download.suggested_filename == session + ".md"
                    content = Path(download.path()).read_text(encoding="utf-8")
                    assert value["summary"] in content and "Status: Ended" in content
                    assert "Private launch transcript." not in content
                    print("PASS reload preserves review; default download excludes the context log")

                    page.locator("#exportContext").check()
                    with page.expect_download() as download_info:
                        page.locator("#exportSession").click()
                    assert "Private launch transcript." in Path(
                        download_info.value.path()).read_text(encoding="utf-8")
                    print("PASS explicit context selection includes retained transcript in download")

                    page.set_viewport_size({"width": 390, "height": 844})
                    expect(page.locator("#exportSession")).to_be_visible()
                    assert page.evaluate(
                        "document.documentElement.scrollWidth <= innerWidth + 1")
                    assert not errors, errors
                    print("PASS mobile review has no horizontal overflow or JavaScript errors")
                finally:
                    browser.close()
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


if __name__ == "__main__":
    main()
