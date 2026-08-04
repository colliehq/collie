"""The browser tools' side of the bridge: batching, spaces, and the warnings that must not be lost.

The extension half is covered by tests/browser_ext_test.js. This half stubs `_call` — the one
localhost round trip — and checks what the TOOLS do with what comes back, because that is where the
lessons of the Reddit launch live: a partly-run script, a cut-off snapshot, a frame that could not be
read, a tab that belongs to someone else. Every one of those has to reach the model as a warning it
cannot mistake for success.

    python tests/test_browserbridge.py
"""
import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import browserbridge as bb   # noqa: E402

_fails = []
_ran = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    _ran.append(msg)
    if not cond:
        _fails.append(msg)


class Stub:
    """Stand in for the bridge round trip: record what was sent, reply with what we choose."""

    def __init__(self, reply):
        self.reply = reply
        self.sent = []

    def __call__(self, cmd, timeout=60):
        self.sent.append(dict(cmd, _timeout=timeout))
        r = self.reply(cmd) if callable(self.reply) else self.reply
        return r


def with_stub(reply):
    stub = Stub(reply)
    bb._call = stub
    return stub


def ok(data):
    return {"ok": True, "data": data}


CTX = types.SimpleNamespace(cwd=".", project="t", images=[])


# --- spaces: two runs, two tabs -------------------------------------------------------------------
class Wire:
    """Stub the localhost round trip itself, so the REAL _call runs. Stubbing _call would skip the
    very line under test — the one that stamps the space onto every command."""

    def __init__(self, reply):
        self.reply = reply
        self.sent = []

    def urlopen(self, req, timeout=None):
        self.sent.append(json.loads((getattr(req, "data", None) or b"{}").decode()))
        payload = json.dumps(self.reply).encode()

        class R:
            def read(self):
                return payload

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        return R()


def on_the_wire(fn, reply=None):
    """Run `fn()` with the bridge's transport stubbed; return the commands that were sent."""
    wire = Wire(reply if reply is not None else {"ok": True, "data": "page text"})
    real_open, real_ensure = bb.urllib.request.urlopen, bb._ensure_server
    bb.urllib.request.urlopen = wire.urlopen
    bb._ensure_server = lambda port: True
    try:
        fn()
    finally:
        bb.urllib.request.urlopen = real_open
        bb._ensure_server = real_ensure
    return wire.sent


def test_space_is_attached_to_every_command():
    try:
        bb._CURRENT_SPACE[0] = None
        os.environ.pop("COLLIE_BROWSER_SPACE", None)
        sent = on_the_wire(lambda: bb.BrowserRead().run({}, CTX))
        check(sent and sent[0].get("space") == "default", "commands carry a space (default)")

        os.environ["COLLIE_BROWSER_SPACE"] = "apply-job"
        sent = on_the_wire(lambda: bb.BrowserRead().run({}, CTX))
        check(sent and sent[0].get("space") == "apply-job",
              "COLLIE_BROWSER_SPACE gives a concurrent run its own lane")
        os.environ.pop("COLLIE_BROWSER_SPACE", None)

        sent = on_the_wire(lambda: bb.BrowserOpen().run(
            {"url": "https://example.com", "space": "research"}, CTX))
        check(sent and sent[0].get("space") == "research", "browser_open can name the space")
        sent = on_the_wire(lambda: bb.BrowserRead().run({}, CTX))
        check(sent and sent[0].get("space") == "research",
              "and the space sticks for the rest of the run")
    finally:
        bb._CURRENT_SPACE[0] = None
        os.environ.pop("COLLIE_BROWSER_SPACE", None)


def test_open_does_not_adopt_unless_asked():
    real = bb._call
    try:
        stub = with_stub(ok("page text"))
        bb.BrowserOpen().run({"url": "https://example.com"}, CTX)
        check(stub.sent[0].get("adopt") is False,
              "browser_open does NOT take over the user's tab by default")
        stub = with_stub(ok("page text"))
        bb.BrowserOpen().run({"url": "https://example.com", "adopt": True}, CTX)
        check(stub.sent[0].get("adopt") is True, "adopt=true is passed through when asked for")
    finally:
        bb._call = real


