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
from harness.verifier import VERIFIED, FAILED, INCONCLUSIVE  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


class _Rec:
    def __init__(self, args, job_id="", snapshot=None):
        self.args = args
        self.job_id = job_id
        self.snapshot = snapshot or {}


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


def test_compose_instruction_produces_deliverable_instead_of_echoing_request():
    print("test_compose_instruction_produces_deliverable_instead_of_echoing_request")

    class CapturingProvider:
        def __init__(self):
            self.calls = []

        def complete(self, system, messages, tools):
            self.calls.append((system, messages, tools))
            return type("C", (), {
                "text": "VocalCode keeps voice input local. Try it at vocalcode.app.",
                "stop_reason": "end_turn",
            })()

    provider = CapturingProvider()
    clear_registry()
    register_primitives(stub=False, provider=provider)
    cap = get_capability("compose")
    instruction = "Write a final short launch post with a link."
    rec = _Rec({"facts": "VocalCode uses local speech recognition.",
                "instruction": instruction})
    result = cap.execute(rec)
    check(len(provider.calls) == 1 and instruction in provider.calls[0][1][0]["content"],
          "compose sends the explicit instruction to the writing model")
    check(result.get("text", "").startswith("VocalCode keeps") and
          result.get("text") != instruction,
          "compose returns final copy rather than the writing instruction")
    check(cap.verify(rec, result).status == VERIFIED,
          "generated final copy verifies")

    literal = "Already final copy — publish this exactly."
    result2 = cap.execute(_Rec({"facts": "ignored", "text": literal}))
    check(result2.get("text") == literal and len(provider.calls) == 1,
          "an explicitly final literal is preserved without another model call")

    echoed = {"text": instruction}
    check(cap.verify(rec, echoed).status == FAILED,
          "an echoed writing instruction cannot be recorded as composed copy")


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
    fake = FakeActuator(page_text="Still available — can you meet locally?")
    _register(0, actuator=fake)
    cap = get_capability("web.send")
    r = cap.execute(_Rec({"url": "https://m.test/thread/1", "selector": "#msg",
                          "text": "Still available — can you meet locally?", "send": "#send", "to": "buyer"}))
    check(r.get("sent") is True, "send completed")
    check(("type", "#msg", "Still available — can you meet locally?") in fake.calls, "typed the message")
    check(("click", "#send") in fake.calls, "clicked send")
    check(r.get("confirmed") and cap.verify(_Rec({}), r).status == VERIFIED,
          "send verifies only after a fresh outgoing-thread observation")


def test_browse_and_submit_real():
    print("test_browse_and_submit_real")
    fake = FakeActuator(page_text="Post published — View post")
    fake._url = "https://example.test/compose"
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
    inspected = {"result": "Current public facts recorded", "form": [],
                 "page": {"host": "vocalcode.app", "title": "VocalCode"}}
    inspected_verdict = _browse_verify(_Rec({"read_only": True}), inspected)
    check(inspected_verdict.status == VERIFIED and inspected_verdict.evidence and
          inspected_verdict.evidence[0].channel == "browser-page-reread",
          "explicit read-only browsing verifies against the independently reread live page")
    check(_browse_verify(_Rec({}), inspected).status == INCONCLUSIVE,
          "an empty-form fill cannot masquerade as read-only success without the explicit flag")
    no_page = dict(inspected, page={})
    check(_browse_verify(_Rec({"read_only": True}), no_page).status == INCONCLUSIVE,
          "read-only browsing still fails closed without independent page identity")
    social = {"result": "drafted", "page": {"host": "x.com", "title": "Compose / X"},
              "form": [{"label": "tweetTextarea_0", "value": "Try VocalCode today"}]}
    check(_browse_verify(_Rec({"expect": {"platform": "Twitter/X",
                                          "tweet_text": "Try VocalCode today"}}), social).status
          == VERIFIED,
          "platform uses live page identity and rich editor text does not require an invented label")
    wrong_site = dict(social, page={"host": "facebook.com", "title": "Marketplace"})
    check(_browse_verify(_Rec({"expect": {"platform": "Twitter/X",
                                          "tweet_text": "Try VocalCode today"}}), wrong_site).status
          == FAILED, "semantic expectations still refute a draft on the wrong platform")

    sub = get_capability("browse.submit")
    check(sub.reversible is False and sub.risk == "publish", "browse.submit is irreversible (gated)")
    args = {"button": "Publish"}
    snap = sub.snapshot(args, "m-browser")
    sr = sub.execute(_Rec(args, job_id="m-browser", snapshot=snap))
    check(sr.get("submitted") is True and ("click_ref", "e1") in fake.calls,
          "browse.submit clicks the exact snapshotted Publish element")
    check(sr.get("confirmed") is True and sub.verify(_Rec({}), sr).status == VERIFIED,
          "browse.submit verifies only after a fresh success state")

    uncertain = FakeActuator(result_url="https://example.test/compose")
    uncertain._url = "https://example.test/compose"
    clear_registry(); register_primitives(stub=False, actuator=uncertain,
                                           browse_runner=lambda _g: "prepared")
    usub = get_capability("browse.submit")
    usnap = usub.snapshot(args, "m-uncertain")
    ur = usub.execute(_Rec(args, job_id="m-uncertain", snapshot=usnap))
    check(ur.get("submitted") and not ur.get("confirmed") and
          usub.verify(_Rec({}), ur).status == INCONCLUSIVE,
          "a click without a permalink/toast/state change is not called published")


