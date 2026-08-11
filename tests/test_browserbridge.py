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
import threading
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


def test_reversible_advance_uses_only_an_exact_ref_and_surfaces_refusal():
    real = bb._call
    try:
        stub = with_stub(ok({"advance": {"allowed": True, "clicked": "Company"}}))
        out = bb.BrowserAdvance().run({"ref": "e16"}, CTX)
        check(stub.sent[0]["action"] == "advance" and stub.sent[0]["ref"] == "e16",
              "reversible advance sends only the exact snapshotted ref")
        check("Company" in out, "an allowed reversible step is reported")
        with_stub(ok({"advance": {"error": "consequential control requires the outer Mission gate"}}))
        refused = bb.BrowserAdvance().run({"ref": "e9"}, CTX)
        check(refused.startswith("ERROR(browser)"),
              "an extension-side consequential-target refusal remains a hard error")
    finally:
        bb._call = real


# --- the rest of a hand: keys, hover, drag, a bare point ----------------------------------------------
def test_press_passes_key_and_modifiers_through():
    real = bb._call
    try:
        stub = with_stub(ok({"pressed": "Escape", "trusted": True, "times": 1}))
        out = bb.BrowserPress().run({"key": "Escape"}, CTX)
        check(stub.sent[0]["action"] == "press" and stub.sent[0]["key"] == "Escape",
              "browser_press sends the key")
        check("Escape" in out, "and reports what was pressed")
        stub = with_stub(ok({"pressed": "a", "trusted": True, "times": 2}))
        bb.BrowserPress().run({"key": "a", "modifiers": ["ctrl"], "repeat": 2}, CTX)
        check(stub.sent[0]["modifiers"] == ["ctrl"] and stub.sent[0]["repeat"] == 2,
              "modifiers and repeat reach the browser")
    finally:
        bb._call = real


def test_hover_targets_the_same_ways_a_click_does():
    real = bb._call
    try:
        stub = with_stub(ok({"hovered": "Products", "trusted": True}))
        bb.BrowserHover().run({"text": "Products"}, CTX)
        check(stub.sent[0]["action"] == "hover" and stub.sent[0]["text"] == "Products",
              "browser_hover takes visible text")
        stub = with_stub(ok({"hovered": "e5", "trusted": True}))
        bb.BrowserHover().run({"ref": "e5"}, CTX)
        check(stub.sent[0]["ref"] == "e5", "and a snapshot ref")
    finally:
        bb._call = real


def test_drag_refuses_endpoints_it_cannot_use():
    real = bb._call
    try:
        stub = with_stub(ok({"dragged": "pointer"}))
        t = bb.BrowserDrag()
        check("ERROR" in t.run({"from": "e1", "to": "e2"}, CTX),
              "endpoints given as bare strings are refused")
        check(not stub.sent, "and nothing was sent to the browser")
        out = t.run({"from": {"ref": "e1"}, "to": {"x": 100, "y": 200}}, CTX)
        check(stub.sent[0]["from"] == {"ref": "e1"} and stub.sent[0]["to"] == {"x": 100, "y": 200},
              "an element and a point are both valid endpoints")
        check("not the same as it having had an effect" in out,
              "a completed drag does NOT claim the page changed — that needs a snapshot")
    finally:
        bb._call = real


def test_click_can_take_a_bare_point():
    real = bb._call
    try:
        stub = with_stub(ok({"click": {"clicked_at": [400, 300], "trusted": True,
                                       "hit": {"at": "canvas"}}, "page": "…"}))
        bb.BrowserClick().run({"x": 400, "y": 300}, CTX)
        check(stub.sent[0]["x"] == 400 and stub.sent[0]["y"] == 300,
              "browser_click passes coordinates through for a canvas-style target")
    finally:
        bb._call = real


def test_the_new_actions_are_script_steps_too():
    real = bb._call
    try:
        stub = with_stub(ok({"ok": True, "ran": 3, "of": 3, "steps": [], "result": ""}))
        out = bb.BrowserScript().run({"steps": [{"action": "hover", "text": "Menu"},
                                                {"action": "press", "key": "ArrowDown"},
                                                {"action": "drag", "from": {"ref": "e1"},
                                                 "to": {"ref": "e2"}}]}, CTX)
        check("ERROR" not in out, "hover / press / drag are accepted as script steps")
        check(len(stub.sent) == 1, "and the whole sequence is ONE round trip")
    finally:
        bb._call = real


# --- the token: what it does, and what it must not pretend to do -------------------------------------
def _isolated_home(fn):
    """Run with a throwaway collie home, so a test never reads or writes the real token."""
    import tempfile
    real = bb._home
    with tempfile.TemporaryDirectory() as d:
        bb._home = lambda: d
        try:
            return fn(d)
        finally:
            bb._home = real