def test_tabs_tool_lists_and_routes():
    real = bb._call
    try:
        stub = with_stub(ok({"spaces": [{"space": "default", "tab_id": 7, "owned": True,
                                         "title": "Inbox", "url": "https://mail.example.com"},
                                        {"space": "apply", "tab_id": 9, "owned": False,
                                         "title": "Ashby", "url": "https://jobs.ashbyhq.com/x"}],
                             "current": "default"}))
        out = bb.BrowserTabs().run({}, CTX)
        check("default" in out and "apply" in out, "browser_tabs lists every space")
        check("no (yours)" in out, "a tab collie did not open is marked as the user's")
        check("yes" in out, "a tab collie opened is marked as its own")

        stub = with_stub(ok({"attached": True, "space": "default", "url": "https://x.test"}))
        bb.BrowserTabs().run({"action": "attach"}, CTX)
        check(stub.sent[0]["action"] == "attach", "attach routes to the attach action")

        stub = with_stub(ok({"released": True, "closed": False}))
        bb.BrowserTabs().run({"action": "release", "close": True}, CTX)
        check(stub.sent[0]["action"] == "release" and stub.sent[0]["close"] is True,
              "release passes close through")
    finally:
        bb._CURRENT_SPACE[0] = None
        bb._call = real


# --- browser_script: the batch ---------------------------------------------------------------------
def test_script_rejects_nonsense_before_touching_the_browser():
    real = bb._call
    try:
        stub = with_stub(ok({"ok": True, "ran": 0, "of": 0, "steps": []}))
        t = bb.BrowserScript()
        check("ERROR" in t.run({"steps": []}, CTX), "an empty script is refused")
        check("ERROR" in t.run({"steps": "click"}, CTX), "a non-list is refused")
        check("ERROR" in t.run({"steps": [{"url": "x"}]}, CTX), "a step with no action is refused")
        check("ERROR" in t.run({"steps": [{"action": "fly"}]}, CTX), "an unknown action is refused")
        check("ERROR" in t.run({"steps": [{"action": "click"}] * 41}, CTX), "41 steps is refused")
        out = t.run({"steps": [{"action": "upload", "path": "x"}]}, CTX)
        check("browser_upload" in out, "upload as a step points at the real tool")
        check(not stub.sent, "none of those reached the browser")
    finally:
        bb._call = real


def test_script_reports_a_clean_run():
    real = bb._call
    try:
        with_stub(ok({"ok": True, "ran": 3, "of": 3, "result": "the final page text",
                      "steps": [{"step": 1, "action": "open", "ok": True},
                                {"step": 2, "action": "type", "ok": True, "typed": "Sining",
                                 "landed": True},
                                {"step": 3, "action": "read", "ok": True}]}))
        out = bb.BrowserScript().run({"steps": [{"action": "open", "url": "https://e.test"},
                                                {"action": "type", "ref": "e1", "text": "Sining"},
                                                {"action": "read"}]}, CTX)
        check("3/3 steps ran" in out, "a clean run says so")
        check("landed=True" in out, "the read-back result of each write is visible")
        check("the final page text" in out, "the last step's payload comes back in full")
        check("UNTRUSTED WEB CONTENT" in out, "page content stays fenced as data")
    finally:
        bb._call = real


def test_script_that_stopped_half_way_is_an_error_not_a_summary():
    real = bb._call
    try:
        with_stub(ok({"ok": False, "ran": 2, "of": 5, "stopped_at": 2,
                      "result": {"typed": "hello", "landed": False, "value": ""},
                      "steps": [{"step": 1, "action": "open", "ok": True},
                                {"step": 2, "action": "type", "ok": False, "landed": False,
                                 "error": "the text did not land"}]}))
        out = bb.BrowserScript().run({"steps": [{"action": "open", "url": "https://e.test"},
                                                {"action": "type", "ref": "e1", "text": "hello"},
                                                {"action": "click", "ref": "e2"},
                                                {"action": "wait", "ms": 500},
                                                {"action": "read"}]}, CTX)
        check(out.startswith("ERROR(browser)"), "a half-run script is an ERROR, not a report")
        check("stopped at step 2 of 5" in out.lower(), "it says exactly where it stopped")
        check("did NOT run" in out, "it says the rest did not happen")
        check("browser_snapshot" in out, "it says how to find out where the page actually is")
    finally:
        bb._call = real


