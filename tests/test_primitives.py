"""Pin the REAL neutral primitives (harness.primitives, stub=False) with injected
fakes + a localhost fixture — no live browser, model, or network needed.

Run: python tests/test_primitives.py   (exit 0 = all green)

Proves each real body drives the right dependency and verifies honestly:
  - research  -> collie's browser research (injected runner) + the cited-answer gate
  - compose   -> the model (injected provider) turns facts into text
  - observe   -> a logged-out fetch through the independent channel (fixture)
  - web.submit-> drives the actuator (open/type/click) then VERIFIES via an
                 independent logged-out re-fetch of the resulting URL (fixture)
  - web.send  -> drives the actuator; sent, honestly not claimed as read
  - no browser-> web.submit degrades to a clean 'no browser' FAILED, never a crash
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["COLLIE_WEBFETCH_ALLOW_LOCAL"] = "1"     # allow the loopback fixture through the SSRF guard
os.environ["COLLIE_NOTES_DIR"] = tempfile.mkdtemp() # keep research reports out of ~/.collie

from harness.jobs import clear_registry, get_capability  # noqa: E402
from harness.primitives import register_primitives  # noqa: E402
from harness.webact import FakeActuator  # noqa: E402
from harness.verifier import VERIFIED, FAILED  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


class _Rec:
    def __init__(self, args):
        self.args = args


class _MockProvider:
    text = "2018 Toyota Corolla · 60k mi · one owner. $7700, local pickup, cash."

    def complete(self, system, messages, tools):
        return type("C", (), {"text": self.text, "stop_reason": "end_turn"})()


# a localhost fixture: /listing shows the published item; /src is a citeable source
class _Fix(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/listing"):
            body = b"<html><body><h1>2018 Toyota Corolla</h1><p>Price: $7700</p></body></html>"
        elif self.path.startswith("/src"):
            body = b"<html><body>comps: corollas around 7500-8000</body></html>"
        else:
            body = b"<html><body>ok</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Fix)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _register(port, actuator=None):
    clear_registry()
    runner = lambda q: (f"Fair asking price is about $7700 based on comparable listings.\n\n"
                        f"Sources:\n- http://127.0.0.1:{port}/src")
    register_primitives(stub=False, actuator=actuator, provider=_MockProvider(),
                        research_runner=runner)


def test_research_real():
    print("test_research_real")
    httpd, port = _server()
    _register(port)
    cap = get_capability("research")
    r = cap.execute(_Rec({"query": "price for a 2018 corolla"}))
    check(r.get("answer") and r.get("citations"), "research returns an answer with citations")
    check(cap.verify(_Rec({}), r).status == VERIFIED, "the cited-answer gate passes a real sourced answer")
    httpd.shutdown()


def test_compose_real():
    print("test_compose_real")
    _register(0)
    cap = get_capability("compose")
    r = cap.execute(_Rec({"facts": {"make": "Toyota", "year": 2018}}))
    check(_MockProvider.text in r.get("text", ""), "compose used the model to produce text")
    check(cap.verify(_Rec({}), r).status == VERIFIED, "composed text verifies")


def test_observe_loggedout_real():
    print("test_observe_loggedout_real")
    httpd, port = _server()
    _register(port)
    cap = get_capability("observe")
    r = cap.execute(_Rec({"url": f"http://127.0.0.1:{port}/listing", "expect": "Corolla"}))
    check(r.get("present") is True and r.get("channel") == "logged-out-fetch",
          "observe found the expected text through the independent logged-out channel")
    miss = cap.execute(_Rec({"url": f"http://127.0.0.1:{port}/listing", "expect": "Ferrari"}))
    check(miss.get("present") is False, "observe reports absence honestly")
    httpd.shutdown()


def test_web_submit_real_drives_and_verifies():
    print("test_web_submit_real_drives_and_verifies")
    httpd, port = _server()
    fake = FakeActuator(result_url=f"http://127.0.0.1:{port}/listing")
    _register(port, actuator=fake)
    cap = get_capability("web.submit")
    r = cap.execute(_Rec({"url": f"http://127.0.0.1:{port}/new",
                          "fields": {"#title": "2018 Toyota Corolla", "#price": "7700"},
                          "submit": "#post", "expect_title": "2018 Toyota Corolla"}))
    check(r.get("submitted") is True and r.get("url", "").endswith("/listing"), "submit completed with a url")
    check(("open", f"http://127.0.0.1:{port}/new") in fake.calls, "actuator navigated to the form")
    check(any(c[0] == "type" for c in fake.calls) and ("click", "#post") in fake.calls,
          "actuator filled the fields and clicked submit")
    v = cap.verify(_Rec({}), r)
    check(v.status == VERIFIED, f"independent logged-out re-fetch confirms the listing, got {v.status}: {v.reason}")
    httpd.shutdown()


def test_web_submit_no_browser_degrades():
    print("test_web_submit_no_browser_degrades")
    # point the bridge probe at a dead port so get_actuator() sees no live browser
    # (this box may have a real bridge on the default port), forcing the degrade path.
    os.environ["COLLIE_BROWSER_BRIDGE_PORT"] = "1"
    os.environ["COLLIE_BROWSER_BRIDGE_NOSPAWN"] = "1"
    _register(0, actuator=None)             # no actuator + no live bridge -> clean failure
    cap = get_capability("web.submit")
    r = cap.execute(_Rec({"url": "https://x.test/new", "fields": {}, "submit": "#go"}))
    # either no browser at all (degrades) OR playwright is present and it errors on x.test — both FAILED, no crash
    check(r.get("submitted") is not True, "no live submit without a real browser/session")
    check(cap.verify(_Rec({}), r).status == FAILED, "a non-submit verifies as FAILED, not a crash")


def test_web_send_real_drives():
    print("test_web_send_real_drives")
    fake = FakeActuator()
    _register(0, actuator=fake)
    cap = get_capability("web.send")
    r = cap.execute(_Rec({"url": "https://m.test/thread/1", "selector": "#msg",
                          "text": "Still available — can you meet locally?", "send": "#send", "to": "buyer"}))
    check(r.get("sent") is True, "send completed")
    check(("type", "#msg", "Still available — can you meet locally?") in fake.calls, "typed the message")
    check(("click", "#send") in fake.calls, "clicked send")
    check(cap.verify(_Rec({}), r).status == VERIFIED, "send verifies as sent")


def test_browse_and_submit_real():
    print("test_browse_and_submit_real")
    fake = FakeActuator()
    clear_registry()
    register_primitives(stub=False, actuator=fake,
                        browse_runner=lambda goal: "Filled the Corolla listing "
                        "(Year/Make/Model/Price/Description); ready to Publish.")
    cap = get_capability("browse")
    r = cap.execute(_Rec({"goal": "fill a Marketplace listing for a 2015 Corolla"}))
    check("Filled the Corolla" in r.get("result", ""),
          "browse ran the (injected) agent loop and returned its result")
    check(cap.reversible is True and cap.risk == "read", "browse is reversible (fills, no submit)")

    # rigorous verify: an INDEPENDENT re-read of the form, not the agent's say-so
    from harness.primitives import _browse_verify
    form = [{"label": "Make", "value": "Toyota"}, {"label": "Model", "value": "Corolla"},
            {"label": "Price", "value": "$9,500"}, {"label": "Year", "value": "Year 2015"}]
    res = {"result": "done", "form": form}
    check(_browse_verify(_Rec({"expect": {"Make": "Toyota", "Price": "9500", "Year": "2015"}}), res).status
          == VERIFIED, "expect values found in the re-read form -> VERIFIED")
    check(_browse_verify(_Rec({"expect": {"Make": "Honda"}}), res).status == FAILED,
          "a value ABSENT from the re-read form -> FAILED (refutes a false 'done')")
    check(_browse_verify(_Rec({"expect": {"Make": "Toyota"}}), {"result": "done", "form": []}).status
          == FAILED, "'done' over an empty form is refuted, not trusted")
    check(_browse_verify(_Rec({}), res).status == VERIFIED, "no expect + substantially filled -> VERIFIED")

    sub = get_capability("browse.submit")
    check(sub.reversible is False and sub.risk == "publish", "browse.submit is irreversible (gated)")
    sr = sub.execute(_Rec({"button": "Publish"}))
    check(sr.get("submitted") is True and ("click_text", "Publish") in fake.calls,
          "browse.submit clicks the Publish button by text")
    check(sub.verify(_Rec({}), sr).status == VERIFIED, "browse.submit verifies as clicked")


def main():
    test_research_real()
    test_browse_and_submit_real()
    test_compose_real()
    test_observe_loggedout_real()
    test_web_submit_real_drives_and_verifies()
    test_web_submit_no_browser_degrades()
    test_web_send_real_drives()
    if _fails:
        print(f"\n{len(_fails)} FAILED")
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
