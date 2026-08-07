"""Real-socket test of the delegate dashboard server (harness.jobsweb).

Run: python tests/test_jobsweb.py   (exit 0 = all green)

Spins the actual HTTP server on loopback and drives it: GET / serves the page,
POST /api/run creates+drives a live note.append (verified), GET /api/state
reflects it, and a state-changing POST WITHOUT the same-origin header is refused
(CSRF gate).
"""
import json
import os
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_notes = tempfile.mkdtemp(prefix="collie-web-notes-")
os.environ["COLLIE_NOTES_DIR"] = _notes

from harness import capabilities as caps  # noqa: E402
from harness import jobsweb  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def _req(url, method="GET", body=None, header=True, origin=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if header:
        req.add_header("X-Collie-Jobs", "1")
    if origin:
        req.add_header("Origin", origin)      # simulate a browser's same-origin fetch
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main():
    caps.register_builtins()
    state = tempfile.mkdtemp(prefix="collie-web-state-")

    # Seed a paused Mission with a concrete parked publish action. The legacy
    # dashboard confirm endpoint must not execute it through the one-shot Job path.
    from harness.missionweb import MissionService
    from harness.actions import ActionStore

    class OnePublish:
        def __call__(self, *_args):
            return {"action": "web.submit", "args": {"what": "post"}, "reason": "publish"}

    msvc = MissionService(state_dir=state, decider=OnePublish(), stub=True)
    mst = msvc.start("publish safely", autonomous=False)
    mst = msvc.run(mst["mission_id"])
    mission_nonce = mst["inbox"]["nonce"]
    msvc.pause(mst["mission_id"])
    msvc.close()

    srv = ThreadingHTTPServer(("127.0.0.1", 0), jobsweb._make_handler(state))
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    print("test_page_serves")
    code, page = _req(base + "/")
    check(code == 200 and "collie · delegate" in page, "GET / must serve the dashboard")

    print("test_run_creates_and_verifies")
    code, out = _req(base + "/api/run", "POST",
                     {"capability": "note.append",
                      "args": {"file": "w.txt", "text": "via-dashboard"},
                      "leash": {"may": ["note.*"]}})
    r = json.loads(out)
    check(code == 200 and r.get("status") == "verified",
          f"run must create+verify a note.append, got {out}")
    with open(os.path.join(_notes, "w.txt"), encoding="utf-8") as f:
        check("via-dashboard" in f.read(), "the note must be on disk")

    print("test_state_reflects_the_job")
    code, out = _req(base + "/api/state")
    d = json.loads(out)
    check(any(j["state"] == "done_verified" for j in d["jobs"]),
          "state must show the verified job")
    check(any(rc["verdict"] == "verified" for rc in d["receipts"]),
          "state must include the receipt")

    print("test_leash_denies_out_of_scope")
    code, out = _req(base + "/api/run", "POST",
                     {"capability": "note.append", "args": {"file": "x", "text": "y"},
                      "leash": {"may": ["email.*"]}})
    check("leash denied" in out, f"out-of-scope run must be denied: {out}")

    print("test_csrf_post_without_header_refused")
    code, out = _req(base + "/api/run", "POST",
                     {"capability": "note.append", "args": {}, "leash": {}},
                     header=False)
    check(code == 403, f"a POST without the same-origin header must be 403, got {code}")

    print("test_same_origin_browser_post_allowed")
    # a real browser sends Origin on same-origin POSTs; that MUST NOT be rejected
    # (the bug that 403'd the dashboard's own calls -> "not sure").
    code, out = _req(base + "/api/run", "POST",
                     {"capability": "note.append",
                      "args": {"file": "b.txt", "text": "browser-origin"},
                      "leash": {"may": ["note.*"]}},
                     origin=base)
    r = json.loads(out)
    check(code == 200 and r.get("status") == "verified",
          f"a same-origin browser POST (Origin set + header) must succeed, got {code} {out}")

    print("test_dashboard_confirm_cannot_bypass_paused_mission")
    code, out = _req(base + "/api/confirm", "POST", {"nonce": mission_nonce})
    got = json.loads(out)
    actions = ActionStore(os.path.join(state, "actions.db"))
    check(code == 200 and got.get("error") and actions.get(mission_nonce).state == "pending",
          "dashboard routes Mission nonce through lifecycle checks; paused action stays pending")
    actions.close()

    srv.shutdown()
    if _fails:
        print(f"\n== JOBSWEB: {len(_fails)} FAILED ==")
        sys.exit(1)
    print("\n== JOBSWEB: all checks passed (real sockets) ==")


if __name__ == "__main__":
    main()
