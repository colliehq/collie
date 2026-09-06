"""A steer is shown where it happened, and you can always see what you just typed.

Accepted input remains visible in a durable queue. Only a model-boundary event
places it inside the answer it changes. Storage acknowledgment must not masquerade
as delivery; refused writes keep the original draft and an actionable message.

The page's script is an IIFE, so nothing is reachable to call directly. The test drives the real
composer and controls the TRANSPORT instead: a stub EventSource lets the run stay mid-flight for as
long as the assertions need, which is the one moment worth checking and the one a mock run passes
through in about a tenth of a second.

    COLLIE_WEB=http://127.0.0.1:8996 COLLIE_TOKEN=<token> python3 tests/steer_ui_check.py
"""
import json
import os
import sys

from playwright.sync_api import sync_playwright

BASE = os.environ.get("COLLIE_WEB", "http://127.0.0.1:8996")
TOKEN = os.environ.get("COLLIE_TOKEN", "")

_fails = []

# Keeps the run in flight. Records every instance so the test can push events in at will.
STUB_ES = """
window.__es = [];
class FakeES {
  constructor(url) {
    this.url = url; this.readyState = 1; this._h = {};
    window.__es.push(this);
  }
  addEventListener(t, fn) { (this._h[t] = this._h[t] || []).push(fn); }
  removeEventListener() {}
  close() { this.readyState = 2; }
  emit(type, data) {
    const e = { data: JSON.stringify(data), type };
    (this._h[type] || []).forEach(fn => fn(e));
    if (type === "message" && this.onmessage) this.onmessage(e);
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

    with sync_playwright() as p:
        br = p.chromium.launch()
        pg = br.new_page(viewport={"width": 1280, "height": 860})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.add_init_script(STUB_ES)

        # the classifying head, so send() goes straight to a stream
        pg.route("**/api/route*", lambda r: r.fulfill(
            status=200, content_type="application/json", body='{"kind": "chat"}'))
        # Storage acknowledgment and delivery at a model boundary are distinct.
        inbox = {"entries": [], "refuse": False}

        def queue_transport(route):
            if route.request.method == "POST":
                body = route.request.post_data_json
                if inbox["refuse"]:
                    return route.fulfill(status=409, content_type="application/json",
                        body=json.dumps({"error": "the request was not saved"}))
                entry = {"id": body["id"], "text": body["text"], "state": "pending",
                         "mode": "steer", "seq": len(inbox["entries"]) + 1}
                inbox["entries"].append(entry)
                out = {"accepted": True, "entry": entry}
            else:
                out = {"session": "s-steer-check", "entries": inbox["entries"],
                       "active": True, "owner_busy": True}
            route.fulfill(status=200, content_type="application/json", body=json.dumps(out))

        pg.route("**/api/task-inbox*", queue_transport)

        # Normal follow-ups now queue by default. Select the supported steering mode
        # explicitly so this suite exercises in-flight delivery rather than that queue.
        pg.goto(BASE + "/?followup=steer&token=" + TOKEN, wait_until="load")
        # Wait for the welcome overlay rather than sampling for it: it opens when the provider probe
        # answers, which is later than 600ms on a cold machine — and if it is missed, it opens over
        # the composer a moment after and the run this suite is about never starts. That failure
        # arrived as `es.emit` on undefined, twenty lines further down.
        try:
            pg.wait_for_selector("#obOverlay.open", timeout=15000)
            pg.click("#obSkip")
            pg.wait_for_selector("#obOverlay.open", state="detached", timeout=3000)
        except Exception:
            pass                    # already authed, or it never comes: either way, carry on

        pg.fill("#input", "the original question")
        pg.press("#input", "Enter")
        pg.wait_for_timeout(500)
        started = pg.evaluate("""() => {
            const es = (window.__es || []).find(e => e.url.indexOf('/api/stream') > -1);
            if (!es) return false;
            es.emit('start', {session: 's-steer-check', provider: 'mock', cwd: '/tmp', prior_turns: 0,
                              worker_capabilities: {steer: true}});
            return true;
        }""")
        check(started, "the composer opened a run stream")
        pg.wait_for_timeout(300)
        check(pg.query_selector(".msg.assistant .flow") is not None,
              "and the run has a live assistant bubble")

        # Scroll away: the steer has to bring itself back.
        pg.evaluate("""() => {
            const es = (window.__es || []).find(e => e.url.indexOf('/api/stream') > -1);
            for (let i = 0; i < 60; i++) es.emit('token', {t: 'filler line ' + i + '\\n\\n'});
            const s = document.getElementById('scroll');
            s.scrollTo({top: 0, behavior: 'instant'});
        }""")
        pg.wait_for_timeout(300)
        check(pg.evaluate("() => document.getElementById('scroll').scrollTop < 50"),
              "scrolled up, away from the live run")

        pg.fill("#input", "actually, use the other endpoint")
        pg.press("#input", "Enter")
        pg.wait_for_timeout(700)

        check(pg.input_value("#input") == "", "the draft clears after the durable acknowledgment")
        check(pg.query_selector(".flow .steer-note") is None,
              "saved input is not labeled delivered before a model boundary")
        check("actually, use the other endpoint" in pg.locator("#taskQueue").inner_text(),
              "the accepted correction is visible in the durable queue")
        if inbox["entries"]:
            entry = inbox["entries"][0]
            entry["state"] = "consumed"
            pg.evaluate("""data => {
                const es = window.__es.find(e => e.url.indexOf('/api/stream') > -1);
                es.emit('steer', data);
            }""", {"session": "s-steer-check", "id": entry["id"], "text": entry["text"]})
        pg.wait_for_timeout(200)
        note = pg.query_selector(".flow .steer-note")
        check(note is not None, "the steer lands inside the run's own flow, not after it")
        if note:
            check("actually, use the other endpoint" in (note.inner_text() or ""),
                  "and carries what was typed")
            pos = pg.evaluate("""() => {
                const n = document.querySelector('.flow .steer-note');
                const f = n.closest('.flow');
                const kids = Array.prototype.slice.call(f.children);
                const stat = f.querySelector('.thinking');
                return {note: kids.indexOf(n), status: stat ? kids.indexOf(stat) : -1,
                        inTurn: !!n.closest('.msg.assistant')};
            }""")
            check(pos["inTurn"], "it is inside the assistant turn it interrupted")
            check(pos["status"] == -1 or pos["note"] < pos["status"],
                  "and above the status line, where the next segment goes")
            check(pg.query_selector(".msg.steer") is None,
                  "nothing was appended below the answer any more")

        note_class = note.get_attribute("class") if note else ""
        check(note is not None and "pending" not in note_class,
              "and the model boundary confirms delivery")

        # A refused write stays in the composer and never claims model delivery.
        inbox["refuse"] = True
        before = len(inbox["entries"])
        pg.fill("#input", "and rename the flag")
        pg.press("#input", "Enter")
        pg.wait_for_timeout(700)
        check(pg.input_value("#input") == "and rename the flag",
              "an unacknowledged instruction stays in the draft")
        check("the request was not saved" in pg.locator("#taskQueue").inner_text(),
              "the refusal is visible beside the pending requests")
        check(len(inbox["entries"]) == before and pg.locator(".steer-note").count() == 1,
              "refusal creates neither a queued request nor a delivery claim")

        check(not errs, "no JS errors%s" % ("" if not errs else ": " + errs[0][:90]))
        br.close()

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "steer UI: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
