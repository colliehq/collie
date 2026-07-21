"""Pin the confirm-token / executor / receipt spine (harness.actions).

Run: python tests/test_actions.py   (exit 0 = all green)

Proves the six guarantees the delegate's safety rests on, plus the full chain:
propose -> confirm -> deterministic executor -> real done-check -> receipt.
The side effect is a counter, so 'fired exactly once' is checkable without any
real irreversible action.
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.actions import ActionStore, RefusedError, EXECUTED, APPROVED  # noqa: E402
from harness.observe import donecheck_listing  # noqa: E402
from harness.verifier import VERIFIED, INCONCLUSIVE, Verdict, FAILED  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def _tmp():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return p


def test_happy_path_fires_once_and_receipts():
    print("test_happy_path_fires_once_and_receipts")
    p = _tmp()
    st = ActionStore(p)
    calls = {"n": 0}
    n = st.propose("email.send", {"to": "a@b.com", "subject": "hi"}, leash_id="L1")
    st.confirm(n)
    rc = st.execute(n,
                    side_effect_fn=lambda r: calls.__setitem__("n", calls["n"] + 1),
                    donecheck_fn=lambda r, res: Verdict(VERIFIED, "sent", ()))
    check(calls["n"] == 1, f"side effect must fire once, fired {calls['n']}")
    check(rc.fired and rc.verdict == VERIFIED, "receipt must record fired+verified")
    check(len(st.receipts(n)) == 1, "exactly one receipt written")
    st.close()


def test_single_use_no_double_send():
    print("test_single_use_no_double_send")
    st = ActionStore(_tmp())
    calls = {"n": 0}
    n = st.propose("pay.charge", {"amt": 50})
    st.confirm(n)
    st.execute(n, side_effect_fn=lambda r: calls.__setitem__("n", calls["n"] + 1))
    try:
        st.execute(n, side_effect_fn=lambda r: calls.__setitem__("n", calls["n"] + 1))
        check(False, "second execute must be refused")
    except RefusedError:
        pass
    check(calls["n"] == 1, f"side effect must fire exactly once across duplicate executes, got {calls['n']}")
    st.close()


def test_fail_closed_unconfirmed_cannot_execute():
    print("test_fail_closed_unconfirmed_cannot_execute")
    st = ActionStore(_tmp())
    fired = {"v": False}
    n = st.propose("listing.publish", {"price": 420})
    try:
        st.execute(n, side_effect_fn=lambda r: fired.__setitem__("v", True))
        check(False, "executing an unconfirmed action must be refused")
    except RefusedError:
        pass
    check(not fired["v"], "unconfirmed side effect must NOT fire")
    st.close()


def test_durable_across_restart():
    print("test_durable_across_restart")
    p = _tmp()
    st = ActionStore(p)
    n = st.propose("listing.publish", {"price": 420}, leash_id="L9")
    st.confirm(n)
    st.close()                       # simulate the proposing process dying
    st2 = ActionStore(p)             # a fresh process reopens the on-disk store
    fired = {"v": 0}
    rc = st2.execute(n, side_effect_fn=lambda r: fired.__setitem__("v", fired["v"] + 1))
    check(fired["v"] == 1, "an approval must survive restart and still execute")
    check(rc.leash_id == "L9", "record fields survive restart")
    st2.close()


def test_payload_tamper_refused():
    print("test_payload_tamper_refused")
    st = ActionStore(_tmp())
    n = st.propose("pay.charge", {"amt": 50})
    st.confirm(n)
    # tamper the stored args AFTER approval (simulating a compromised write path)
    st.db.execute("UPDATE pending_actions SET args_json=? WHERE nonce=?",
                  ('{"amt": 5000}', n))
    st.db.commit()
    fired = {"v": False}
    try:
        st.execute(n, side_effect_fn=lambda r: fired.__setitem__("v", True))
        check(False, "tampered payload must be refused")
    except RefusedError:
        pass
    check(not fired["v"], "tampered side effect must NOT fire (digest binding)")
    st.close()


def test_toctou_divergence_refused():
    print("test_toctou_divergence_refused")
    st = ActionStore(_tmp())
    n = st.propose("listing.publish", {"price": 420},
                   snapshot={"price_field": "420"})
    st.confirm(n)
    fired = {"v": False}
    try:
        st.execute(n,
                   side_effect_fn=lambda r: fired.__setitem__("v", True),
                   unchanged_fn=lambda r: False)   # world diverged from snapshot
        check(False, "TOCTOU divergence must refuse")
    except RefusedError:
        pass
    check(not fired["v"], "diverged side effect must NOT fire")
    # a refusal-after-approval still writes an evidenced receipt, and the latch
    # rolled back to APPROVED so a real re-check can proceed later
    check(st.get(n).state == APPROVED, "latch must roll back to APPROVED after TOCTOU refuse")
    check(any(not r["fired"] for r in st.receipts(n)), "a non-fired receipt must record the refusal")
    st.close()


def test_end_to_end_with_real_donecheck():
    print("test_end_to_end_with_real_donecheck")
    # fixture marketplace: publishing 'flips' the page from 404 to a live listing.
    state = {"live": False}
    pages_live = "<h1>Vintage resin figurine</h1><span>¥420</span>"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if state["live"]:
                body = pages_live.encode()
                self.send_response(200)
            else:
                body = b"<h1>Not Found</h1>"
                self.send_response(404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/listing/1"

    st = ActionStore(_tmp())
    n = st.propose("listing.publish", {"url": url, "title": "resin figurine", "price": 420})
    st.confirm(n)

    def publish(record):           # the real (here, fixture) irreversible action
        state["live"] = True
        return {"ok": True}

    def donecheck(record, result):  # independent logged-out re-fetch verifies it
        return donecheck_listing(record.args["url"], record.args["title"],
                                 price_max=450, publish_at=1, at=2)

    rc = st.execute(n, side_effect_fn=publish, donecheck_fn=donecheck)
    check(rc.fired, "listing publish must fire")
    check(rc.verdict == VERIFIED,
          f"independent re-fetch must VERIFY the published listing, got {rc.verdict}: {rc.verdict_reason}")
    check(st.get(n).state == EXECUTED, "action must be marked executed")
    check("logged-out-fetch" in rc.evidence or "200" in rc.evidence,
          "receipt evidence must cite the independent observation")
    srv.shutdown()
    st.close()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    if _fails:
        print(f"\n== ACTIONS: {len(_fails)} FAILED ==")
        sys.exit(1)
    print(f"\n== ACTIONS: {len(tests)} test groups passed ==")


if __name__ == "__main__":
    main()