def test_browser_snapshot_redacts_secrets_and_rejects_ambiguous_target():
    print("test_browser_snapshot_redacts_secrets_and_rejects_ambiguous_target")
    from harness.primitives import _find_button, _sanitize_form
    raw = [{"label": "Email", "value": "owner@example.test"},
           {"label": "Password", "value": "hunter2"},
           {"label": "Card number", "value": "4111111111111111"},
           {"label": "Description", "value": "A safe listing"}]
    safe = _sanitize_form(raw)
    encoded = repr(safe)
    check("owner@example.test" not in encoded and "hunter2" not in encoded and
          "4111111111111111" not in encoded and encoded.count("[redacted]") == 3,
          "credentials and signup/payment PII never enter durable form snapshots")
    collapsed = {"snapshot": '[e1] button "Publish" ×2 (identical siblings: e1–e2)'}
    check(_find_button(collapsed, "Publish") is None,
          "a collapsed row with two identical Publish buttons is ambiguous")


def test_bridge_propagates_nested_click_error_and_forces_exact_node_click():
    print("test_bridge_propagates_nested_click_error_and_forces_exact_node_click")
    from harness.webact import BridgeActuator

    class BB:
        def __init__(self): self.cmd = None
        def _call(self, cmd):
            self.cmd = cmd
            return {"ok": True, "data": {"click": {
                "error": "no live element for ref e1"}, "page": {}}}

    bb = BB()
    act = BridgeActuator.__new__(BridgeActuator)
    act._bb, act._space = bb, "mission-one"
    try:
        act.click_ref("e1")
        check(False, "a detached exact ref must raise")
    except RuntimeError:
        check(True, "nested extension click errors reach the verifier")
    check(bb.cmd.get("trusted") is False and bb.cmd.get("ref") == "e1",
          "final Mission click targets the exact live node, never stale screen coordinates")


def test_browse_domain_boundary_survives_redirects_and_blocks_action_gets():
    print("test_browse_domain_boundary_survives_redirects_and_blocks_action_gets")
    from contextlib import nullcontext
    from unittest.mock import patch
    from harness.primitives import _BoundBrowserTool
    state = {"url": "https://allowed.test/start"}

    class Inner:
        name, tier, description, schema = "browser_open", "always", "open", {}
        def run(self, _args, _ctx):
            state["url"] = "https://evil.test/collect"
            return "SECRET PAGE"

    wrapped = _BoundBrowserTool(
        Inner(), "mission-one", "open", boundary={"domains": ["allowed.test"],
                                                   "first_host": ""})
    with patch("harness.browserbridge.space_identity",
               side_effect=lambda _space: dict(state)), \
         patch("harness.browserbridge.browser_space",
               side_effect=lambda _space: nullcontext()):
        out = wrapped.run({"url": "https://allowed.test/redirect"}, None)
        check(out.startswith("ERROR") and "SECRET PAGE" not in out,
              "an allowed-domain redirect cannot expose or act on an off-scope origin")
        state["url"] = "https://allowed.test/start"
        blocked = wrapped.run({"url": "https://allowed.test/logout"}, None)
        check(blocked.startswith("ERROR") and state["url"].endswith("/start"),
              "consequential GET routes stay outside reversible browsing")


def test_code_tools_are_confined_to_the_approved_workspace():
    print("test_code_tools_are_confined_to_the_approved_workspace")
    from harness.primitives import _BoundCodeTool, _code_resource, _restrict_code_child
    root = tempfile.mkdtemp(prefix="collie-code-root-")
    outside = tempfile.mkdtemp(prefix="collie-code-outside-")

    class Tool:
        tier, description, schema = "always", "fake", {}
        def __init__(self, name): self.name, self.seen = name, []
        def provider_schema(self): return {}
        def run(self, args, _ctx): self.seen.append(dict(args)); return "ok"

    inner = Tool("write_file")
    bound = _BoundCodeTool(inner, root)
    check(bound.run({"path": os.path.join("..", os.path.basename(outside), "x")}, None)
          .startswith("ERROR"), ".. cannot escape an approved code root")
    check(bound.run({"path": os.path.join(outside, "x")}, None).startswith("ERROR"),
          "an absolute path outside the code root is refused")
    check(bound.run({"path": "inside.py"}, None) == "ok" and
          inner.seen[-1]["path"].startswith(os.path.realpath(root)),
          "an in-root path is canonicalized and delegated")

    class Registry:
        def __init__(self):
            self._tools = {n: Tool(n) for n in
                           ("read_file", "write_file", "edit_file", "grep", "glob",
                            "bash", "execute_code", "plan", "undo", "code_search")}
    h = type("H", (), {"registry": Registry()})()
    _restrict_code_child(h, root)
    check("bash" not in h.registry._tools and "execute_code" not in h.registry._tools and
          "glob" not in h.registry._tools,
          "Mission code has no general execution or symlink-traversing glob tool")
    one = _Rec({"workspace": root}); two = _Rec({"workspace": os.path.join(root, ".")})
    check(_code_resource(one) == _code_resource(two),
          "canonical workspace identity serializes concurrent code Missions")


