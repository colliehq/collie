"""A steer is shown where it happened, and you can always see what you just typed.

Two things the web transcript got wrong about mid-run messages.

The message was appended to the log, and the log already ended with the assistant bubble that was
still growing. So a run you interrupted read back as "you asked → Collie answered → you interrupted",
with the interruption sitting under the answer it had already changed. Order is the only thing a
transcript is for.

And the scroll: starting a run forces the view to the bottom, steering did not. Scroll up to read
what happened earlier, type a correction, and your own words stay off-screen — indistinguishable
from a message that never sent.

The page's script is an IIFE, so nothing is reachable to call directly. The test drives the real
composer and controls the TRANSPORT instead: a stub EventSource lets the run stay mid-flight for as
long as the assertions need, which is the one moment worth checking and the one a mock run passes
through in about a tenth of a second.

    COLLIE_WEB=http://127.0.0.1:8996 COLLIE_TOKEN=<token> python3 tests/steer_ui_check.py
"""
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
        steer_reply = {"body": '{"queued": true}'}
        pg.route("**/api/steer*", lambda r: r.fulfill(
            status=200, content_type="application/json", body=steer_reply["body"]))

        pg.goto(BASE + "/?token=" + TOKEN, wait_until="load")
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
            es.emit('start', {session: 's-steer-check', provider: 'mock', cwd: '/tmp', prior_turns: 0});
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

        check(pg.evaluate("() => document.getElementById('scroll').scrollTop > 50"),
              "typing it scrolled the view back to it")
        check("pending" not in (pg.get_attribute(".flow .steer-note", "class") or ""),
              "and it stops saying 'queued' once the desktop confirms")

        # A run that ended first must say so on the note itself, not only in a passing event line.
        steer_reply["body"] = '{"queued": false}'
        pg.fill("#input", "and rename the flag")
        pg.press("#input", "Enter")
        pg.wait_for_timeout(700)
        last = pg.evaluate("""() => {
            const all = document.querySelectorAll('.flow .steer-note');
            const n = all[all.length - 1];
            return n ? {cls: n.className, tag: n.querySelector('.sn-tag').textContent.trim()} : null;
        }""")
        check(last is not None and "dropped" in last["cls"],
              "an undelivered steer is marked on the message itself (%s)" % (last or {}).get("cls"))

        check(not errs, "no JS errors%s" % ("" if not errs else ": " + errs[0][:90]))
        br.close()

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "steer UI: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