def test_script_timeout_grows_with_the_number_of_steps():
    real = bb._call
    try:
        stub = with_stub(ok({"ok": True, "ran": 1, "of": 1, "steps": [], "result": ""}))
        bb.BrowserScript().run({"steps": [{"action": "wait", "ms": 100}]}, CTX)
        one = stub.sent[0]["_timeout"]
        stub = with_stub(ok({"ok": True, "ran": 8, "of": 8, "steps": [], "result": ""}))
        bb.BrowserScript().run({"steps": [{"action": "wait", "ms": 100}] * 8}, CTX)
        many = stub.sent[0]["_timeout"]
        check(many > one, "a longer script gets a longer budget (%ss vs %ss)" % (one, many))
        check(many <= 600, "but never an unbounded one")
    finally:
        bb._call = real


# --- snapshot: every way it can be partial has to be said out loud ------------------------------------
def test_snapshot_says_when_it_was_cut():
    real = bb._call
    try:
        with_stub(ok({"count": 200, "truncated": True, "dropped": 46, "frames": 0,
                      "snapshot": "[e1] button \"Go\""}))
        out = bb.BrowserSnapshot().run({"max": 200}, CTX)
        check("WARNING" in out, "a cut list is announced")
        check("46" in out, "it says how many were dropped")
        check("importance" in out, "it says the cut was by importance, not document order")
    finally:
        bb._call = real


def test_snapshot_mentions_unread_cross_origin_frames():
    real = bb._call
    try:
        with_stub(ok({"count": 3, "truncated": False, "frames": 2, "snapshot": "[e1] button \"Go\""}))
        out = bb.BrowserSnapshot().run({}, CTX)
        check("frames=true" in out, "an unread cross-origin frame points at the way in")
        check("2 cross-origin iframe" in out, "and says how many there are")
    finally:
        bb._call = real


def test_snapshot_never_passes_off_a_failed_frame_read_as_the_page():
    real = bb._call
    try:
        with_stub(ok({"count": 3, "truncated": False, "frames_error": "Debugger is not attached",
                      "snapshot": "[e1] button \"Go\""}))
        out = bb.BrowserSnapshot().run({"frames": True}, CTX)
        check("WARNING" in out and "could NOT be read" in out,
              "a failed frame read is a warning, not a silent top-document result")
        check("Debugger is not attached" in out, "with the reason it failed")
    finally:
        bb._call = real


def test_snapshot_passes_its_options_through():
    real = bb._call
    try:
        stub = with_stub(ok({"count": 1, "snapshot": "[e1] button", "truncated": False}))
        bb.BrowserSnapshot().run({"max": 600, "text": True, "frames": True}, CTX)
        sent = stub.sent[0]
        check(sent["max"] == 600 and sent["text"] is True and sent["frames"] is True,
              "max / text / frames reach the extension")
        check(sent["_timeout"] >= 90, "reaching into frames is given longer than a plain snapshot")
    finally:
        bb._call = real


# --- the older guarantees must still hold ------------------------------------------------------------
def test_type_that_did_not_land_is_still_a_hard_error():
    real = bb._call
    try:
        with_stub(ok({"typed": "hello", "landed": False, "value": ""}))
        out = bb.BrowserType().run({"ref": "e1", "text": "hello"}, CTX)
        check(out.startswith("ERROR(browser)"), "a write that went nowhere is still an ERROR")
    finally:
        bb._call = real


def test_ambiguous_click_still_warns():
    real = bb._call
    try:
        with_stub(ok({"click": {"clicked": "Save", "matches": 4, "candidates": ["Save", "Save"]},
                      "page": "after"}))
        out = bb.BrowserClick().run({"text": "Save"}, CTX)
        check("WARNING" in out and "4 elements matched" in out,
              "clicking the first of several still says so")
    finally:
        bb._call = real


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("\n== browserbridge tools: %d/%d checks passed ==%s"
          % (len(_ran) - len(_fails), len(_ran),
             "" if not _fails else " FAILS: " + "; ".join(_fails)))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
