"""The now-playing control in collie's own UI — the one that exists on every platform.

macOS gets a menu-bar item and Windows a tray icon, but this pill is always there, and on Linux it is
the only control. What has to be true: it is invisible while nothing is playing, it appears with the
track when collie starts something, and clicking it actually stops the audio rather than only hiding
itself.

The first version failed the first of those. `[hidden]` loses to `.pill`'s `display:inline-flex`, so
the attribute was set and the pill stayed on screen anyway — an indicator that shows when nothing is
playing is worse than no indicator, and nothing about the markup looked wrong.

Needs a browser and a running `collie web`, so it runs directly rather than from run_all.sh:

    COLLIE_WEB=http://127.0.0.1:8996 COLLIE_TOKEN=<token> python3 tests/nowplaying_web_check.py
"""
import os
import sys
import time

from playwright.sync_api import sync_playwright

BASE = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8996")
TOKEN = os.environ.get("COLLIE_TOKEN", "")

_fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        _fails.append(what)


def main():
    if not TOKEN:
        print("  COLLIE_TOKEN not set — start `collie web` and pass its token")
        return 2

    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        pg.goto(BASE + "/?token=" + TOKEN, wait_until="networkidle")
        check(not errs, "the page loads without a JS error%s"
              % ("" if not errs else ": " + errs[0][:80]))
        check(pg.locator("#npPill").count() == 1, "the control is in the page")
        check(pg.locator("#npPill").is_hidden(), "and hidden while nothing is playing")

        pg.request.post(BASE + "/api/desktop/play?token=" + TOKEN,
                        data={"q": "Cruel Summer"}, timeout=180000)
        pg.wait_for_selector("#npPill:not([hidden])", timeout=20000)
        check(True, "it appears once collie starts playing")
        text = pg.inner_text("#npPill").replace("\n", " ")
        check("Cruel Summer" in text, "and names the track (%r)" % text[:60])

        pg.click("#npPill")
        # state="hidden", not the default "visible": waiting for a hidden element to become visible is
        # a test that can only ever time out.
        pg.wait_for_selector("#npPill", state="hidden", timeout=20000)
        check(True, "one click hides it")

        time.sleep(2)
        np = pg.request.get(BASE + "/api/desktop/nowplaying?token=" + TOKEN).json()
        check(np.get("collie") is None,
              "and the audio really stopped — not just the indicator")
        check(not errs, "still no JS errors")
        br.close()

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "now playing (web): all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
