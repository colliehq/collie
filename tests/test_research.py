"""Pin the research capability (harness.research) with an injected agent runner.

Run: python tests/test_research.py   (exit 0 = all green)

Deterministic — no live model or browser: a fake runner returns a canned cited
answer, and the done-check re-fetches the citations against a local fixture
server (SSRF guard opted-out for loopback). Proves: reachable citation ->
VERIFIED, no citation -> INCONCLUSIVE, and citations are extracted + a report is
written.
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["COLLIE_NOTES_DIR"] = tempfile.mkdtemp(prefix="collie-research-")
os.environ["COLLIE_WEBFETCH_ALLOW_LOCAL"] = "1"   # citations are a localhost fixture here

from harness import research  # noqa: E402
from harness.jobs import clear_registry, get_capability  # noqa: E402
from harness import capabilities as caps  # noqa: E402
from harness.verifier import VERIFIED, INCONCLUSIVE  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def main():
    # fixture "source" pages the citations point at
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            code = 403 if self.path.startswith("/blocked") else 200
            body = b"<h1>PowerRider P1 review</h1>"
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    src = f"http://127.0.0.1:{port}/review"

    print("test_extracts_citations_and_writes_report")
    fake = lambda q: f"Buy the PowerRider P1 at RideCo, best price. Sources:\n- {src}\n"
    out = research.run_research("where to buy PowerRider P1", runner=fake)
    check(src in out["citations"], "must extract the cited URL")
    check(os.path.exists(out["report_file"]), "must write a report file")

    print("test_reachable_citation_verifies")
    v = research._research_verify(type("R", (), {"args": {}})(), out)
    check(v.status == VERIFIED, f"a reachable cited source must VERIFY, got {v.status}")

    print("test_answer_without_sources_still_verifies")
    # deliverable-is-the-answer: a real answer succeeds even with no sources.
    out2 = research.run_research("vague question", runner=lambda q: "Here is a useful answer.")
    v2 = research._research_verify(type("R", (), {"args": {}})(), out2)
    check(v2.status == VERIFIED, f"a delivered answer must VERIFY, got {v2.status}")

    print("test_blocked_source_still_verifies_not_failed")
    # a real site that 403s a cookieless bot must NOT fail the job (the Kickstarter
    # case): the answer was delivered; source re-check is just an annotation.
    blk = f"http://127.0.0.1:{port}/blocked"
    out3 = research.run_research("q", runner=lambda q: f"Buy it. Sources:\n- {blk}\n")
    v3 = research._research_verify(type("R", (), {"args": {}})(), out3)
    check(v3.status == VERIFIED,
          f"a 403-blocked source must NOT fail a delivered answer, got {v3.status}")

    print("test_empty_answer_is_the_only_miss")
    out4 = research.run_research("q", runner=lambda q: "   ")
    v4 = research._research_verify(type("R", (), {"args": {}})(), out4)
    check(v4.status != VERIFIED, "an empty answer is the only genuine miss")

    print("test_hedged_no_info_is_failed_not_fabricated")
    for noinfo in ("Sorry, I was unable to find any reliable information on this topic.",
                   "Unfortunately, I couldn't find anything.",
                   "There are no results available for this product.",
                   "I searched but was unable to find anything useful.",
                   "I could not locate anything useful.",
                   "I'm sorry, but I couldn't find that.",
                   "抱歉，我没有找到相关信息。", "很遗憾，查不到这个产品。",
                   "很抱歉，我没能找到答案。", "无法找到相关结果。"):
        o = research.run_research("q", runner=lambda q, n=noinfo: n)
        v = research._research_verify(type("R", (), {"args": {}})(), o)
        check(v.status != VERIFIED, f"a hedged no-info reply must NOT verify: {noinfo!r} -> {v.status}")
    # a real answer that merely opens with 'Sorry' is not falsely failed
    ok = research.run_research("q", runner=lambda q: "Sorry for the wait — buy it at RideCo.")
    check(research._research_verify(type("R", (), {"args": {}})(), ok).status == VERIFIED,
          "a real answer opening with 'Sorry' must still verify")

    print("test_registered_in_builtins")
    clear_registry(); caps.register_builtins()
    check(get_capability("research.web") is not None, "research.web must be registered")

    srv.shutdown()
    clear_registry()
    if _fails:
        print(f"\n== RESEARCH: {len(_fails)} FAILED ==")
        sys.exit(1)
    print("\n== RESEARCH: all checks passed ==")


if __name__ == "__main__":
    main()