def test_token_is_made_once_and_kept():
    def body(home):
        os.environ.pop("COLLIE_BRIDGE_DANGEROUSLY_OMIT_AUTH", None)
        first = bb.token()
        check(len(first) >= 32, "a fresh token is long enough to be worth having (%d chars)" % len(first))
        check(bb.token() == first, "asking twice returns the SAME token, not a new one")
        check(os.path.isfile(os.path.join(home, "bridge-token")),
              "it is stored in collie's state dir, not in the repo (where a commit could take it)")
    _isolated_home(body)


def test_token_gate_accepts_only_the_real_one():
    def body(home):
        os.environ.pop("COLLIE_BRIDGE_DANGEROUSLY_OMIT_AUTH", None)
        good = bb.token()
        check(bb._token_ok({"Authorization": "Bearer " + good}) is True, "the right token is accepted")
        check(bb._token_ok({"Authorization": "Bearer " + good + "x"}) is False, "a near-miss is not")
        check(bb._token_ok({}) is False, "no Authorization header at all is not")
        check(bb._token_ok({"Authorization": good}) is False, "and neither is the token without Bearer")
        check(bb._token_ok({"authorization": "bearer " + good}) is True,
              "header name and scheme are case-insensitive, as HTTP says")
    _isolated_home(body)


def test_auth_can_be_switched_off_only_loudly():
    def body(home):
        os.environ["COLLIE_BRIDGE_DANGEROUSLY_OMIT_AUTH"] = "1"
        try:
            check(bb.auth_off() is True and bb._token_ok({}) is True,
                  "the escape hatch really does disable the check")
        finally:
            os.environ.pop("COLLIE_BRIDGE_DANGEROUSLY_OMIT_AUTH", None)
        check("dangerously" in "COLLIE_BRIDGE_DANGEROUSLY_OMIT_AUTH".lower(),
              "and it is named so nobody turns it on by accident")
    _isolated_home(body)


# --- the audit log: what happened, without recording the secrets it happened with ----------------------
def test_audit_records_the_action_but_never_the_typed_text():
    def body(home):
        summary = bb._audit_summary({"action": "type", "space": "apply", "ref": "e7",
                                     "text": "hunter2-my-actual-password"})
        blob = json.dumps(summary)
        check("hunter2" not in blob, "the typed text is NOT in the audit record")
        check(summary.get("text_len") == 26, "only its length is (%s)" % summary.get("text_len"))
        check(summary.get("action") == "type" and summary.get("ref") == "e7",
              "the action and its target are")

        up = bb._audit_summary({"action": "upload", "files": [{"name": "cv.pdf", "data": "AAAA"}]})
        check(json.dumps(up).count("AAAA") == 0, "uploaded file CONTENT is not logged")
        check(up.get("files") == ["cv.pdf"], "its name is")

        sc = bb._audit_summary({"action": "script", "steps": [{"action": "type", "text": "secret"},
                                                              {"action": "click", "ref": "e1"}]})
        check("secret" not in json.dumps(sc), "a script's step text is not logged either")
        check(sc.get("steps") == ["type", "click"], "only the shape of the script is")
    _isolated_home(body)


def test_audit_writes_one_line_per_command_with_its_outcome():
    def body(home):
        bridge = bb._Bridge()

        def answer():
            cmd = bridge.next_cmd(wait=5)
            if cmd:
                bridge.deliver(cmd["id"], {"error": "no element for e9"})

        t = threading.Thread(target=answer)
        t.start()
        bridge.enqueue({"action": "click", "ref": "e9", "space": "s1"}, timeout=10)
        t.join()

        path = os.path.join(home, "bridge-audit.log")
        lines = [json.loads(x) for x in open(path, encoding="utf-8").read().splitlines() if x.strip()]
        check(len(lines) == 1, "one command wrote exactly one audit line (got %d)" % len(lines))
        rec = lines[0] if lines else {}
        check(rec.get("action") == "click" and rec.get("ref") == "e9", "with the action and target")
        check(rec.get("outcome") == "error", "and the outcome, not just the intent")
        check("at" in rec and "took_ms" in rec, "and when it happened / how long it took")
    _isolated_home(body)


def test_audit_records_a_command_the_browser_never_answered():
    def body(home):
        bridge = bb._Bridge()
        bridge.enqueue({"action": "read", "space": "s1"}, timeout=1)   # nobody is polling
        path = os.path.join(home, "bridge-audit.log")
        lines = [json.loads(x) for x in open(path, encoding="utf-8").read().splitlines() if x.strip()]
        check(len(lines) == 1 and lines[0].get("outcome") == "timeout",
              "a command that timed out is logged as such, not lost")
    _isolated_home(body)


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