def test_live_browse_cannot_bypass_the_outer_action_gate():
    print("test_live_browse_cannot_bypass_the_outer_action_gate")
    from unittest.mock import patch
    from harness.primitives import _live_browse

    class Obj:
        def close(self):
            pass

    class Registry:
        def __init__(self):
            class Tool:
                tier, description, schema = "always", "fake", {
                    "type": "object", "properties": {"submit": {"type": "boolean"},
                    "space": {"type": "string"}, "adopt": {"type": "boolean"}}}
                def __init__(self, name): self.name, self.calls = name, []
                def run(self, args, _ctx): self.calls.append(dict(args)); return "ok"
            self._tools = {name: Tool(name) for name in (
                "browser_open", "browser_read", "browser_fields", "browser_type",
                "browser_pick", "browser_snapshot", "browser_links", "browser_click",
                "browser_press", "browser_drag", "browser_upload", "browser_script",
                "browser_eval", "bash", "desktop_click", "desktop_type",
                "enable_capability", "mcpctl_add", "slack_send", "load_tools")}

    class FakeHarness:
        def __init__(self):
            self.registry, self.memory, self.recorder = Registry(), Obj(), Obj()
            self.answer = ""

        def run(self, _task_id, prompt):
            allowed = {"browser_open", "browser_read", "browser_snapshot", "browser_fields",
                       "browser_links", "browser_type", "browser_pick"}
            check(set(self.registry._tools) == allowed,
                  "reversible browse child is a positive browser-only authority list")
            wrapped = self.registry._tools["browser_type"]
            wrapped.run({"text": "x", "submit": True}, None)
            check(wrapped.inner.calls[-1].get("submit") is False and
                  "submit" not in wrapped.schema.get("properties", {}),
                  "browser_type cannot smuggle Enter/submit through the outer gate")
            opened = self.registry._tools["browser_open"]
            check(opened.run({"url": "https://social.test/start"}, None) == "ok" and
                  opened.run({"url": "https://evil.test/collect"}, None).startswith("ERROR"),
                  "unscoped browse is pinned to its first site against cross-site exfiltration")
            check("outer Mission can gate it" in prompt,
                  "child is told to hand consequential actions back to Mission")
            return type("R", (), {"answer": "prepared", "error": ""})()

    with patch("harness.cli.make_harness", return_value=FakeHarness()):
        check(_live_browse("prepare the form") == "prepared", "safe browse child still runs")


def test_code_primitive():
    print("test_code_primitive")
    clear_registry()
    # injected coding runner returns collie's executed-verification result
    register_primitives(stub=False,
                        code_runner=lambda g: {"answer": "fixed the null-pointer; repro passes",
                                               "verified": True})
    cap = get_capability("code")
    check(cap.reversible is True and cap.risk == "code",
          "code is reversible (VCS) -> auto-runs under the leash")
    r = cap.execute(_Rec({"goal": "fix the null-pointer in parser.py", "workspace": "/tmp/x"}))
    check(r.get("verified") is True and "fixed" in r.get("result", ""),
          "code ran the coding agent and captured its executed-verification result")
    check(cap.verify(_Rec({}), r).status == VERIFIED,
          "code VERIFIED only when the coding loop executed-verified the fix (repro RED->GREEN)")
    from harness.primitives import _code_verify
    check(_code_verify(_Rec({}), {"result": "edited, no repro", "verified": False}).status
          == INCONCLUSIVE, "an edit WITHOUT executed verification is INCONCLUSIVE, not a false 'done'")


def main():
    test_research_real()
    test_browse_and_submit_real()
    test_browser_snapshot_redacts_secrets_and_rejects_ambiguous_target()
    test_bridge_propagates_nested_click_error_and_forces_exact_node_click()
    test_browse_domain_boundary_survives_redirects_and_blocks_action_gets()
    test_code_tools_are_confined_to_the_approved_workspace()
    test_live_browse_cannot_bypass_the_outer_action_gate()
    test_code_primitive()
    test_compose_real()
    test_compose_instruction_produces_deliverable_instead_of_echoing_request()
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
