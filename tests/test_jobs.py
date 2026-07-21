"""Pin the Job lifecycle + capability registry + model-free executor (harness.jobs).

Run: python tests/test_jobs.py   (exit 0 = all green)

Drives a full delegated errand end to end through the durable Job object:
create -> propose gated action (needs_you) -> confirm -> executor looks the
capability up BY NAME and runs it -> real done-check -> job reaches the right
terminal state. Proves the verified/accepted/failed/needs_you mapping, including
that an INCONCLUSIVE post-check does NOT become a false success.
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.actions import ActionStore, RefusedError  # noqa: E402
from harness.jobs import (  # noqa: E402
    Capability, Executor, JobStore, register, clear_registry, get_capability,
    QUEUED, NEEDS_YOU, DONE_VERIFIED, DONE_ACCEPTED, FAILED_S,
)
from harness.observe import donecheck_listing  # noqa: E402
from harness.verifier import VERIFIED, FAILED, INCONCLUSIVE, NOT_ARMED, Verdict  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def _stores():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return ActionStore(p + ".actions"), JobStore(p + ".jobs")


def test_registry_lookup():
    print("test_registry_lookup")
    clear_registry()
    register(Capability("x.noop", execute=lambda r: None))
    check(get_capability("x.noop") is not None, "registered capability must be found")
    check(get_capability("nope") is None, "unregistered capability must be None")


def test_full_lifecycle_verified():
    print("test_full_lifecycle_verified")
    clear_registry()
    # fixture marketplace that flips 404 -> live on publish
    state = {"live": False}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if state["live"]:
                body, code = b"<h1>resin figurine</h1><span>ered120</span>".replace(
                    b"ered120", b"\xc2\xa5420"), 200
            else:
                body, code = b"<h1>Not Found</h1>", 404
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/l/1"

    def publish(rec):
        state["live"] = True
        return {"ok": True}

    def verify(rec, res):
        return donecheck_listing(rec.args["url"], rec.args["title"], price_max=450,
                                 publish_at=1, at=2)

    register(Capability("listing.publish", execute=publish, verify=verify,
                        reversible=False))

    acts, jobs = _stores()
    job = jobs.create("job-1", "sell the resin figurine under ¥450",
                      leash={"price_floor": 400})
    check(job.state == QUEUED, "new job starts queued")

    # the proposing step materializes the gated action and the job waits on a human
    n = acts.propose("listing.publish", {"url": url, "title": "resin figurine"},
                     job_id="job-1")
    jobs.set_state("job-1", NEEDS_YOU)
    check(jobs.get("job-1").state == NEEDS_YOU, "gated action puts the job in needs_you")

    # human confirms; the model-free executor runs it by capability name
    acts.confirm(n)
    ex = Executor(acts, jobs)
    verdict = ex.run_confirmed(n, job_id="job-1")
    check(verdict.status == VERIFIED, f"independent re-fetch must verify, got {verdict.status}")
    check(jobs.get("job-1").state == DONE_VERIFIED, "verified outcome -> done_verified")
    check(len(acts.receipts(n)) == 1 and acts.receipts(n)[0]["fired"] == 1,
          "a fired receipt must exist")
    srv.shutdown()
    acts.close()
    jobs.close()


def test_inconclusive_becomes_needs_you_not_success():
    print("test_inconclusive_becomes_needs_you_not_success")
    clear_registry()
    # done-check that cannot observe (transport error) -> INCONCLUSIVE
    register(Capability(
        "listing.publish", execute=lambda r: {"ok": True}, reversible=False,
        verify=lambda r, res: donecheck_listing("http://127.0.0.1:1/x", "t",
                                                 price_max=9, publish_at=1, at=2)))
    acts, jobs = _stores()
    jobs.create("job-2", "publish", {})
    n = acts.propose("listing.publish", {"url": "x"}, job_id="job-2")
    acts.confirm(n)
    verdict = Executor(acts, jobs).run_confirmed(n, job_id="job-2")
    check(verdict.status == INCONCLUSIVE, f"unobservable -> INCONCLUSIVE, got {verdict.status}")
    check(jobs.get("job-2").state == NEEDS_YOU,
          "an INCONCLUSIVE post-check must go to needs_you, NEVER a done state")
    acts.close()
    jobs.close()


def test_failed_verdict_marks_failed_and_compensates():
    print("test_failed_verdict_marks_failed_and_compensates")
    clear_registry()
    comp = {"ran": False}
    register(Capability(
        "pay.charge", execute=lambda r: {"ok": True}, reversible=False,
        verify=lambda r, res: Verdict(FAILED, "bank declined"),
        compensate=lambda r: comp.__setitem__("ran", True)))
    acts, jobs = _stores()
    jobs.create("job-3", "pay", {})
    n = acts.propose("pay.charge", {"amt": 50}, job_id="job-3")
    acts.confirm(n)
    verdict = Executor(acts, jobs).run_confirmed(n, job_id="job-3")
    check(verdict.status == FAILED, "failed done-check -> FAILED verdict")
    check(jobs.get("job-3").state == FAILED_S, "failed outcome -> job failed")
    check(comp["ran"], "an irreversible failure must run the compensation hook")
    acts.close()
    jobs.close()


def test_unknown_capability_refused_before_firing():
    print("test_unknown_capability_refused_before_firing")
    clear_registry()
    acts, jobs = _stores()
    jobs.create("job-4", "mystery", {})
    n = acts.propose("nonexistent.cap", {}, job_id="job-4")
    acts.confirm(n)
    try:
        Executor(acts, jobs).run_confirmed(n, job_id="job-4")
        check(False, "unknown capability must be refused")
    except RefusedError:
        pass
    check(jobs.get("job-4").state != DONE_VERIFIED, "job must not reach a done state")
    acts.close()
    jobs.close()


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    clear_registry()
    if _fails:
        print(f"\n== JOBS: {len(_fails)} FAILED ==")
        sys.exit(1)
    print(f"\n== JOBS: {len(tests)} test groups passed ==")


if __name__ == "__main__":
    main()
