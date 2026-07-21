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
            body = b"<h1>PowerRider P1 review</h1>"
            self.send_response(200)
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

    print("test_no_citation_is_inconclusive")
    out2 = research.run_research("vague question", runner=lambda q: "I think just buy one.")
    v2 = research._research_verify(type("R", (), {"args": {}})(), out2)
    check(v2.status == INCONCLUSIVE,
          f"a source-less answer must be INCONCLUSIVE, got {v2.status}")

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
