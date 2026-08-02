"""Two conversations at once, without tabs.

Collie could hold exactly one thought. `openSession` and `newThread` both began `if (running) return`
— while a run was going the sidebar was inert and the only ways out were to wait or to stop the work.
That guard was honest at the time, because a run really did die when its socket went away; the fix
that made runs outlive their window is what makes leaving one safe.

What has to be true for a second thread to be worth starting: leaving does not cancel, the list says
which threads are working without this window having to be the one watching, and a finished run
reports whether the gate actually verified it — that verdict is the whole point of running several,
since it is the only thing that says which results deserve your attention first.

    COLLIE_WEB=http://127.0.0.1:8996 COLLIE_TOKEN=<token> python3 tests/parallel_ui_check.py
"""
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8996")
TOKEN = os.environ.get("COLLIE_TOKEN", "")

_fails = []

STUB_ES = """
window.__es = [];
class FakeES {
  constructor(url) { this.url = url; this.readyState = 1; this._h = {}; this.closed = false;
                     window.__es.push(this); }
  addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); }
  removeEventListener() {}
  close() { this.readyState = 2; this.closed = true; }
  emit(type, data) {
    const e = { data: JSON.stringify(data), type };
    (this._h[type] || []).forEach(fn => fn(e));
  }
}
window.EventSource = FakeES;
"""


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        _fails.append(what)


def main():
    if not TOKEN:
        print("  COLLIE_TOKEN not set — start `collie web` and pass its token")
        return 2

    runs = {"body": '{"runs": []}'}
    # A finished run has saved its thread by the time it reports `done`, so the sessions list is
    # where the row comes from then. Modelling that here keeps the product rule honest: a session
    # that is neither saved nor running is a crash, and does not deserve a row.
    sessions = {"body": '{"sessions": []}'}
    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1280, "height": 900})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.add_init_script(STUB_ES)
        pg.route("**/api/route*", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"kind": "chat"}'))
        pg.route("**/api/runs*", lambda r: r.fulfill(
            status=200, content_type="application/json", body=runs["body"]))
        pg.route("**/api/sessions*", lambda r: r.fulfill(
            status=200, content_type="application/json", body=sessions["body"]))

        pg.goto(BASE + "/?token=" + TOKEN, wait_until="load")
        pg.wait_for_timeout(600)
        ov = pg.query_selector("#obOverlay")
        if ov and "open" in (ov.get_attribute("class") or ""):
            pg.click("#obSkip")
            pg.wait_for_timeout(300)

        pg.fill("#input", "first thing")
        pg.press("#input", "Enter")
        pg.wait_for_timeout(400)
        pg.evaluate("""() => {
            const es = window.__es.find(e => e.url.indexOf('/api/stream') > -1);
            es.emit('start', {session: 'sess-A', provider: 'mock', cwd: '/tmp', prior_turns: 0});
        }""")
        pg.wait_for_timeout(300)
        check(pg.query_selector(".msg.assistant .flow") is not None, "a first run is going")

        # Leaving must not stop it. The old guard made this click do nothing at all.
        pg.click("#newChat")
        pg.wait_for_timeout(500)
        left = pg.evaluate("""() => ({
            welcome: !!document.querySelector('#welcome'),
            liveBubble: !!document.querySelector('.msg.assistant .flow'),
            composerReady: !document.getElementById('input').disabled
        })""")
        check(left["welcome"], "starting a new thread while one runs actually leaves")
        check(not left["liveBubble"], "the window stops showing the run it walked away from")
        check(left["composerReady"], "and the composer is ready for the second thing")

        # The run is the server's; the page only stopped listening.
        runs["body"] = ('{"runs": [{"session": "sess-A", "state": "running", "started": 1, '
                        '"ask": "first thing", "cwd": "/tmp", "turns": 1, "verified": null, '
                        '"error": "", "ended": null}]}')
        pg.wait_for_timeout(3200)
        state = pg.evaluate("""() => {
            const el = document.querySelector('.thread .t-state');
            return el ? {cls: el.className, txt: el.textContent.trim()} : null;
        }""")
        check(state is not None, "the sidebar shows a thread is working")
        if state:
            check("running" in state["cls"], "marked as running (%s)" % state["txt"])

        # A finished run has to say whether the gate verified it — that is what makes several runs
        # reviewable in an order rather than all at once.
        sessions["body"] = ('{"sessions": [{"id": "sess-A", "title": "first thing", '
                            '"last": "first thing", "turns": 4}]}')
        runs["body"] = ('{"runs": [{"session": "sess-A", "state": "done", "started": 1, '
                        '"ask": "first thing", "cwd": "/tmp", "turns": 4, "verified": true, '
                        '"error": "", "ended": 2}]}')
        pg.wait_for_timeout(3200)
        done = pg.evaluate("""() => {
            const el = document.querySelector('.thread .t-state');
            return el ? {cls: el.className, txt: el.textContent.trim()} : null;
        }""")
        check(done is not None and "running" not in done["cls"],
              "and updates when it finishes (%s)" % (done or {}).get("txt"))
        check(done and done["txt"].strip() != "", "with a verdict, not a blank (%r)" % (done or {}).get("txt"))

        runs["body"] = ('{"runs": [{"session": "sess-A", "state": "failed", "started": 1, '
                        '"ask": "first thing", "cwd": "/tmp", "turns": 2, "verified": false, '
                        '"error": "boom", "ended": 3}]}')
        pg.wait_for_timeout(3200)
        failed = pg.evaluate("""() => {
            const el = document.querySelector('.thread .t-state');
            return el ? el.className : '';
        }""")
        check("failed" in failed, "a failed run is distinguishable at a glance (%s)" % failed)

        check(not errs, "no JS errors%s" % ("" if not errs else ": " + errs[0][:90]))
        br.close()

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "parallel UI: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
