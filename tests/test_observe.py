"""End-to-end done-check over a REAL independent channel (real sockets, no mocks).

Run: python tests/test_observe.py   (exit 0 = all green)

Spins a stdlib http.server on localhost serving listing fixtures and drives the
full publish done-check through harness.observe.fetch_loggedout — proving the
world verifier's four properties against actual HTTP:
  - present + within cap        -> VERIFIED
  - present + over cap          -> FAILED  (refuted by a real page)
  - 404 (absent to a stranger)  -> FAILED
  - login wall / no title       -> INCONCLUSIVE (honest "can't tell", not FAILED)
  - closed port (transport err) -> INCONCLUSIVE (fail-closed)
  - stale observation           -> INCONCLUSIVE (freshness: must post-date publish)
  - the fetch carries NO Cookie -> the channel is genuinely session-free
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.observe import donecheck_listing, fetch_loggedout  # noqa: E402
from harness.verifier import VERIFIED, FAILED, INCONCLUSIVE  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


# ── fixture site ────────────────────────────────────────────────────────────
PAGES = {
    "/listing/ok":      (200, "<h1>Vintage resin figurine</h1><span>Price: ¥420</span>"),
    "/listing/pricey":  (200, "<h1>Vintage resin figurine</h1><span>Price: ¥900</span>"),
    "/listing/wall":    (200, "<h1>Log in to continue</h1><form>password</form>"),
    "/listing/gone":    (404, "<h1>Not Found</h1>"),
}
_seen_headers = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        _seen_headers[self.path] = {k.lower(): v for k, v in self.headers.items()}
        status, body = PAGES.get(self.path, (404, "<h1>Not Found</h1>"))
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("test_present_within_cap_verifies")
    v = donecheck_listing(base + "/listing/ok", "resin figurine", price_max=450,
                          publish_at=1, at=2)
    check(v.status == VERIFIED, f"present+within cap must VERIFY, got {v.status}: {v.reason}")

    print("test_present_over_cap_fails")
    v = donecheck_listing(base + "/listing/pricey", "resin figurine", price_max=450,
                          publish_at=1, at=2)
    check(v.status == FAILED, f"price over cap must FAIL, got {v.status}")

    print("test_absent_404_fails")
    v = donecheck_listing(base + "/listing/gone", "resin figurine", price_max=450,
                          publish_at=1, at=2)
    check(v.status == FAILED, f"404 (absent to a stranger) must FAIL, got {v.status}")

    print("test_login_wall_is_inconclusive")
    v = donecheck_listing(base + "/listing/wall", "resin figurine", price_max=450,
                          publish_at=1, at=2)
    check(v.status == INCONCLUSIVE,
          f"login wall / no title must be INCONCLUSIVE (not FAILED), got {v.status}")

    print("test_transport_error_is_inconclusive")
    # a definitely-closed port on this host -> connection refused -> can't observe
    v = donecheck_listing("http://127.0.0.1:1/listing/ok", "resin figurine",
                          price_max=450, publish_at=1, at=2)
    check(v.status == INCONCLUSIVE, f"transport error must fail-closed to INCONCLUSIVE, got {v.status}")

    print("test_stale_observation_is_inconclusive")
    # observation stamped BEFORE the publish -> not fresh -> INCONCLUSIVE even though present
    v = donecheck_listing(base + "/listing/ok", "resin figurine", price_max=450,
                          publish_at=5, at=2)
    check(v.status == INCONCLUSIVE, f"pre-publish (stale) evidence must be INCONCLUSIVE, got {v.status}")

    print("test_channel_is_session_free")
    fetch_loggedout(base + "/listing/ok")
    hdr = _seen_headers.get("/listing/ok", {})
    check("cookie" not in hdr,
          "independent channel must send NO Cookie header (session-free by construction)")

    srv.shutdown()
    if _fails:
        print(f"\n== OBSERVE: {len(_fails)} FAILED ==")
        sys.exit(1)
    print("\n== OBSERVE: 7 checks passed (real sockets) ==")


if __name__ == "__main__":
    main()
