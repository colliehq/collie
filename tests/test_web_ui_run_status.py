"""What the task surface says about a run, checked against a real page over real HTTP/SSE.

These are the four things the UI got wrong that a person actually notices:

  * the header still said "New task" after the task had a name, while the sidebar row beside it
    showed the request — the two disagreed about the very thread being read;
  * every answer, including a read-only one, ended under an EVIDENCE card reading "No executed
    check", which is irrelevant to a question that changed nothing and reads like a failure;
  * a run stopped by a turn or budget cap was labelled DONE, because the terminal frame could only
    tell an error from a cancellation — so a half-finished answer looked finished;
  * child investigations and context compaction were dead air in the middle of a run.

The page is served by a fixture server rather than the product's own, so every run here is
deterministic: the SSE script is chosen by the request text and ends by closing the stream. It is
the real index.html, the real EventSource, and the real fetch paths — only the answers are staged.
"""
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI = os.path.join(os.path.dirname(HERE), "harness", "webui")
TOKEN = "fixture-token"

# ---------------------------------------------------------------- staged runs
# Each script is a list of (event, payload). The stream sends them in order and closes, which is
# what the real server does at the end of a run.
DONE_BASE = {"model": "mock", "turns": 1, "tool_calls": 0, "wall_ms": 1200,
             "total_tokens": 900, "prefix_tokens": 400, "cost_usd": 0.0}

# What the backend says when a run stopped with an external tool possibly already fired. The
# second one is deliberately hostile: a reason is server text, and text is all it may ever be.
FENCE_REASON = "run_shell ./deploy.sh may already have run"
HOSTILE_REASON = ('<img src=x onerror="window.__fenced_xss=1">'
                  '<script>window.__fenced_xss=1</script> publish.sh may have run')


def _fence(sid, run, reason):
    """The recovery block the terminal contract carries beside a fenced verdict."""
    return {"session_id": sid, "run_id": run, "state": "external_action", "turn": 1,
            "recovery_required": True, "auto_resumable": False, "reason": reason}


# What `run_verification_command` records for a green check that moved the tree under
# itself — `cargo test` writing `target/`, `npm run build` writing `dist/`. Both commands
# are ones `detect_verification_commands` proposes, so this is the ordinary case, not a
# corner of one.
UNCERTIFIED_EVIDENCE = {"command": "npm run build", "executed": True, "cancelled": False,
                        "exit_code": 0, "command_passed": True, "passed": False,
                        "ran_after_last_edit": False, "freshness": "changed_during_check",
                        "working_tree_changed_during_check": True, "source": "user"}
UNCERTIFIED_ERROR = ("required check did not certify this result: npm run build exited 0, "
                     "but the workspace changed while it was running, so its result does "
                     "not cover the files that exist now")
# The same run, except the command really did fail. Nothing about the repair may soften it.
FAILED_EVIDENCE = dict(UNCERTIFIED_EVIDENCE, exit_code=1, command_passed=False,
                       freshness="fresh", working_tree_changed_during_check=False,
                       command="pytest -q")


def _script(text):
    """Pick a staged run from the request text, the way a router would pick a route."""
    q = (text or "").lower()
    if "deploy the release" in q:
        # Stopped mid-tool: the run is over AND the thread stays fenced until a person says
        # what actually happened outside the process.
        return [
            ("start", {"session": "s-fence", "run": "r-fence", "model": "mock", "prior_turns": 0}),
            ("token", {"t": "I started the deploy script."}),
            ("tool", {"name": "run_shell", "args": {"cmd": "./deploy.sh"}, "ok": True,
                      "result": "uploading release bundle"}),
            ("done", dict(DONE_BASE, session="s-fence", run="r-fence", answer="", error="",
                          canceled=True, stop_reason="canceled", completed=False, edited=False,
                          recovery_required=True,
                          recovery=_fence("s-fence", "r-fence", FENCE_REASON))),
        ]
    if "publish the changelog" in q:
        # A result that reads as finished while the fence it left is still open.
        return [
            ("start", {"session": "s-fence", "run": "r-pub", "model": "mock", "prior_turns": 0}),
            ("token", {"t": "Published the changelog draft."}),
            ("done", dict(DONE_BASE, session="s-fence", run="r-pub",
                          answer="Published the changelog draft.", error="", canceled=False,
                          stop_reason="completed", completed=True, edited=False,
                          recovery_required=True,
                          recovery=_fence("s-fence", "r-pub", HOSTILE_REASON))),
        ]
    if "restart the importer" in q:
        # The same terminal frame twice, the way a reconnecting mirror delivers it.
        frame = ("done", dict(DONE_BASE, session="s-fence", run="r-twice",
                              answer="Import halted.", error="", canceled=True,
                              stop_reason="canceled", completed=False, edited=False,
                              recovery_required=True,
                              recovery=_fence("s-fence", "r-twice", FENCE_REASON)))
        return [
            ("start", {"session": "s-fence", "run": "r-twice", "model": "mock", "prior_turns": 0}),
            ("token", {"t": "Import halted."}),
            frame, frame,
        ]
    if "cap the fenced migration" in q:
        # A cap with a fence behind it: the work is kept, but "Continue" is not on offer.
        return [
            ("start", {"session": "s-fence", "run": "r-cap", "model": "mock", "prior_turns": 0}),
            ("token", {"t": "Converted the first module."}),
            ("done", dict(DONE_BASE, session="s-fence", run="r-cap",
                          answer="Converted the first module.", error="", canceled=False,
                          stop_reason="turn_limit", completed=False, turns_exhausted=True,
                          turns=8, max_turns=8, edited=True, recovery_required=True,
                          recovery=_fence("s-fence", "r-cap", FENCE_REASON))),
        ]
    if "stop before checking" in q:
        skipped = {"command": "pytest -q", "executed": False, "passed": False,
                   "freshness": "not_run", "skipped_reason": "The user stopped the run."}
        return [
            ("start", {"session": "s-stop-check", "run": "r-sc", "model": "mock", "prior_turns": 0}),
            ("verification_evidence", {"evidence": skipped}),
            ("done", dict(DONE_BASE, session="s-stop-check", run="r-sc", answer="_[stopped by user]_",
                          error="", canceled=True, stop_reason="canceled", completed=False,
                          edited=False, verification_evidence=skipped)),
        ]
    if "interrupt" in q:
        canceled = "error" not in q
        edited = "edit" in q
        return [
            ("start", {"session": "s-interrupt", "run": "r-stop", "model": "mock", "prior_turns": 0}),
            ("token", {"t": "I found the parsing boundary."}),
            ("tool", {"name": "read_file", "args": {"path": "parser.py"}, "ok": True,
                      "result": "The date parser accepts extra whitespace."}),
            *([("edit", {"path": "parser.py", "old": "old", "new": "new"})] if edited else []),
            ("token", {"t": "The next step would be a strict-date check."}),
            ("done", dict(DONE_BASE, session="s-interrupt", run="r-stop", answer="",
                          error="Provider connection was interrupted" if not canceled else "",
                          canceled=canceled, stop_reason="canceled" if canceled else "error",
                          completed=False, edited=edited)),
        ]
    if "cancel the required check" in q:
        evidence = {"command": "python -m unittest", "executed": True, "passed": False,
                    "cancelled": True, "exit_code": 1}
        return [
            ("start", {"session": "s-check-stop", "run": "r-check-stop", "model": "mock", "prior_turns": 0}),
            ("token", {"t": "The requested fix is ready."}),
            ("verification_started", {"command": evidence["command"], "run": "r-check-stop"}),
            ("verification_canceling", {"command": evidence["command"]}),
            ("verification_finished", evidence),
            ("verification_evidence", {"evidence": evidence}),
            ("done", dict(DONE_BASE, session="s-check-stop", run="r-check-stop",
                          answer="The requested fix is ready.", canceled=True, completed=False,
                          stop_reason="canceled", verification_evidence=evidence)),
        ]
    if "bundle the release assets" in q:
        # Exit 0, and still no verdict about these files: the check wrote its own build
        # output into the workspace it was grading, so the receipt could not be bound to
        # the bytes that exist now. `command_passed` and `passed` are two different facts
        # and the surface is not allowed to collapse them into "failed · exit 0".
        return [
            ("start", {"session": "s-uncertified", "run": "r-unc", "model": "mock",
                       "prior_turns": 0}),
            ("edit", {"path": "src/app.js", "old": "a = 1\n", "new": "a = 2\n"}),
            ("verification_evidence", {"evidence": UNCERTIFIED_EVIDENCE}),
            ("done", dict(DONE_BASE, session="s-uncertified", run="r-unc",
                          answer="Bundled the release assets.",
                          error=UNCERTIFIED_ERROR, canceled=False, stop_reason="error",
                          completed=False, edited=True, verified=False,
                          verification_evidence=UNCERTIFIED_EVIDENCE)),
        ]
    if "break the release build" in q:
        return [
            ("start", {"session": "s-red", "run": "r-red", "model": "mock", "prior_turns": 0}),
            ("edit", {"path": "src/app.js", "old": "a = 1\n", "new": "a = 3\n"}),
            ("verification_evidence", {"evidence": FAILED_EVIDENCE}),
            ("done", dict(DONE_BASE, session="s-red", run="r-red",
                          answer="Changed the loader.",
                          error="required check failed: pytest -q (exit 1)", canceled=False,
                          stop_reason="error", completed=False, edited=True, verified=False,
                          verification_evidence=FAILED_EVIDENCE)),
        ]
    if "readme" in q:
        return [
            ("start", {"session": "s-read", "run": "r1", "model": "mock", "prior_turns": 0}),
            ("tool", {"name": "read_file", "args": {"path": "README.md"}, "ok": True,
                      "result": "1 # Ledger summary"}),
            ("token", {"t": "It reads a CSV and prints per-category totals."}),
            ("done", dict(DONE_BASE, session="s-read", run="r1",
                          answer="It reads a CSV and prints per-category totals.",
                          error="", canceled=False, stop_reason="completed", completed=True,
                          edited=False, model_calls=2)),
        ]
    if "summarise the ledger module" in q:
        # Same run, same cost number, two routes. On a plan route that number is an
        # API-equivalent estimate the provider never billed, so the card may not
        # print it as the charge; on a metered route it is the amount.
        plan = "on my plan" in q
        sid = "s-plan" if plan else "s-metered"
        return [
            ("start", {"session": sid, "run": "r-" + sid, "model": "mock", "prior_turns": 0}),
            ("token", {"t": "Summarised the ledger module."}),
            ("done", dict(DONE_BASE, session=sid, run="r-" + sid,
                          answer="Summarised the ledger module.", error="", canceled=False,
                          stop_reason="completed", completed=True, edited=False,
                          cost_usd=0.42, subscription=plan)),
        ]
    if "fix the parser" in q:
        return [
            ("start", {"session": "s-edit", "run": "r2", "model": "mock", "prior_turns": 0}),
            ("edit", {"path": "harness/parser.py", "old": "a = 1\n", "new": "a = 2\n"}),
            ("verification_evidence", {"evidence": {"command": "pytest -q tests/test_parser.py",
                                                    "passed": True, "exit_code": 0}}),
            ("done", dict(DONE_BASE, session="s-edit", run="r2", answer="Fixed the off-by-one.",
                          error="", canceled=False, stop_reason="completed", completed=True,
                          edited=True, verified=True, turns=3, tool_calls=4,
                          verification_evidence={"command": "pytest -q tests/test_parser.py",
                                                 "passed": True, "exit_code": 0})),
        ]
    if "migrate every module" in q:
        return [
            ("start", {"session": "s-cap", "run": "r3", "model": "mock", "prior_turns": 0}),
            ("token", {"t": "I converted the first two modules and listed the rest."}),
            ("done", dict(DONE_BASE, session="s-cap", run="r3",
                          answer="I converted the first two modules and listed the rest.",
                          error="", canceled=False, stop_reason="turn_limit", completed=False,
                          turns_exhausted=True, turns=8, max_turns=8, model_calls=17,
                          edited=True)),
        ]
    if "continue from where you stopped" in q:
        return [
            ("start", {"session": "s-cap", "run": "r4", "model": "mock", "prior_turns": 2}),
            ("done", dict(DONE_BASE, session="s-cap", run="r4",
                          answer="Converted the remaining modules.", error="", canceled=False,
                          stop_reason="completed", completed=True, edited=True, turns=4)),
        ]
    if "audit the whole monorepo" in q:
        return [
            ("start", {"session": "s-budget", "run": "r5", "model": "mock", "prior_turns": 0}),
            ("token", {"t": "Audited two packages before the cap."}),
            ("done", dict(DONE_BASE, session="s-budget", run="r5",
                          answer="Audited two packages before the cap.", error="", canceled=False,
                          stop_reason="budget_limit", completed=False, budget_exhausted=True,
                          turns=5, model_calls=31, edited=False)),
        ]
    if "tighten the config loader" in q:
        # Verification was demanded and the run ended without executing one. The promise is the
        # point: this must not settle as a quiet success.
        return [
            ("start", {"session": "s-req", "run": "r9", "model": "mock", "prior_turns": 0}),
            ("done", dict(DONE_BASE, session="s-req", run="r9", answer="Tightened it.", error="",
                          canceled=False, stop_reason="completed", completed=True, edited=True,
                          verified=False)),
        ]
    if "why is login slow" in q:
        return [
            ("start", {"session": "s-deleg", "run": "r6", "model": "mock", "prior_turns": 0}),
            ("delegate_start", {"task": "Trace the login request path and report hot spots",
                                "model": "mock-sonnet"}),
            ("delegate_progress", {"parent_run_id": "r6", "event": "tool", "tool": "grep",
                                   "ok": True, "turns": 1}),
            ("delegate_progress", {"parent_run_id": "r6", "event": "tool", "tool": "read_file",
                                   "ok": True, "turns": 2}),
            ("delegate_done", {"status": "completed", "run_id": "child-1", "turns": 3,
                               "model_calls": 5, "tool_calls": 4}),
            ("done", dict(DONE_BASE, session="s-deleg", run="r6",
                          answer="Session lookup runs per request.", error="", canceled=False,
                          stop_reason="completed", completed=True, edited=False)),
        ]
    if "summarize the design doc" in q:
        return [
            ("start", {"session": "s-compact", "run": "r7", "model": "mock", "prior_turns": 6}),
            ("compaction", {"status": "started", "before_tokens": 148000, "generation": 1}),
            ("compaction", {"status": "failed", "generation": 1,
                            "reason": "summarizer call timed out"}),
            ("token", {"t": "The doc proposes a two-stage pipeline."}),
            ("done", dict(DONE_BASE, session="s-compact", run="r7",
                          answer="The doc proposes a two-stage pipeline.", error="",
                          canceled=False, stop_reason="completed", completed=True, edited=False)),
        ]
    if "shrink the history" in q:
        return [
            ("start", {"session": "s-compact2", "run": "r8", "model": "mock", "prior_turns": 9}),
            ("compaction", {"status": "started", "before_tokens": 150000, "generation": 2}),
            ("compaction", {"status": "applied", "before_tokens": 150000, "after_tokens": 42000,
                            "cutoff": 12, "kept": 6, "generation": 2}),
            ("done", dict(DONE_BASE, session="s-compact2", run="r8", answer="Done.", error="",
                          canceled=False, stop_reason="completed", completed=True, edited=False)),
        ]
    return [
        ("start", {"session": "s-misc", "run": "r0", "model": "mock", "prior_turns": 0}),
        ("done", dict(DONE_BASE, session="s-misc", run="r0", answer="ok", error="",
                      canceled=False, stop_reason="completed", completed=True, edited=False)),
    ]


# Saved threads the sidebar and a reload read back. `s-cap` is the unfinished one.
SESSIONS = [
    {"id": "s-cap", "title": "Migrate every module to the new config loader", "turns": 1,
     "updated": time.time(), "last": "", "cwd": "/repo", "edits": 2, "touches": 3},
    {"id": "s-read", "title": "Read README.md and tell me what this tool does", "turns": 1,
     "updated": time.time() - 60, "last": "", "cwd": "/repo", "edits": 0, "touches": 1},
]

TRANSCRIPTS = {
    "s-cap": {
        "messages": [
            {"role": "user", "content": "Migrate every module to the new config loader"},
            {"role": "assistant", "content": "I converted the first two modules and listed the rest."},
        ],
        "run_receipts": [{"run": "r3", "stop_reason": "turn_limit", "completed": False,
                          "turns_exhausted": True, "turns": 8, "max_turns": 8, "edited": True,
                          "verified": False, "canceled": False, "error": "",
                          "decision": {"intent": "build", "verification": "auto"}}],
    },
    "s-read": {
        "messages": [
            {"role": "user", "content": "Read README.md and tell me what this tool does"},
            {"role": "assistant", "content": "It reads a CSV and prints per-category totals."},
        ],
        "run_receipts": [{"run": "r1", "stop_reason": "completed", "completed": True,
                          "turns": 1, "edited": False, "verified": False, "canceled": False,
                          "error": "", "decision": {"intent": "build", "verification": "auto"}}],
    },
}

RUNS = [{"session": "s-cap", "run": "r3", "state": "done", "stop_reason": "turn_limit",
         "verified": False, "turns": 8, "ask": "Migrate every module to the new config loader",
         "started": time.time() - 30, "ended": time.time() - 5, "error": ""}]


class _Fixture(BaseHTTPRequestHandler):
    stream_requests = []                 # every /api/stream the page opened, in order
    route_requests = []
    queue_entries = {}
    queue_posts = []
    queue_starts = []
    uploads = []
    queue_release = threading.Event()
    queue_ack = threading.Event()
    queue_seen = threading.Event()
    queue_deliver = threading.Event()
    queue_fail_once = False
    queue_fail_all = False               # every save refused, for two-failure races
    upload_delay = 0.0                   # hold the pre-stream upload phase open
    upload_fail_once = False
    busy_delay = 0.0                     # hold the refusal open, so a race is observable
    start_delay = 0.0                    # hold an ACCEPTED start open, the same way
    queue_active = False
    queue_status_extra = {}
    recovery_states = {}                 # session id -> /api/recovery/<sid> body, else 404
    recovery_center_fails = False        # the control-plane snapshot is unavailable
    recovery_delay = 0.0                 # hold a recovery read open, so leaving is observable
    recovery_extra_items = []            # snapshot rows beside the session-derived ones
    recovery_only_extra = False          # ...and, when set, instead of them
    recovery_status = ""                 # force a snapshot `status`, including one nobody knows
    reconcile_posts = []                 # nothing in this UI may ever write one of these
    repair_posts = []                    # nor one of these: no repair/test/retry runs by itself
    # A restarted server rotates the process token the page was loaded with. Only the recovery
    # reads are guarded, so an armed test isolates exactly that path: `valid_token` is what the
    # server accepts now, `session_token_value` is what /api/session-token hands back, and
    # `guarded_reads` records (path, accepted) for every attempt, retries included.
    guard_token = False
    guarded_paths = ("/api/recovery/", "/api/recovery-center")
    valid_token = TOKEN
    session_token_value = TOKEN
    token_refresh_fails = False
    guarded_reads = []
    # The phone's Activity sheet reads these two lanes (and nothing else) for its decisions.
    activity = {}
    health = {}
    activity_delay = 0.0                 # hold an activity read open, so leaving is observable
    token_refreshes = 0                  # /api/session-token attempts, storms included
    guarded_writes = []                  # (path, accepted) for every write that reached the server
    lang = "en"                          # what /api/settings reports, so t() can be exercised

    def log_message(self, *_a):
        pass

    # -- plumbing ---------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        with open(os.path.join(WEBUI, name), "rb") as fh:
            body = fh.read()
        if name.endswith(".html"):
            meta = ('<meta name="collie-token" content="%s">\n' % TOKEN).encode()
            body = body.replace(b'<meta charset="utf-8">',
                                b'<meta charset="utf-8">\n' + meta, 1)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, kind, data):
        self.wfile.write(("event: %s\ndata: %s\n\n" % (kind, json.dumps(data))).encode())
        self.wfile.flush()

    # -- routes -----------------------------------------------------------
    def do_POST(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        body = json.loads(raw or b"{}")
        if _Fixture.guard_token:
            # A rotated token refuses writes exactly as it refuses reads. Every attempt is
            # recorded, refused ones included, so a silent replay cannot hide here.
            accepted = (query.get("token") or [""])[0] == _Fixture.valid_token
            _Fixture.guarded_writes.append((path, accepted))
            if not accepted:
                return self._json({"error": "stale session token"}, 403)
        if path == "/api/route":
            _Fixture.route_requests.append(path)
            return self._json({"kind": "chat"})
        if path == "/api/upload":
            time.sleep(_Fixture.upload_delay)
            _Fixture.uploads.append(body)
            if _Fixture.upload_fail_once:
                _Fixture.upload_fail_once = False
                return self._json({"error": "upload unavailable"}, 503)
            return self._json({"id": "upload-%d" % len(_Fixture.uploads)})
        if path == "/api/task-inbox":
            _Fixture.queue_posts.append(body)
            key = body["id"]
            _Fixture.queue_seen.set()
            _Fixture.queue_ack.wait(15)
            if _Fixture.queue_fail_once or _Fixture.queue_fail_all:
                # A refused save records nothing, so a listing cannot show it as queued.
                _Fixture.queue_fail_once = False
                return self._json({"error": "temporary response failure"}, 503)
            entry = _Fixture.queue_entries.setdefault(key, dict(body, seq=len(_Fixture.queue_entries)+1,
                state="pending", digest="v1", metadata={}))
            return self._json({"accepted": True, "entry": entry, "session": body["session"]})
        if path in ("/api/task-inbox/edit", "/api/task-inbox/cancel"):
            entry = _Fixture.queue_entries[body["id"]]
            if path.endswith("edit"):
                if body["expected_digest"] != entry["digest"]:
                    return self._json({"error": "request changed elsewhere"}, 409)
                entry.update(text=body["text"], digest="v2")
            else:
                entry["state"] = "canceled"
            return self._json({"entry": entry})
        if path == "/api/task-inbox/start":
            _Fixture.queue_starts.append(body)
            return self._json({"started": True, "session": body["session"]})
        if path == "/api/recovery/reconcile":
            _Fixture.reconcile_posts.append(body)
            return self._json({"ok": True})
        if path == "/api/doctor/repair":
            _Fixture.repair_posts.append(body)
            return self._json({"ok": True})
        return self._json({})

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
        if path == "/api/session-token":
            _Fixture.token_refreshes += 1
            if _Fixture.token_refresh_fails:
                return self._json({"error": "no session"}, 503)
            return self._json({"token": _Fixture.session_token_value})
        if _Fixture.guard_token and path.startswith(_Fixture.guarded_paths):
            accepted = (query.get("token") or [""])[0] == _Fixture.valid_token
            _Fixture.guarded_reads.append((path, accepted))
            if not accepted:
                return self._json({"error": "stale session token"}, 403)
        if path == "/api/activity":
            time.sleep(_Fixture.activity_delay)
            return self._json(_Fixture.activity)
        if path == "/api/healthz":
            return self._json(_Fixture.health)
        if path == "/m":
            return self._file("mobile.html", "text/html; charset=utf-8")
        if path == "/logo.svg":
            return self._file("logo.svg", "image/svg+xml")
        if path == "/api/whoami":
            return self._json({"name": "Collie", "machine": "fixture", "avatar": "",
                               "name_source": "explicit", "name_editable": False})
        if path == "/api/models":
            return self._json({"current": "anthropic:mock",
                               "entries": [{"provider": "anthropic", "model": "mock", "auth": "ok",
                                            "label": "Mock", "kind": "metered", "via": "api",
                                            "id": "anthropic:mock"}]})
        if path == "/api/settings":
            return self._json({"values": {"LANG": _Fixture.lang}})
        if path == "/api/sessions":
            return self._json({"sessions": SESSIONS})
        if path.startswith("/api/session/"):
            return self._json(TRANSCRIPTS.get(path.rsplit("/", 1)[-1], {"messages": []}))
        if path == "/api/runs":
            return self._json({"runs": RUNS})
        if path == "/api/task-inbox":
            sid = (query.get("session") or [""])[0]
            return self._json({"session": sid, "entries": [entry for entry in _Fixture.queue_entries.values()
                              if entry["session"] == sid], "active": _Fixture.queue_active,
                              **_Fixture.queue_status_extra})
        if path == "/api/recovery-center":
            if _Fixture.recovery_center_fails:
                return self._json({"error": "recovery snapshot unavailable"}, 503)
            items = [] if _Fixture.recovery_only_extra else [
                {"id": "interactive:" + sid, "kind": "interactive", "identity": sid,
                 "severity": "needs_you", "title": "Interrupted interactive action",
                 "detail": state.get("reason", ""),
                 "actions": ["completed", "not_fired", "cancel"]}
                for sid, state in sorted(_Fixture.recovery_states.items())]
            items += [dict(item) for item in _Fixture.recovery_extra_items]
            severe = [i for i in items if i["severity"] in ("needs_you", "error")]
            warning = [i for i in items if i["severity"] == "warning"]
            return self._json({"status": _Fixture.recovery_status or (
                                   "needs_you" if severe else "warning" if items else "ok"),
                               "summary": {"total": len(items), "needs_you": len(severe),
                                           "warning": len(warning)},
                               "items": items})
        if path.startswith("/api/recovery/"):
            time.sleep(_Fixture.recovery_delay)
            sid = path[len("/api/recovery/"):]
            state = _Fixture.recovery_states.get(sid)
            if state is None:
                return self._json({"error": "no active recovery state"}, 404)
            return self._json(dict(state, session_id=sid))
        if path == "/api/stream":
            return self._stream(query)
        if path.startswith("/api/"):
            return self._json({})
        return self._json({}, 404)

    def _stream(self, query):
        text = (query.get("q") or [""])[0]
        _Fixture.stream_requests.append({"q": text,
                                         "session": (query.get("session") or [""])[0]})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            if text == "Hold queue fixture":
                _Fixture.queue_active = True
                self._sse("start", {"session": "s-read", "run": "queue-run", "model": "mock",
                                    "prior_turns": 0, "worker_capabilities": {"steer": True}})
                deadline = time.monotonic() + 20
                while not _Fixture.queue_release.wait(.02) and time.monotonic() < deadline:
                    if _Fixture.queue_deliver.is_set():
                        _Fixture.queue_deliver.clear()
                        for entry in list(_Fixture.queue_entries.values()):
                            if entry["mode"] == "steer" and entry["state"] == "pending":
                                entry["state"] = "consumed"
                                self._sse("steer", {"session": "s-read", "id": entry["id"],
                                                    "text": entry["text"], "state": "consumed"})
                _Fixture.queue_active = False
                self._sse("done", dict(DONE_BASE, session="s-read", run="queue-run", answer="Partial work",
                    canceled=True, error="", stop_reason="canceled", completed=False))
                return
            if text.startswith("Accepted fixture"):
                # An accepted start that takes its time: the answer is `start`, not `busy`.
                sid = (query.get("session") or [""])[0] or "s-read"
                time.sleep(_Fixture.start_delay)
                self._sse("start", {"session": sid, "run": "slow-run", "model": "mock",
                                    "prior_turns": 0, "worker_capabilities": {"steer": True}})
                self._sse("done", dict(DONE_BASE, session=sid, run="slow-run", answer="Done."))
                return
            if text.startswith("Malformed fixture"):
                # A terminal frame this page cannot parse, before any `start`. Not an
                # acceptance, and not proof of anything else either.
                time.sleep(_Fixture.busy_delay)
                self.wfile.write(b"event: done\ndata: {not json at all\n\n")
                self.wfile.flush()
                return
            if text.startswith("Prestart error fixture"):
                # An error emitted before the run exists: web_tasks answers this the same
                # way it answers a bad lease, without a `start`.
                sid = (query.get("session") or [""])[0] or "s-read"
                time.sleep(_Fixture.busy_delay)
                self._sse("done", {"session": sid, "answer": "", "error": "workspace is gone"})
                return
            if text.startswith("Fenced fixture"):
                # What /api/run answers for a conversation with an unresolved recovery fence:
                # refused before the journal is read, so there is no `start`, no run row and
                # nothing executed. The refusal carries the fence, not a result.
                sid = (query.get("session") or [""])[0] or "s-read"
                state = _Fixture.recovery_states.get(sid) or _fence(sid, "r-earlier", FENCE_REASON)
                time.sleep(_Fixture.busy_delay)
                self._sse("done", {"session": sid, "answer": "",
                                   "error": state.get("reason") or "recovery required",
                                   "recovery_required": True, "recovery": state})
                return
            if text.startswith("Busy fixture"):
                # What web_tasks.serve_managed_stream answers when the session's
                # execution lease is already held: refused before the journal is
                # read, so there is no run row, no start frame and nothing sent.
                sid = (query.get("session") or [""])[0] or "s-read"
                time.sleep(_Fixture.busy_delay)
                self._sse("done", {"session": sid, "answer": "", "busy": True,
                                   "error": "this session already has an active run",
                                   "owner": "collie web on this machine"})
                return
            for kind, payload in _script(text):
                if kind == "done":
                    time.sleep(0.25)     # a beat of real "running", so live state is observable
                self._sse(kind, payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        br = p.chromium.launch()
        yield br
        br.close()


def await_run(page):
    """Wait for a run to actually start before waiting for it to end.

    A single "not running any more" wait could pass before the run had begun. The pill's `live` class
    is the signal rather than its text, which is translated.
    """
    live = "() => document.getElementById('statePill').classList.contains('live')"
    page.wait_for_function(live, timeout=8000)
    page.wait_for_function("() => !(" + live[6:] + ")", timeout=8000)
    page.wait_for_timeout(200)


class Page:
    """One loaded task surface, with its JS errors collected."""

    def __init__(self, page, errors):
        self.page = page
        self.errors = errors

    def ask(self, text):
        self.page.fill("#input", text)
        self.page.press("#input", "Enter")
        await_run(self.page)

    def title(self):
        return self.page.inner_text("#pageTitle")

    def gate_state(self):
        return self.page.get_attribute("#gate", "data-state")

    def gate_visible(self):
        return self.page.is_visible("#gate")

    def log_text(self):
        return self.page.inner_text("#log")


def _reset_fixture_state():
    """Every surface (desktop tab or phone client) starts from the same clean server state."""
    _Fixture.stream_requests = []
    _Fixture.route_requests = []
    _Fixture.queue_entries = {}; _Fixture.queue_posts = []; _Fixture.queue_starts = []
    _Fixture.uploads = []
    _Fixture.queue_release = threading.Event(); _Fixture.queue_ack = threading.Event()
    _Fixture.queue_ack.set(); _Fixture.queue_seen = threading.Event()
    _Fixture.queue_deliver = threading.Event()
    _Fixture.queue_fail_once = False; _Fixture.queue_active = False
    _Fixture.queue_fail_all = False; _Fixture.upload_delay = 0.0
    _Fixture.upload_fail_once = False
    _Fixture.busy_delay = 0.0
    _Fixture.start_delay = 0.0
    _Fixture.queue_status_extra = {}
    _Fixture.recovery_states = {}
    _Fixture.recovery_center_fails = False
    _Fixture.recovery_delay = 0.0
    _Fixture.recovery_extra_items = []
    _Fixture.recovery_only_extra = False
    _Fixture.recovery_status = ""
    _Fixture.reconcile_posts = []
    _Fixture.repair_posts = []
    _Fixture.guard_token = False
    _Fixture.guarded_paths = ("/api/recovery/", "/api/recovery-center")
    _Fixture.valid_token = TOKEN; _Fixture.session_token_value = TOKEN
    _Fixture.token_refresh_fails = False; _Fixture.guarded_reads = []
    _Fixture.activity = {}; _Fixture.health = {}
    _Fixture.activity_delay = 0.0; _Fixture.token_refreshes = 0
    _Fixture.guarded_writes = []


@pytest.fixture
def ui(server, browser):
    _reset_fixture_state()
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(400)          # identity + model probes settle; onboarding must stay shut
    assert not page.is_visible("#obOverlay.open"), "fixture should look configured"
    yield Page(page, errors)
    _Fixture.queue_release.set(); _Fixture.queue_ack.set()
    assert errors == [], "JS errors: %r" % errors
    context.close()


def test_refused_delete_keeps_open_thread_and_shows_server_reason(ui):
    page = ui.page
    row = page.locator(".thread").first
    row.click()
    page.wait_for_timeout(250)
    title = ui.title()
    page.route("**/api/delete/**", lambda route: route.fulfill(
        status=409, content_type="application/json",
        body=json.dumps({"error": "This conversation still has pending requests."})))
    page.on("dialog", lambda dialog: dialog.accept())
    row.get_by_role("button", name="Delete thread").click()
    page.wait_for_function("document.querySelector('.thread-error')?.textContent.includes('pending requests')")
    assert ui.title() == title
    assert page.locator(".thread.active").count() == 1
    assert "pending requests" in page.locator(".thread-error").inner_text()


# ------------------------------------------------- the registry behind the sidebar
class _Res:
    def __init__(self, **kw):
        self.turns, self.verified, self.error, self.canceled = 2, False, "", False
        self.turns_exhausted = self.budget_exhausted = False
        self.__dict__.update(kw)


def _end(session, **kw):
    from harness.webapp import Handler
    with Handler._runs_lock:
        Handler._runs.clear()
    Handler._run_begin(session, "ask", "/tmp")
    Handler._run_end(session, **kw)
    row = Handler._runs_snapshot()[0]
    with Handler._runs_lock:
        Handler._runs.clear()
    return row


def test_registry_publishes_a_stop_reason_without_breaking_state():
    """`state` has three verdicts and several consumers. The cap goes beside it, not inside it."""
    assert _end("a", res=_Res(turns=4, verified=True))["stop_reason"] == "completed"
    assert _end("b", error="RuntimeError: boom")["stop_reason"] == "error"
    assert _end("c", canceled=True)["stop_reason"] == "canceled"

    capped = _end("d", res=_Res(turns=8, turns_exhausted=True))
    assert capped["stop_reason"] == "turn_limit"
    assert capped["state"] == "done", "existing consumers still see a terminal 'done'"

    broke = _end("e", res=_Res(budget_exhausted=True))
    assert broke["stop_reason"] == "budget_limit" and broke["state"] == "done"

    # A cancellation observed by the host outranks whatever the result object says.
    assert _end("f", res=_Res(turns_exhausted=True), canceled=True)["stop_reason"] == "canceled"


# ------------------------------------------------------------------ tests
def test_plan_route_shows_plan_and_a_metered_route_shows_the_amount(ui):
    """The cost tile must not turn a subscription estimate into a bill — either way.

    This is what the `subscription` flag on the terminal frame is for, so it is
    also the check that says the flag is worth getting right on the server.
    """
    cost = "#rCost", "el => [el.textContent, el.title]"

    ui.ask("Summarise the ledger module on my plan")
    text, title = ui.page.eval_on_selector(*cost)
    assert text == "plan", "a plan run must not print a currency amount as its charge"
    assert "actual marginal charge was not independently observed" in title
    assert "$0.42" in title, "the estimate stays visible, named as an estimate"

    ui.ask("Summarise the ledger module on the metered key")
    text, title = ui.page.eval_on_selector(*cost)
    assert text == "$0.42" and title == ""


def test_read_only_answer_shows_no_check_card(ui):
    """A question that changed nothing is not "No executed check" — it has nothing to check."""
    ui.ask("Read README.md and tell me what this tool does")
    assert not ui.gate_visible(), "the evidence card must stay away from a read-only answer"
    assert "No executed check" not in ui.log_text()
    assert "It reads a CSV" in ui.log_text()
    assert _Fixture.route_requests == [], "normal messages start a run without a classifier call"


def test_header_follows_the_task_not_the_surface(ui):
    assert ui.title() == "Today"
    ui.page.fill("#input", "Read README.md and tell me what this tool does")
    ui.page.press("#input", "Enter")
    # Named the moment the request is made, from the text the user already wrote — not after a
    # round trip, and never by asking a model for a title.
    ui.page.wait_for_timeout(120)
    assert ui.title() == "Read README.md and tell me what this tool does"
    await_run(ui.page)
    assert ui.title() == "Read README.md and tell me what this tool does"
    # and the sidebar row for the same thread says exactly the same thing
    rows = [r.inner_text() for r in ui.page.query_selector_all(".thread .t-last")]
    assert "Read README.md and tell me what this tool does" in rows


def test_edit_with_executed_check_shows_that_check(ui):
    ui.ask("Fix the parser off-by-one")
    assert ui.gate_visible()
    assert ui.gate_state() == "pass"
    assert "pytest -q tests/test_parser.py" in ui.page.inner_text("#gateSub")


def test_required_verification_still_says_when_no_check_ran(ui):
    """Hiding the card for read-only answers must not also hide a broken promise."""
    ui.page.evaluate("""() => {
        document.getElementById('runVerification').value = 'required';
        document.getElementById('verifyCommand').value = 'pytest -q';
    }""")
    ui.page.fill("#input", "Tighten the config loader")
    ui.page.press("#input", "Enter")
    ui.page.wait_for_function(
        "() => document.getElementById('gate').getAttribute('data-state') === 'pending'",
        timeout=8000)
    await_run(ui.page)
    assert ui.gate_visible()
    assert ui.gate_state() == "missing"
    assert "Required check did not run" in ui.page.inner_text("#gateStatus")


# ---------------------------------------------------------------- exit 0, certifies nothing
# The receipt distinguishes "the command exited 0" (`command_passed`) from "and that result
# covers the files that exist now" (`passed`). The evidence card, the timeline row and the
# run's own error line all used to read only `passed`, so a green check that wrote its own
# build output into the workspace it graded was announced as a failure — with "exit 0"
# printed beside it. Those two claims cannot both be shown to a person at once.
def test_an_exit_zero_check_that_certifies_nothing_is_not_called_a_failure(ui):
    ui.ask("Bundle the release assets")

    assert ui.gate_visible()
    assert ui.gate_state() == "inconclusive", "not a pass, and not a failure either"
    status = ui.page.inner_text("#gateStatus")
    assert "Check did not certify this result" in status
    assert "failed" not in status.lower(), status
    sub = ui.page.inner_text("#gateSub")
    assert "npm run build" in sub, "the card still names the command that ran"
    assert "exit 0 does not certify this result" in sub
    assert "the workspace changed while it was running" in sub


def test_the_timeline_row_says_what_the_uncertified_check_established(ui):
    ui.ask("Bundle the release assets")
    ui.page.click("#tabTimeline") if ui.page.query_selector("#tabTimeline") else None
    timeline = ui.page.inner_text("#timeline")

    assert "did not certify this result" in timeline
    assert "proposed check failed" not in timeline, timeline
    # The old row printed "· exit 0" next to the word "failed"; the reason replaces it.
    assert "the workspace changed while it was running" in timeline


def test_a_check_that_really_failed_is_still_reported_as_a_failure(ui):
    """The repair must not turn every unmet check into a softer word."""
    ui.ask("Break the release build")

    assert ui.gate_state() == "fail"
    assert "Check failed" in ui.page.inner_text("#gateStatus")
    assert "proposed check failed" in ui.page.inner_text("#timeline")
    assert "required check failed: pytest -q (exit 1)" in ui.log_text()


def test_a_reopened_thread_restates_the_uncertified_check_from_its_receipt(ui, monkeypatch):
    """The durable receipt carries both facts, so reopening must not re-flatten them."""
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [
            {"role": "user", "content": "Bundle the release assets"},
            {"role": "assistant", "content": "Bundled the release assets."},
        ],
        "run_receipts": [{"run": "r-unc", "stop_reason": "error", "completed": False,
                          "edited": True, "verified": False, "canceled": False,
                          "error": UNCERTIFIED_ERROR,
                          "verification_evidence": UNCERTIFIED_EVIDENCE,
                          "decision": {"intent": "build", "verification": "required"}}],
    })
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function(
        "() => document.getElementById('gate').getAttribute('data-state') === 'inconclusive'",
        timeout=8000)

    assert "Check did not certify this result" in ui.page.inner_text("#gateStatus")
    assert "npm run build" in ui.page.inner_text("#gateSub")
    assert not _Fixture.stream_requests, "reopening a thread must not execute a turn"


def test_the_uncertified_headline_is_amber_like_unverified_not_failure_red(ui):
    """An inconclusive check has its own caution styling, separate from a failed check.

    The dot and the border were already amber; the headline had no rule of its own, so the
    one line a person reads stayed default-coloured beside them.
    """
    ui.ask("Bundle the release assets")

    colors = ui.page.evaluate("""() => {
        const probe = (v) => {
          const el = document.createElement('span');
          el.style.color = 'var(--' + v + ')';
          document.body.appendChild(el);
          const c = getComputedStyle(el).color; el.remove(); return c;
        };
        return {status: getComputedStyle(document.getElementById('gateStatus')).color,
                amber: probe('amber'), coral: probe('coral')};
    }""")

    assert colors["status"] == colors["amber"], colors
    assert colors["status"] != colors["coral"], "inconclusive is not a failed-check verdict"


@pytest.mark.parametrize("lang,headline,why,row", [
    ("zh", "当前结果尚未通过验证", "它运行期间工作区发生了变化", "建议的检查尚未确认当前结果"),
    ("zh-tw", "目前結果尚未通過驗證", "它執行期間工作區發生了變化", "建議的檢查尚未確認目前結果"),
])
def test_the_uncertified_check_speaks_the_reader_s_language(server, browser, lang, headline,
                                                            why, row):
    """A Chinese reader got the English sentence, which is the same failure in a new place:
    the one surface that explains why exit 0 settled nothing was unreadable to them."""
    _reset_fixture_state()
    _Fixture.lang = lang
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        page.wait_for_timeout(300)
        page.fill("#input", "Bundle the release assets")
        page.press("#input", "Enter")
        await_run(page)

        assert page.get_attribute("#gate", "data-state") == "inconclusive"
        assert page.inner_text("#gateStatus") == headline
        sub = page.inner_text("#gateSub")
        assert "npm run build" in sub, "the command itself is not translated away"
        assert why in sub
        assert "certify" not in sub and "exit 0" not in sub, sub
        timeline = page.inner_text("#timeline")
        assert row in timeline and why in timeline
        assert "did not certify" not in timeline, timeline
        assert errors == [], "JS errors: %r" % errors
    finally:
        _Fixture.lang = "en"
        context.close()


def test_turn_limit_keeps_output_and_offers_one_resume(ui):
    ui.ask("Migrate every module to the new config loader")
    text = ui.log_text()
    assert "I converted the first two modules" in text, "a capped run keeps the work it did"
    assert "Stopped at the turn limit" in text
    # The pill carries the verdict instead of resetting to a bare idle/done.
    assert ui.page.inner_text("#stateText") == "turn limit"
    # An edited-but-unchecked run says exactly that, neutrally.
    assert ui.gate_state() == "unverified"

    assert len(_Fixture.stream_requests) == 1
    ui.page.click(".stop-note button")
    await_run(ui.page)
    assert len(_Fixture.stream_requests) == 2, "Continue starts exactly one more turn"
    resumed = _Fixture.stream_requests[1]
    assert resumed["session"] == "s-cap", "resume stays in the same thread"
    assert "continue" in resumed["q"].lower()
    assert "Converted the remaining modules." in ui.log_text()
    assert ui.page.is_disabled(".stop-note button"), "the resume offer is spent once used"


def test_budget_limit_does_not_offer_to_spend_more(ui):
    ui.ask("Audit the whole monorepo for dead code")
    text = ui.log_text()
    assert "Audited two packages before the cap." in text
    assert "Stopped at the budget limit" in text
    assert ui.page.query_selector(".stop-note button") is None, \
        "raising a budget is the user's decision, never a button Collie offers"
    assert ui.page.inner_text("#stateText") == "budget limit"


def test_child_investigation_is_visible_without_its_transcript(ui):
    ui.ask("Why is login slow?")
    text = ui.log_text()
    assert "Investigat" in text, "a delegated investigation must not be dead air"
    assert "3 turns" in text and "5 model calls" in text
    assert "Trace the login request path" in text          # the subtask, which the user asked for
    assert "child-1" not in text                            # not the child's own run internals


def test_failed_compaction_is_housekeeping_not_a_failed_task(ui):
    ui.ask("Summarize the design doc")
    text = ui.log_text()
    assert "context" in text.lower()
    assert "continuing with the full history" in text
    assert "The doc proposes a two-stage pipeline." in text
    assert ui.gate_state() != "fail", "optional compaction failing is not the task failing"
    assert ui.page.query_selector(".msg .err") is None
    assert ui.page.inner_text("#stateText") == "idle"


def test_applied_compaction_reports_what_it_did(ui):
    ui.ask("Shrink the history and answer")
    text = ui.log_text()
    assert "earlier turns summarized" in text
    assert "150,000" in text and "42,000" in text


def test_switching_threads_never_shows_a_stale_title_or_status(ui):
    ui.ask("Read README.md and tell me what this tool does")
    assert ui.gate_state() in ("idle", None)

    rows = ui.page.query_selector_all(".thread")
    capped = [r for r in rows if "Migrate every module" in r.inner_text()][0]
    capped.click()
    ui.page.wait_for_timeout(400)
    assert ui.title() == "Migrate every module to the new config loader"
    assert "Stopped at the turn limit" in ui.log_text(), "a reopened capped run still says so"
    assert ui.page.inner_text("#stateText") == "turn limit"

    finished = [r for r in ui.page.query_selector_all(".thread")
                if "Read README.md" in r.inner_text()][0]
    finished.click()
    ui.page.wait_for_timeout(400)
    assert ui.title() == "Read README.md and tell me what this tool does"
    assert "Stopped at the turn limit" not in ui.log_text(), "the previous thread's verdict is gone"
    assert not ui.gate_visible()


def test_sidebar_calls_a_capped_run_paused_not_done(ui):
    ui.page.wait_for_timeout(400)       # the 2.5s registry poll seeds from /api/runs on load
    row = [r for r in ui.page.query_selector_all(".thread")
           if "Migrate every module" in r.inner_text()][0]
    state = row.query_selector(".t-state")
    assert state is not None
    label = state.inner_text().lower()          # the row is uppercased by CSS
    assert "done" not in label, "a run stopped by a cap is terminal but not finished"
    assert "paused" in label and "turn limit" in label


def test_reload_restores_the_thread_name_and_verdict(server, browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector(".thread", timeout=8000)
    row = [r for r in page.query_selector_all(".thread")
           if "Migrate every module" in r.inner_text()][0]
    row.click()
    page.wait_for_timeout(500)
    assert page.inner_text("#pageTitle") == "Migrate every module to the new config loader"
    assert "Stopped at the turn limit" in page.inner_text("#log")
    assert page.get_attribute("#gate", "data-state") == "unverified"
    assert errors == [], "JS errors: %r" % errors
    context.close()


@pytest.mark.parametrize("size", [{"width": 390, "height": 844}, {"width": 1280, "height": 900}])
def test_no_overflow_or_errors_at_either_size(server, browser, size):
    context = browser.new_context(viewport=size)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.fill("#input", "Migrate every module to the new config loader")
    page.press("#input", "Enter")
    await_run(page)
    overflow = page.evaluate(
        "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
    assert overflow <= 1, "horizontal overflow of %spx at %s" % (overflow, size)
    assert "Stopped at the turn limit" in page.inner_text("#log")
    assert errors == [], "JS errors: %r" % errors
    context.close()


def test_long_thread_name_cannot_push_the_header_off_screen(server, browser):
    context = browser.new_context(viewport={"width": 900, "height": 800})
    page = context.new_page()
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.fill("#input", "Read README.md " + ("and explain it very carefully " * 12))
    page.press("#input", "Enter")
    await_run(page)
    box = page.evaluate("""() => {
        const h = document.getElementById('pageTitle').getBoundingClientRect();
        return {right: h.right, width: document.documentElement.clientWidth};
    }""")
    assert box["right"] <= box["width"], "a long title must clamp, not widen the toolbar"
    context.close()


@pytest.mark.parametrize("lang,stopped,unchecked,resume", [
    ("zh", "在轮次上限处停下", "有改动，未经检查", "继续这项任务"),
    ("zh-tw", "在輪次上限處停下", "有改動，未經檢查", "繼續這項任務"),
])
def test_new_status_copy_is_translated(server, browser, lang, stopped, unchecked, resume):
    """Chinese readers get the new verdicts in Chinese; the user's own title stays as they wrote it."""
    _Fixture.lang = lang
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        page.wait_for_timeout(300)
        page.fill("#input", "Migrate every module to the new config loader")
        page.press("#input", "Enter")
        await_run(page)
        text = page.inner_text("#log")
        assert stopped in text
        assert resume in text
        assert page.inner_text("#gateStatus") == unchecked
        assert page.inner_text("#pageTitle") == "Migrate every module to the new config loader"
        assert errors == [], "JS errors: %r" % errors
    finally:
        _Fixture.lang = "en"
        context.close()


def test_phone_client_names_the_cap_that_stopped_a_run(server, browser):
    """The phone is often where a result is actually read, so it must not call a capped run done."""
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/m?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        page.wait_for_timeout(300)
        page.fill("#input", "Migrate every module to the new config loader")
        page.press("#input", "Enter")
        page.wait_for_timeout(1200)
        body = page.inner_text("body")
        assert "I converted the first two modules" in body, "a capped run keeps its output"
        assert "Stopped at the turn limit" in body
        overflow = page.evaluate(
            "() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 1, "horizontal overflow of %spx on the phone client" % overflow
        assert errors == [], "JS errors: %r" % errors
    finally:
        context.close()


def test_stream_requests_are_never_duplicated(ui):
    """One send is one run. The resume affordance must not replay a queued message."""
    ui.ask("Read README.md and tell me what this tool does")
    assert len(_Fixture.stream_requests) == 1
    asked = [r["q"] for r in _Fixture.stream_requests]
    assert len(asked) == len(set(asked))
    assert not re.search(r"token=", ui.title() or "")


def test_reopened_active_session_receives_mirror_completion(ui):
    state = {"mirrors": 0, "reads": 0, "ended": False}

    def runs(route):
        row = dict(RUNS[0], state="done" if state["ended"] else "running",
                   stop_reason="completed" if state["ended"] else "", can_steer=True)
        route.fulfill(json={"runs": [row]})

    def transcript(route):
        state["reads"] += 1
        route.fulfill(json={"messages": [
            {"role": "user", "content": "Migrate every module to the new config loader"},
            {"role": "assistant", "content": "Finished after reconnect" if state["ended"] else "Still investigating"},
        ]})

    def mirror(route):
        state["mirrors"] += 1
        state["ended"] = True
        data = dict(session="s-cap", run="r3", stop_reason="completed", answer="Finished after reconnect")
        route.fulfill(content_type="text/event-stream", body="event: done\ndata: %s\n\n" % json.dumps(data))

    ui.page.route("**/api/runs", runs)
    ui.page.route("**/api/session/s-cap", transcript)
    ui.page.route("**/api/mirror?*", mirror)
    ui.page.reload(wait_until="domcontentloaded")
    ui.page.locator(".thread").filter(has_text="Migrate every module").click()
    ui.page.get_by_text("Finished after reconnect", exact=True).wait_for(timeout=8000)
    assert state["mirrors"] == 1 and state["reads"] >= 2
    assert not ui.page.locator("#send").evaluate("el => el.classList.contains('stop')")
    assert not ui.page.locator("#statePill").evaluate("el => el.classList.contains('live')")
    assert ui.page.locator("#stateText").inner_text() == "idle"


def test_late_language_settings_cannot_overwrite_the_open_thread_title(ui):
    ui.page.add_init_script("""(() => {
      const original = window.fetch.bind(window);
      const waiting = [];
      window.releaseLanguageSettings = () => waiting.forEach(resolve => resolve(
        new Response(JSON.stringify({values:{LANG:'en'}}), {headers:{'content-type':'application/json'}})));
      window.fetch = (url, options) => String(url).split('?')[0] === '/api/settings'
        ? new Promise(resolve => waiting.push(resolve))
        : original(url, options);
    })();""")
    ui.page.reload(wait_until="domcontentloaded")
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function("() => document.getElementById('log').textContent.includes('README')")
    before = ui.page.locator("#pageTitle").inner_text()
    assert "README" in before
    ui.page.evaluate("() => window.releaseLanguageSettings()")
    ui.page.wait_for_timeout(100)
    assert ui.page.locator("#pageTitle").inner_text() == before


def test_late_transcript_cannot_replace_the_newly_selected_thread(ui):
    ui.page.evaluate("""() => {
      const original = window.fetch.bind(window);
      window.fetch = (url, options) => String(url) === '/api/session/s-cap'
        ? new Promise(resolve => { window.releaseOldThread = () => resolve(new Response(
            JSON.stringify({messages:[{role:'user',content:'OLD THREAD MUST NOT APPEAR'}]}),
            {headers:{'content-type':'application/json'}})); })
        : original(url, options);
    }""")
    ui.page.locator(".thread").filter(has_text="Migrate every module").click()
    ui.page.wait_for_function("() => typeof window.releaseOldThread === 'function'")
    ui.page.locator(".thread").filter(has_text="Read README.md").click()
    ui.page.get_by_text("It reads a CSV and prints per-category totals.", exact=True).wait_for()
    ui.page.evaluate("() => window.releaseOldThread()")
    ui.page.wait_for_timeout(150)
    assert "OLD THREAD MUST NOT APPEAR" not in ui.log_text()
    assert ui.title() == "Read README.md and tell me what this tool does"


def test_internal_reminders_do_not_reappear_as_user_requests(ui):
    ui.page.route("**/api/session/s-read", lambda route: route.fulfill(json={
        "messages": [
            {"role": "user", "source": "harness", "kind": "verification_reminder",
             "content": "INTERNAL CHECK REMINDER"},
            {"role": "user", "content": "Read README.md and tell me what this tool does"},
            {"role": "assistant", "content": "It reads a CSV and prints per-category totals."},
        ]}))
    ui.page.locator(".thread").filter(has_text="Read README.md").click()
    ui.page.get_by_text("It reads a CSV and prints per-category totals.", exact=True).wait_for()
    assert "INTERNAL CHECK REMINDER" not in ui.log_text()


@pytest.mark.parametrize("ending", ["cancel", "error"])
def test_interrupted_run_keeps_text_tools_and_buffered_partial_answer(ui, ending):
    ui.ask("Interrupt this review with " + ending)
    text = ui.log_text()
    assert "I found the parsing boundary." in text
    assert "parser.py" in text
    assert "The next step would be a strict-date check." in text
    expected = "Run stopped by the user." if ending == "cancel" else "Provider connection was interrupted"
    assert expected in ui.page.inner_text(".interruption-note")
    assert not ui.gate_visible()
    assert ui.page.inner_text("#stateText") == ("stopped" if ending == "cancel" else "failed")


def test_canceled_edits_keep_the_unverified_evidence(ui):
    ui.ask("Interrupt the edit with cancel")
    assert ui.gate_state() == "unverified"
    assert "parser.py" in ui.log_text()


def test_skipped_check_is_not_shown_as_a_failed_check(ui):
    ui.ask("Stop before checking")
    assert not ui.gate_visible()
    assert "_[stopped by user]_" not in ui.log_text()
    assert ui.page.locator(".interruption-note").count() == 1


def test_a_stopped_required_check_keeps_progress_and_an_honest_verdict(ui):
    ui.ask("Cancel the required check")
    assert ui.gate_state() == "stopped"
    assert "The requested fix is ready." in ui.log_text()
    timeline = ui.page.locator("#timeline").text_content()
    assert "Running project check" in timeline
    assert "proposed check failed" not in timeline
    assert not ui.errors


def test_stop_does_not_automatically_launch_a_queued_follow_up(ui):
    ui.page.fill("#input", "Interrupt the current review with cancel")
    ui.page.press("#input", "Enter")
    ui.page.wait_for_function("() => document.getElementById('input').placeholder.includes('Queue') || "
                              "document.getElementById('input').placeholder.includes('Follow')")
    ui.page.fill("#input", "This follow-up must remain queued after Stop")
    ui.page.press("#input", "Enter")
    ui.page.wait_for_timeout(900)
    assert len(_Fixture.stream_requests) == 1


def _hold_queue(ui):
    ui.page.fill("#input", "Hold queue fixture")
    ui.page.press("#input", "Enter")
    ui.page.wait_for_function("() => document.getElementById('send').classList.contains('stop') && "
                              "!document.getElementById('input').disabled")
    ui.page.wait_for_timeout(100)


def test_accepted_long_input_survives_reload_and_can_be_edited_or_removed(ui):
    _hold_queue(ui)
    request = "保留完整要求：" + "需要检查边界。" * 900 + "最后一项：不要丢掉这句话。"
    ui.page.fill("#input", request)
    ui.page.press("#input", "Enter")
    ui.page.wait_for_function("() => document.getElementById('input').value === ''")
    row = ui.page.locator(".task-queue-row")
    assert row.count() == 1
    assert row.locator(".task-queue-text").text_content() == request
    assert _Fixture.queue_posts[0]["text"] == request
    _Fixture.queue_release.set()
    ui.page.wait_for_function("() => !document.getElementById('send').classList.contains('stop')")
    ui.page.reload(wait_until="load")
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.locator(".task-queue-row").wait_for()
    assert ui.page.locator(".task-queue-text").text_content() == request
    ui.page.locator("#taskQueue").get_by_role("button", name="Edit", exact=True).click()
    ui.page.get_by_label("Edit pending request").fill("修改后的完整要求")
    ui.page.locator("#taskQueue").get_by_role("button", name="Save", exact=True).click()
    ui.page.wait_for_function("() => document.querySelector('.task-queue-text')?.textContent === '修改后的完整要求'")
    assert next(iter(_Fixture.queue_entries.values()))["text"] == "修改后的完整要求"
    ui.page.locator("#taskQueue").get_by_role("button", name="Remove", exact=True).click()
    ui.page.wait_for_function("() => document.getElementById('taskQueue').hidden")
    assert next(iter(_Fixture.queue_entries.values()))["state"] == "canceled"
    assert len(_Fixture.stream_requests) == 1 and not _Fixture.queue_starts


def test_slow_acceptance_keeps_draft_and_cannot_erase_newer_typing(ui):
    _hold_queue(ui); _Fixture.queue_ack.clear()
    ui.page.fill("#input", "The first queued instruction")
    ui.page.press("#input", "Enter")
    assert _Fixture.queue_seen.wait(3)
    assert ui.page.input_value("#input") == "The first queued instruction"
    ui.page.fill("#input", "New words typed while saving")
    _Fixture.queue_ack.set()
    ui.page.locator(".task-queue-row").wait_for()
    assert ui.page.input_value("#input") == "New words typed while saving"
    assert len(_Fixture.queue_posts) == 1


def test_ambiguous_acceptance_retries_same_id_and_keeps_the_draft(ui):
    _hold_queue(ui); _Fixture.queue_fail_once = True
    ui.page.fill("#input", "Keep this queued instruction")
    ui.page.press("#input", "Enter")
    ui.page.wait_for_function("() => document.getElementById('taskQueueNotice').textContent.includes('draft is kept')")
    assert ui.page.input_value("#input") == "Keep this queued instruction"
    ui.page.press("#input", "Enter")
    ui.page.wait_for_function("() => document.getElementById('input').value === ''")
    assert len(_Fixture.queue_posts) == 2
    assert _Fixture.queue_posts[0]["id"] == _Fixture.queue_posts[1]["id"]
    assert len(_Fixture.queue_entries) == 1


def test_follow_up_stays_pending_after_stop_until_send_next(ui):
    _hold_queue(ui)
    ui.page.fill("#input", "Run the next task only when requested")
    ui.page.press("#input", "Enter")
    ui.page.locator(".task-queue-row").wait_for()
    _Fixture.queue_release.set()
    ui.page.wait_for_function("() => !document.getElementById('send').classList.contains('stop')")
    ui.page.locator("#taskQueueStart").click(timeout=5000)
    assert len(_Fixture.queue_starts) == 1
    assert _Fixture.queue_starts[0]["session"] == "s-read"
    assert len(_Fixture.stream_requests) == 1, "the client never starts a second SSE executor"


def test_queue_displays_detached_failure_and_foreign_process_ownership(ui):
    _hold_queue(ui)
    ui.page.fill("#input", "Waiting for the other window")
    ui.page.press("#input", "Enter")
    ui.page.locator(".task-queue-row").wait_for()
    _Fixture.queue_status_extra = {"owner_busy": True,
        "queue_error": {"error": "The accepted attachment could not be read; request kept."}}
    _Fixture.queue_release.set()
    ui.page.wait_for_function("() => !document.getElementById('send').classList.contains('stop')")
    ui.page.wait_for_function("() => document.getElementById('taskQueueNotice').textContent.includes('attachment could not be read')")
    assert ui.page.locator("#taskQueueStart").is_disabled()
    _Fixture.queue_status_extra = {"owner_busy": False, "queue_error": None}
    ui.page.wait_for_function("() => !document.getElementById('taskQueueStart').disabled")
    assert ui.page.locator("#taskQueueNotice").is_hidden()


def test_accepting_attachments_clears_only_the_images_actually_submitted(ui):
    import base64

    _hold_queue(ui); _Fixture.queue_ack.clear()
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")
    ui.page.locator("#fileInput").set_input_files({"name": "first.png", "mimeType": "image/png", "buffer": png})
    ui.page.locator("#attachStrip .thumb").wait_for()
    ui.page.fill("#input", "Inspect this queued image")
    ui.page.press("#input", "Enter")
    assert _Fixture.queue_seen.wait(3)
    assert ui.page.locator("#attachStrip .thumb").count() == 1
    ui.page.locator("#fileInput").set_input_files({"name": "second.png", "mimeType": "image/png", "buffer": png})
    ui.page.wait_for_function("() => document.querySelectorAll('#attachStrip .thumb').length === 2")
    _Fixture.queue_ack.set()
    ui.page.wait_for_function("() => document.querySelectorAll('#attachStrip .thumb').length === 1")
    assert _Fixture.queue_posts[0]["images"] == ["upload-1"]
    assert len(_Fixture.uploads) == 1


def test_reopened_canceled_multimodal_thread_preserves_its_request_and_status(ui, monkeypatch):
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [
            {"role": "user", "source": "user", "content": [
                {"type": "text", "text": "Inspect the attached diagram"},
                {"type": "image", "media_type": "image/png", "data": "aW1hZ2U="}]},
            {"role": "assistant", "content": "I inspected the labels.\n\n_[stopped by user]_"},
        ],
        "run_receipts": [{"run": "canceled", "canceled": True, "stop_reason": "canceled",
                          "completed": False, "edited": False, "error": ""}],
    })
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_selector(".interruption-note")
    assert "Inspect the attached diagram" in ui.log_text()
    assert "I inspected the labels." in ui.log_text()
    assert "_[stopped by user]_" not in ui.log_text()
    assert ui.page.locator('#log .msg.user img[src^="data:image/png"]').count() == 1
    assert ui.page.locator(".interruption-note").count() == 1


def test_refresh_restores_the_selected_conversation_without_another_run(ui):
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function("() => document.getElementById('log').textContent.includes('README')")
    assert "session=s-read" in ui.page.url
    ui.page.reload(wait_until="load")
    ui.page.wait_for_function("() => document.getElementById('log').textContent.includes('README')")
    assert ui.page.locator('.thread.active').count() == 1
    assert not _Fixture.stream_requests, "opening or reloading history must not execute a turn"
    ui.page.get_by_role("button", name="New task", exact=True).click()
    assert "session=" not in ui.page.url
    ui.page.reload(wait_until="load")
    assert not ui.page.locator('.thread.active').count()


def test_steering_is_only_labeled_delivered_after_the_model_boundary(ui):
    _hold_queue(ui)
    ui.page.fill("#input", "Keep the exact steering instruction")
    ui.page.press("#input", "Control+Shift+Enter")
    ui.page.locator(".task-queue-row").wait_for()
    assert _Fixture.queue_posts[0]["mode"] == "steer"
    assert "Waiting" in ui.page.locator("#taskQueue").inner_text()
    assert ui.page.locator(".steer-note").count() == 0
    _Fixture.queue_deliver.set()
    ui.page.locator(".steer-note").wait_for()
    assert "Keep the exact steering instruction" in ui.page.locator(".steer-note").inner_text()
    assert "steering delivered" in ui.page.locator(".steer-note").inner_text().lower()
    assert ui.page.locator(".task-queue-row").count() == 0


# ------------------------------------------------ the fence a terminal verdict can carry
#
# `recovery_required` means an external action may already have happened, so this conversation
# cannot continue until a person says what the outside world actually looks like. Before this,
# the only way to learn that from the task surface was to send the next message and be refused.
# None of these tests may see the page resolve, acknowledge or re-run anything by itself.
def test_canceled_run_with_an_open_fence_keeps_its_work_and_says_what_is_needed(ui):
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
    ui.ask("Deploy the release to staging")
    text = ui.log_text()
    assert "I started the deploy script." in text, "a fenced stop keeps the partial answer"
    assert "deploy.sh" in text, "and the tool evidence that made it uncertain"
    # The stop itself is still reported as a stop; the fence is the extra statement beside it.
    assert "Run stopped by the user." in ui.page.inner_text(".interruption-note")
    note = ui.page.locator(".recovery-note")
    assert note.count() == 1
    body = note.inner_text()
    assert "Check the outcome before continuing" in body
    assert "Progress is saved." in body
    assert FENCE_REASON in body, "the server's own reason is shown"
    assert _Fixture.reconcile_posts == [], "showing a fence must never resolve one"


def test_an_ordinary_clean_stop_gets_no_recovery_note(ui):
    """The negative control: a stop with nothing uncertain behind it stays a plain stop."""
    ui.ask("Interrupt this review with cancel")
    assert "Run stopped by the user." in ui.page.inner_text(".interruption-note")
    assert ui.page.locator(".recovery-note").count() == 0
    ui.ask("Read README.md and tell me what this tool does")
    assert ui.page.locator(".recovery-note").count() == 0


def test_a_finished_looking_result_still_reports_its_fence_and_renders_reason_as_text(ui):
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-pub", HOSTILE_REASON)
    ui.ask("Publish the changelog entry")
    assert "Published the changelog draft." in ui.log_text(), "the answer is kept"
    note = ui.page.locator(".recovery-note")
    assert note.count() == 1, "a completed-looking result with an open fence still warns"
    reason = ui.page.locator(".recovery-note .rc-reason")
    assert HOSTILE_REASON in reason.inner_text(), "hostile server text is shown verbatim, as text"
    assert ui.page.locator(".recovery-note img, .recovery-note script").count() == 0
    assert ui.page.evaluate("() => window.__fenced_xss || null") is None


def test_the_recovery_note_opens_this_conversations_own_recovery_controls(ui):
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
    _Fixture.recovery_states["s-other"] = _fence("s-other", "r-other", "unrelated thread")
    ui.ask("Deploy the release to staging")
    button = ui.page.locator(".recovery-note button")
    assert button.count() == 1
    assert button.inner_text() == "Open recovery controls"
    button.focus()
    ui.page.keyboard.press("Enter")                      # keyboard reaches the same action
    ui.page.wait_for_selector("#activityPanel:not([hidden])", timeout=8000)
    ui.page.wait_for_selector(".activity-row.is-focus", timeout=8000)
    assert "on" in (ui.page.get_attribute('[data-control-tab="recovery"]', "class") or "")
    focused = ui.page.locator(".activity-row.is-focus")
    assert focused.count() == 1, "only THIS conversation's row is singled out"
    assert "Interrupted interactive action" in focused.inner_text()
    assert FENCE_REASON in focused.inner_text()
    assert _Fixture.reconcile_posts == [], "navigating is not deciding"


def test_recovery_controls_say_so_when_this_conversation_is_not_listed(ui):
    """A button that lands on an empty page is a dead button. Name what was asked for."""
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
    ui.ask("Deploy the release to staging")
    _Fixture.recovery_states.clear()                     # reconciled from somewhere else meanwhile
    ui.page.locator(".recovery-note button").click()
    ui.page.wait_for_selector("#activityPanel:not([hidden])", timeout=8000)
    ui.page.wait_for_function(
        "() => document.getElementById('activityNotice').textContent.includes('s-fence')",
        timeout=8000)
    assert "not listed in recovery" in ui.page.inner_text("#activityNotice")


def test_a_fenced_send_keeps_the_draft_and_does_not_look_like_a_started_run(ui):
    _Fixture.recovery_states["s-read"] = _fence("s-read", "r-earlier", FENCE_REASON)
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function("() => document.getElementById('log').textContent.includes('README')")
    draft = "Fenced fixture: ship the second half"
    ui.page.fill("#input", draft)
    ui.page.press("#input", "Enter")
    # The refusal arrives before a run ever exists, so there is no running state to wait out.
    ui.page.wait_for_selector(".recovery-note", timeout=8000)
    ui.page.wait_for_timeout(250)
    assert ui.page.input_value("#input") == draft, "the refused request goes back to the composer"
    note = ui.page.locator(".recovery-note").last
    refusal = note.inner_text()
    assert "No new run was started." in refusal, "the refusal states the fence, not a result"
    assert FENCE_REASON in refusal
    # Nothing pretends a turn happened: no optimistic user bubble, no queued resend, no reconcile.
    assert draft not in ui.log_text()
    assert _Fixture.queue_posts == [] and _Fixture.queue_starts == []
    assert _Fixture.reconcile_posts == []
    assert not ui.page.locator("#statePill.live").count()


def test_a_reopened_thread_asks_the_server_before_repeating_a_fence(ui, monkeypatch):
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [{"role": "user", "content": "Deploy the release to staging"},
                     {"role": "assistant", "content": "I started the deploy script."}],
        "run_receipts": [{"run": "r-fence", "canceled": True, "stop_reason": "canceled",
                          "completed": False, "edited": False, "error": "",
                          "recovery_required": True}]})
    _Fixture.recovery_states["s-read"] = _fence("s-read", "r-fence", FENCE_REASON)
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_selector(".recovery-note", timeout=8000)
    assert FENCE_REASON in ui.page.inner_text(".recovery-note"), "the live reason, not the receipt's"
    assert "I started the deploy script." in ui.log_text()


def test_a_receipt_whose_fence_was_already_resolved_leaves_no_note(ui, monkeypatch):
    """The receipt is history. A fence reconciled since must not be re-announced as current."""
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [{"role": "user", "content": "Deploy the release to staging"},
                     {"role": "assistant", "content": "I started the deploy script."}],
        "run_receipts": [{"run": "r-fence", "canceled": True, "stop_reason": "canceled",
                          "completed": False, "edited": False, "error": "",
                          "recovery_required": True}]})
    _Fixture.recovery_states.clear()                     # /api/recovery/<sid> answers 404
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function("() => document.getElementById('log').textContent.includes('deploy')")
    ui.page.wait_for_timeout(600)
    assert ui.page.locator(".recovery-note").count() == 0
    assert "Run stopped by the user." in ui.page.inner_text(".interruption-note")


def test_one_note_per_fenced_run_and_never_in_another_thread(ui):
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-twice", FENCE_REASON)
    ui.ask("Restart the importer job")
    assert ui.page.locator(".recovery-note").count() == 1, "a redelivered verdict adds no second note"
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function("() => document.getElementById('log').textContent.includes('README')")
    ui.page.wait_for_timeout(400)
    assert ui.page.locator(".recovery-note").count() == 0, "a fence belongs to its own conversation"


def test_a_fenced_cap_keeps_its_work_but_does_not_offer_another_turn(ui):
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-cap", FENCE_REASON)
    ui.ask("Cap the fenced migration")
    assert "Converted the first module." in ui.log_text()
    assert "Stopped at the turn limit" in ui.log_text()
    assert ui.page.locator(".recovery-note").count() == 1
    assert ui.page.is_disabled(".stop-note button"), "continuing is refused while the fence is open"
    assert len(_Fixture.stream_requests) == 1, "and nothing is resent on the user's behalf"


@pytest.mark.parametrize("lang,title,button", [
    ("zh", "确认实际结果后继续", "打开恢复控制"),
    ("zh-tw", "確認實際結果後繼續", "開啟復原控制"),
])
def test_the_fence_speaks_the_reader_s_language(server, browser, lang, title, button):
    """Every visible word comes from the shipped dictionaries; the server's reason never does."""
    _Fixture.lang = lang
    _Fixture.recovery_states = {"s-fence": _fence("s-fence", "r-fence", FENCE_REASON)}
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        page.wait_for_timeout(300)
        page.fill("#input", "Deploy the release to staging")
        page.press("#input", "Enter")
        await_run(page)
        note = page.inner_text(".recovery-note")
        assert title in note and button in note
        assert FENCE_REASON in note, "the server's own reason is quoted, not translated"
        assert errors == [], "JS errors: %r" % errors
    finally:
        _Fixture.lang = "en"
        _Fixture.recovery_states = {}
        context.close()


def test_an_unreachable_recovery_route_is_labelled_not_guessed(ui, monkeypatch):
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [{"role": "user", "content": "Deploy the release to staging"},
                     {"role": "assistant", "content": "I started the deploy script."}],
        "run_receipts": [{"run": "r-fence", "canceled": True, "stop_reason": "canceled",
                          "completed": False, "edited": False, "error": FENCE_REASON,
                          "recovery_required": True}]})
    ui.page.route("**/api/recovery/s-read*", lambda route: route.abort())
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_selector(".recovery-note", timeout=8000)
    note = ui.page.inner_text(".recovery-note")
    assert "could not be confirmed" in note, "an unread fence is not presented as a fresh reading"
    assert FENCE_REASON in note


# ------------------------------------------- a stale process token is not an unknown fence
# A desktop window outlives the process it was loaded against: after a restart the server refuses
# its token until the page picks up a new one. The recovery reads have to take the same bounded
# refresh-once-and-retry-once path every other authenticated read takes, or reopening an
# interrupted receipt reports "could not be confirmed" for a fence the server could have answered
# for. Nothing here may reconcile, resend, or keep retrying.
def _reopen_fenced_thread(ui, monkeypatch, error=""):
    """Give the saved thread a history whose receipt records a fence, and open it."""
    monkeypatch.setitem(TRANSCRIPTS, "s-read", {
        "messages": [{"role": "user", "content": "Deploy the release to staging"},
                     {"role": "assistant", "content": "I started the deploy script."}],
        "run_receipts": [{"run": "r-fence", "canceled": True, "stop_reason": "canceled",
                          "completed": False, "edited": False, "error": error,
                          "recovery_required": True}]})
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()


def _rotate_token(handed_back="rotated-token"):
    """The server now accepts only a new token; /api/session-token hands back `handed_back`."""
    _Fixture.valid_token = "rotated-token"
    _Fixture.session_token_value = handed_back
    _Fixture.guarded_reads = []
    _Fixture.guard_token = True


def _reads(fragment, limit=2):
    """Accepted/refused verdicts for the guarded reads of one route, oldest first."""
    return [ok for path, ok in _Fixture.guarded_reads if fragment in path][:limit]


LIVE_REASON = "publish.sh may already have run"


def test_a_reopened_fenced_thread_refreshes_a_stale_token_and_reads_the_live_fence(ui, monkeypatch):
    _Fixture.recovery_states["s-read"] = _fence("s-read", "r-fence", LIVE_REASON)
    _rotate_token()
    _reopen_fenced_thread(ui, monkeypatch, error=FENCE_REASON)
    ui.page.wait_for_selector(".recovery-note", timeout=8000)
    note = ui.page.inner_text(".recovery-note")
    assert LIVE_REASON in note, "the server's current reason, read after the token was refreshed"
    assert "could not be confirmed" not in note, "a refused-then-refreshed read IS a fresh reading"
    assert _reads("/api/recovery/s-read") == [False, True], "one refusal, then one retry"
    assert _Fixture.reconcile_posts == []


def test_a_resolved_fence_stays_silent_even_when_the_token_had_to_be_refreshed(ui, monkeypatch):
    """404 is an answer: the fence is gone. Refreshing to get it must not invent a note."""
    _Fixture.recovery_states.clear()
    _rotate_token()
    _reopen_fenced_thread(ui, monkeypatch)
    ui.page.wait_for_function("() => document.getElementById('log').textContent.includes('deploy')")
    ui.page.wait_for_timeout(800)
    assert _reads("/api/recovery/s-read") == [False, True]
    assert ui.page.locator(".recovery-note").count() == 0, "an old fence is not re-announced"
    assert "Run stopped by the user." in ui.page.inner_text(".interruption-note")


@pytest.mark.parametrize("refresh_fails,expected", [(True, [False]), (False, [False, False])])
def test_a_token_that_cannot_be_made_fresh_falls_back_honestly_and_stops(ui, monkeypatch,
                                                                        refresh_fails, expected):
    """Bounded: one refresh attempt, at most one retry, then the receipt — labelled, not guessed."""
    _Fixture.recovery_states["s-read"] = _fence("s-read", "r-fence", LIVE_REASON)
    _rotate_token(handed_back="still-stale")
    _Fixture.token_refresh_fails = refresh_fails
    _reopen_fenced_thread(ui, monkeypatch, error=FENCE_REASON)
    ui.page.wait_for_selector(".recovery-note", timeout=8000)
    note = ui.page.inner_text(".recovery-note")
    assert "could not be confirmed" in note, "an unread fence is not presented as a fresh reading"
    assert FENCE_REASON in note, "the receipt's own recorded reason is what is quoted"
    assert LIVE_REASON not in note, "and never a reason the page never managed to read"
    assert _reads("/api/recovery/s-read", limit=9) == expected
    ui.page.wait_for_timeout(1500)
    assert _reads("/api/recovery/s-read", limit=9) == expected, "a refusal is not retried forever"
    assert _Fixture.reconcile_posts == []


def test_a_late_recovery_answer_does_not_follow_the_reader_into_another_thread(ui, monkeypatch):
    """The refresh makes the read slower, which makes the stale-session guards matter more."""
    _Fixture.recovery_states["s-read"] = _fence("s-read", "r-fence", LIVE_REASON)
    _rotate_token()
    _Fixture.recovery_delay = 0.9            # refusal and retry both land after the reader leaves
    _reopen_fenced_thread(ui, monkeypatch)
    ui.page.wait_for_timeout(150)
    ui.page.locator(".thread").filter(has_text="Migrate every module").first.click()
    ui.page.wait_for_function(
        "() => document.getElementById('log').textContent.includes('first two modules')")
    ui.page.wait_for_timeout(2800)
    assert _reads("/api/recovery/s-read") == [False, True], "the answer did arrive, late"
    assert ui.page.locator(".recovery-note").count() == 0, "into the thread that was left behind"
    assert LIVE_REASON not in ui.log_text()
    assert _Fixture.reconcile_posts == []


def test_the_recovery_center_still_lists_its_items_after_the_token_rotated(ui):
    """The read-only control-plane snapshot goes through the same path as the fence probe."""
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
    ui.ask("Deploy the release to staging")
    _rotate_token()                          # the process is replaced while the page sits open
    ui.page.locator(".recovery-note button").click()
    ui.page.wait_for_selector("#activityPanel:not([hidden])", timeout=8000)
    ui.page.wait_for_selector(".activity-row.is-focus", timeout=8000)
    focused = ui.page.inner_text(".activity-row.is-focus")
    assert "Interrupted interactive action" in focused and FENCE_REASON in focused
    assert _reads("/api/recovery-center") == [False, True]
    assert "could not refresh" not in ui.page.inner_text("#activityNotice")
    assert _Fixture.reconcile_posts == [], "reading the list is not deciding anything"


# ------------------------------- the same rotation, seen from a phone left open
#
# A phone page is the surface most likely to still be open when the desktop process is restarted:
# it sits in a background tab for days.  Its Activity sheet reads two lanes (/api/activity and
# /api/healthz) for the recovery decisions it shows, so a rotated token used to turn the whole
# sheet into "Activity is unavailable" until the reader reloaded by hand.  Those reads now take the
# same bounded refresh-once/retry-once path as the desktop's.  The decisions themselves are still
# written only by a person: no reconcile, steer or cancel POST is ever replayed because auth failed.
MOBILE_RECOVERY_REASON = "publish.sh may already have run"
PHONE_ACTIVITY = {
    "sessions": [{"session_id": "s-phone", "state": "recovery_required", "recovery_required": True,
                  "reason": MOBILE_RECOVERY_REASON}],
    "task_runs": [], "missions": [], "automations": [], "errors": {}}
PHONE_HEALTH = {"work": {"missions_active": 0, "task_runs_active": 0,
                         "recovery_required": ["s-phone"]}, "workers": {}, "services": {}}


@pytest.fixture
def phone(server, browser):
    """The mobile client, loaded with a token the server is about to stop accepting."""
    _reset_fixture_state()
    _Fixture.activity = dict(PHONE_ACTIVITY)
    _Fixture.health = dict(PHONE_HEALTH)
    _Fixture.guarded_paths = ("/api/activity", "/api/healthz")
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server + "/m?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(200)
    yield Page(page, errors)
    assert errors == [], "JS errors: %r" % errors
    context.close()


def _open_phone_activity(phone):
    phone.page.click("#topState")
    phone.page.wait_for_selector("#mobileActivity:not([hidden])", timeout=8000)


def _phone_notice(phone):
    return phone.page.inner_text("#mobileActivityNotice")


def test_the_phone_activity_sheet_refreshes_a_stale_token_and_reads_the_live_decisions(phone):
    _rotate_token()                          # the process is replaced while the phone sits open
    _open_phone_activity(phone)
    phone.page.wait_for_selector("#mobileRecoveryRows .ops-row", timeout=8000)
    row = phone.page.inner_text("#mobileRecoveryRows .ops-row")
    assert "s-phone" in row and MOBILE_RECOVERY_REASON in row, "the server's current decision"
    assert "Activity is unavailable" not in _phone_notice(phone)
    assert phone.page.inner_text("#mobileRecoveryCount") == "1"
    assert _reads("/api/activity") == [False, True], "one refusal, then one retry"
    assert _reads("/api/healthz") == [False, True]
    assert _Fixture.token_refreshes == 1, "two concurrent reads share one refresh"
    assert _Fixture.reconcile_posts == [] and _Fixture.guarded_writes == []


def test_a_phone_read_that_stays_refused_keeps_saying_so_without_a_refresh_storm(phone):
    """Bounded: one refresh, at most one retry per read, then honest uncertainty — and it stops."""
    _rotate_token(handed_back="still-stale")
    _open_phone_activity(phone)
    phone.page.wait_for_function(
        "() => document.getElementById('mobileActivityNotice').textContent"
        "        .includes('Activity is unavailable')", timeout=8000)
    assert phone.page.locator("#mobileRecoveryRows .ops-row").count() == 0, \
        "no rows are invented for data that was never read"
    phone.page.wait_for_timeout(1500)
    before = (len(_Fixture.guarded_reads), _Fixture.token_refreshes)
    phone.page.wait_for_timeout(1200)
    assert (len(_Fixture.guarded_reads), _Fixture.token_refreshes) == before, \
        "a refusal is not retried forever"
    assert _Fixture.token_refreshes <= 1, "and one stale window is one refresh, not a storm"
    assert _reads("/api/activity", limit=9) == [False, False], "refused, retried once, then stopped"


def test_a_late_phone_refusal_reuses_a_token_another_read_already_refreshed(phone, monkeypatch):
    refreshed_activity = threading.Event()
    released = []
    original = _Fixture.do_GET

    def staggered(handler):
        parsed = urlparse(handler.path)
        token = (parse_qs(parsed.query).get("token") or [""])[0]
        if parsed.path == "/api/healthz" and token == TOKEN:
            released.append(refreshed_activity.wait(4))
        result = original(handler)
        if parsed.path == "/api/activity" and token == "rotated-token":
            refreshed_activity.set()
        return result

    monkeypatch.setattr(_Fixture, "do_GET", staggered)
    _rotate_token()
    _open_phone_activity(phone)
    phone.page.wait_for_selector("#mobileRecoveryRows .ops-row", timeout=8000)
    assert released == [True], "the old health refusal arrived after activity reused the new token"
    assert _reads("/api/activity") == [False, True]
    assert _reads("/api/healthz") == [False, True]
    assert _Fixture.token_refreshes == 1, "reuse the refreshed token instead of asking again"
    assert _Fixture.guarded_writes == []


def test_a_stale_phone_never_replays_a_recovery_decision_or_a_specialist_command(phone):
    """Reads may be retried because they change nothing. A decision is a person's, once."""
    _open_phone_activity(phone)
    phone.page.wait_for_selector("#mobileRecoveryRows .ops-row", timeout=8000)
    _rotate_token()                          # the token goes stale with the sheet already open
    phone.page.on("dialog", lambda dialog: dialog.accept())
    phone.page.locator("#mobileRecoveryRows .ops-row button", has_text="Completed").click()
    phone.page.wait_for_function(
        "() => document.getElementById('mobileActivityNotice').textContent.includes('Action failed')",
        timeout=8000)
    phone.page.wait_for_timeout(900)
    assert _Fixture.reconcile_posts == [], "a refused decision was not recorded"
    assert _Fixture.guarded_writes == [("/api/recovery/reconcile", False)], \
        "one refused attempt, never replayed on a fresh token"
    assert _Fixture.token_refreshes == 0, "a refused write does not even go looking for a token"


def test_a_late_phone_activity_answer_does_not_land_in_a_closed_sheet(phone):
    """The refresh makes the read slower, which makes the latest-request guard matter more."""
    _rotate_token()
    _Fixture.activity_delay = 0.7            # refusal and retry both land after the sheet is closed
    _open_phone_activity(phone)
    phone.page.wait_for_timeout(150)
    phone.page.click("#mobileActivityClose")
    phone.page.wait_for_timeout(3000)
    assert _reads("/api/activity") == [False, True], "the answer did arrive, late"
    assert phone.page.is_hidden("#mobileActivity"), "and not into a sheet nobody has open"
    assert phone.page.locator("#mobileRecoveryRows .ops-row").count() == 0
    assert MOBILE_RECOVERY_REASON not in phone.page.inner_text("body")


def test_a_phone_whose_token_is_still_good_reads_each_lane_exactly_once(phone):
    """The repair is invisible when nothing is wrong: no extra request, no token round-trip."""
    _Fixture.guarded_reads = []
    _Fixture.guard_token = True              # armed, but the page's token is still the valid one
    _open_phone_activity(phone)
    phone.page.wait_for_selector("#mobileRecoveryRows .ops-row", timeout=8000)
    phone.page.wait_for_timeout(400)
    assert _reads("/api/activity", limit=9) == [True], "a successful read is one request"
    assert _reads("/api/healthz", limit=9) == [True]
    assert _Fixture.token_refreshes == 0, "and never asks for a token it did not need"


# ------------------------------- what a worker heartbeat on the phone actually means
#
# The Services lane on the phone renders `aggregate_health`'s worker rows.  Each row is one
# *desired* worker: `state` is the last thing the supervisor wrote, defaulting to "missing" when no
# heartbeat row exists at all, and `fresh` only says whether that row is still inside its TTL.
# Presenting that as `fresh ? Running : Recovery required` said two untrue things.  A stock server
# with no job workers showed five "Recovery required" rows while session recovery was empty --
# telling the reader to inspect an external system for an action nobody sent, which is also not
# something a person can reconcile.  And a *fresh* "failed"/"dead"/"circuit_open" beat -- a worker
# that reported its own failure seconds ago -- was shown as Running.
def _beat(state, fresh, age_s=3.0, pid=4242):
    """Exactly the shape harness.ops.aggregate_health serialises for one desired worker."""
    return {"state": state, "fresh": fresh, "age_s": age_s, "pid": pid, "detail": {}}


NO_HEARTBEAT = _beat("missing", False, age_s=None, pid=0)


def _phone_workers(phone, workers, services=None):
    """Open the sheet against a health snapshot carrying these worker rows."""
    _Fixture.health = dict(PHONE_HEALTH, workers=workers, services=services or {})
    _open_phone_activity(phone)
    phone.page.wait_for_selector("#mobileOtherActivity .ops-row", timeout=8000)
    return phone.page.eval_on_selector_all(
        "#mobileOtherActivity .ops-row",
        "rows => rows.map(row => ({"
        "  title: row.querySelector('.ops-row-title').textContent,"
        "  meta: (row.querySelector('.ops-row-meta') || {}).textContent || '',"
        "  state: row.querySelector('.ops-row-state').textContent,"
        "  tone: row.querySelector('.ops-row-state').className}))")


def test_a_worker_that_never_reported_is_not_a_recovery_decision_for_the_reader(phone):
    """A stock server with no job workers: cautionary, but nothing for a person to reconcile."""
    rows = _phone_workers(phone, {"jobd": NO_HEARTBEAT, "web": NO_HEARTBEAT})
    assert [row["state"] for row in rows] == ["No heartbeat", "No heartbeat"]
    assert all("Recovery required" not in row["state"] for row in rows), \
        "nothing was sent, so there is no uncertain external action to inspect"
    assert all("warn" in row["tone"] for row in rows), "cautionary, not a green Running"
    assert all("no heartbeat recorded" in row["meta"] for row in rows)
    lane = phone.page.inner_text("#mobileOtherActivity")
    assert "Background service status updates automatically." in lane
    # The genuine session decision is still the only thing asking a person to inspect anything.
    assert "s-phone" in phone.page.inner_text("#mobileRecoveryRows .ops-row")
    assert phone.page.locator("#mobileRecoveryRows .ops-row button").count() == 3
    assert _Fixture.reconcile_posts == [], "reading a health page decides nothing"


def test_a_fresh_failure_heartbeat_is_not_reported_as_running(phone):
    """The worker reported its own failure seconds ago. Freshness is not health."""
    rows = _phone_workers(phone, {"ambient": _beat("failed", True),
                                  "bridge": _beat("dead", True),
                                  "jobd": _beat("circuit_open", True)})
    assert [row["state"] for row in rows] == ["Failed", "Failed", "Restarts paused"]
    assert all("bad" in row["tone"] for row in rows), "an actionable failure, stated as one"
    assert "Running" not in phone.page.inner_text("#mobileOtherActivity")


def test_a_stale_failure_heartbeat_does_not_decay_into_something_softer(phone):
    """Going quiet after reporting a failure does not withdraw the failure."""
    rows = _phone_workers(phone, {"jobd": _beat("failed", False, age_s=240.0)})
    assert rows[0]["state"] == "Failed" and "bad" in rows[0]["tone"]
    assert "heartbeat 240s ago" in rows[0]["meta"], "and the silence is still visible"


def test_a_healthy_worker_still_just_says_running(phone):
    rows = _phone_workers(phone, {"web": _beat("running", True, age_s=2.4)},
                          services={"web": {"ok": True}})
    assert rows[0]["state"] == "Running" and rows[0]["tone"].strip() == "ops-row-state"
    assert "heartbeat 2s ago" in rows[0]["meta"]
    assert rows[1]["title"].startswith("web · Service") and rows[1]["state"] == "Running"


def test_a_stale_heartbeat_is_stale_and_keeps_the_last_state_it_reported(phone):
    """Not proof of anything external: the page says what it knows and when it knew it."""
    rows = _phone_workers(phone, {"jobd": _beat("running", False, age_s=95.0),
                                  "web": _beat("starting", True)})
    assert rows[0]["state"] == "Heartbeat stale" and "warn" in rows[0]["tone"]
    assert "heartbeat 95s ago" in rows[0]["meta"]
    assert "last known state: Running" in rows[0]["meta"]
    assert rows[1]["state"] == "Starting", "and a fresh transient state is shown as itself"


@pytest.mark.parametrize("state", ["shutdown_timeout", "__proto__", "constructor"])
def test_a_state_this_page_does_not_know_is_shown_as_uncertain_not_as_healthy(phone, state):
    rows = _phone_workers(phone, {"jobd": _beat(state, True)})
    assert rows[0]["state"] == state.replace("_", " "), "the producer's own word, not a guess"
    assert "warn" in rows[0]["tone"] and "unrecognised worker state" in rows[0]["meta"]


@pytest.mark.parametrize("lang,failed,missing,stale,note", [
    ("zh", "失败", "没有心跳", "心跳已过期", "后台服务状态会自动更新"),
    ("zh-tw", "失敗", "沒有心跳", "心跳已過期", "背景服務狀態會自動更新"),
])
def test_worker_states_speak_the_reader_s_language(server, browser, lang, failed, missing,
                                                   stale, note):
    _reset_fixture_state()
    _Fixture.lang = lang
    _Fixture.activity = dict(PHONE_ACTIVITY)
    _Fixture.health = dict(PHONE_HEALTH, workers={
        "ambient": _beat("failed", True), "bridge": NO_HEARTBEAT,
        "jobd": _beat("running", False, age_s=95.0), "web": _beat("running", True)})
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/m?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        page.click("#topState")
        page.wait_for_selector("#mobileOtherActivity .ops-row", timeout=8000)
        lane = page.inner_text("#mobileOtherActivity")
        assert failed in lane and missing in lane and stale in lane and note in lane
        assert "运行中" in lane or "執行中" in lane, "the healthy worker reads as running"
        assert "Recovery required" not in lane and "需要恢复" not in lane and "需要復原" not in lane
        assert errors == [], "JS errors: %r" % errors
    finally:
        _Fixture.lang = "en"
        context.close()


# ------------------------------------- the fence a person is already looking at
#
# A fenced thread shows its card *because* it is fenced, so the card is on screen for every refusal
# after the first. The refusal has to reach the reader through the card that is already there:
# a second card would say the same thing twice, and silence lets a refused Send look like a sent
# one. None of this may queue, resend, reconcile or take the keyboard away from the composer.
FENCED_THREAD = {
    "messages": [{"role": "user", "content": "Deploy the release to staging"},
                 {"role": "assistant", "content": "I started the deploy script."}],
    "run_receipts": [{"run": "r-fence", "canceled": True, "stop_reason": "canceled",
                      "completed": False, "edited": False, "error": "",
                      "recovery_required": True}]}


def _open_live_fenced_thread(ui, monkeypatch):
    """Reopen a thread whose fence is confirmed live, so its note is up before anything is typed."""
    monkeypatch.setitem(TRANSCRIPTS, "s-read", FENCED_THREAD)
    _Fixture.recovery_states["s-read"] = _fence("s-read", "r-fence", FENCE_REASON)
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_selector(".recovery-note", timeout=8000)
    assert ui.page.locator(".recovery-note .rc-status").count() == 0, "nothing refused yet"


def test_a_refused_send_speaks_through_the_fence_note_already_on_screen(ui, monkeypatch):
    _open_live_fenced_thread(ui, monkeypatch)
    draft = "Fenced fixture: ship the second half"
    ui.page.fill("#input", draft)
    ui.page.press("#input", "Enter")
    ui.page.wait_for_function(
        "() => { var line = document.querySelector('.recovery-note .rc-status');"
        "        return !!line && line.textContent.includes('No new run was started.'); }",
        timeout=8000)
    ui.page.wait_for_timeout(250)
    assert ui.page.locator(".recovery-note").count() == 1, "the one card speaks again, not twice"
    assert FENCE_REASON in ui.page.inner_text(".recovery-note"), "and keeps its live reason"
    assert ui.page.input_value("#input") == draft, "the refused request goes back to the composer"
    assert draft not in ui.log_text(), "nothing pretends a turn happened"
    assert ui.page.evaluate("() => !!document.activeElement.closest('.recovery-note')") is False, \
        "the note re-announces itself; it does not take the keyboard from the composer"
    assert _Fixture.queue_posts == [] and _Fixture.queue_starts == []
    assert _Fixture.reconcile_posts == []


def test_a_second_refusal_rewrites_the_status_line_instead_of_stacking_notes(ui, monkeypatch):
    _open_live_fenced_thread(ui, monkeypatch)
    for draft in ("Fenced fixture: first attempt", "Fenced fixture: second attempt"):
        ui.page.fill("#input", draft)
        ui.page.press("#input", "Enter")
        ui.page.wait_for_function(
            "draft => document.getElementById('input').value === draft", arg=draft, timeout=8000)
        assert ui.page.locator(".recovery-note").count() == 1
        assert ui.page.locator(".recovery-note .rc-status").count() == 1, "one line, rewritten"
        assert "No new run was started." in ui.page.inner_text(".recovery-note .rc-status")
    assert _Fixture.queue_posts == [] and _Fixture.queue_starts == []
    assert _Fixture.reconcile_posts == [], "a refusal is never a resolution"
    assert len(_Fixture.stream_requests) == 2 and all(
        r["q"].startswith("Fenced fixture") for r in _Fixture.stream_requests), \
        "each Send was the user's own, and neither was retried by the page"


# ------------------------------- a capped run whose fence was settled somewhere else
#
# The receipt records the fence the run ENDED with. The page already re-reads the live state for
# the note; the Continue button has to read the same answer, or a thread stays dead after the one
# thing blocking it is gone. Anything short of an authoritative "no fence" keeps the refusal.
def _capped_fenced_transcript():
    return {"messages": [{"role": "user",
                          "content": "Migrate every module to the new config loader"},
                         {"role": "assistant",
                          "content": "I converted the first two modules and listed the rest."}],
            "run_receipts": [{"run": "r3", "stop_reason": "turn_limit", "completed": False,
                              "turns_exhausted": True, "turns": 8, "max_turns": 8, "edited": True,
                              "verified": False, "canceled": False, "error": "",
                              "recovery_required": True,
                              "decision": {"intent": "build", "verification": "auto"}}]}


def _open_capped_fenced_thread(ui, monkeypatch):
    monkeypatch.setitem(TRANSCRIPTS, "s-cap", _capped_fenced_transcript())
    ui.page.locator(".thread").filter(has_text="Migrate every module").first.click()
    ui.page.wait_for_function(
        "() => document.getElementById('log').textContent.includes('Stopped at the turn limit')",
        timeout=8000)


def test_a_capped_receipt_offers_its_turn_again_once_the_live_fence_is_gone(ui, monkeypatch):
    _Fixture.recovery_states.clear()                 # /api/recovery/s-cap answers 404: it is gone
    _open_capped_fenced_thread(ui, monkeypatch)
    ui.page.wait_for_function(
        "() => { var b = document.querySelector('.stop-note button'); return !!b && !b.disabled; }",
        timeout=8000)
    assert ui.page.locator(".recovery-note").count() == 0, "no live fence, so no card to point at"
    assert not ui.page.get_attribute(".stop-note button", "title"), "and no tooltip about one"
    assert _Fixture.stream_requests == [], "re-reading the fence starts nothing by itself"
    ui.page.click(".stop-note button")               # one ordinary manual follow-up, by hand
    await_run(ui.page)
    assert len(_Fixture.stream_requests) == 1, "exactly the turn that was clicked for"
    assert _Fixture.reconcile_posts == [], "continuing is not resolving anything"


@pytest.mark.parametrize("staged,expect_note", [
    ("fence", True),                                 # the route says the fence is still open
    ("unreachable", True),                           # the route could not answer at all
    ("unparseable", False),                          # a 200 whose body is not JSON
    ("no_verdict", False),                           # a 200 that never says either way
])
def test_a_capped_receipt_keeps_refusing_a_turn_on_anything_but_a_clear_answer(
        ui, monkeypatch, staged, expect_note):
    if staged == "fence":
        _Fixture.recovery_states["s-cap"] = _fence("s-cap", "r3", FENCE_REASON)
    elif staged == "unreachable":
        ui.page.route("**/api/recovery/s-cap*", lambda route: route.fulfill(
            status=503, content_type="application/json",
            body=json.dumps({"error": "recovery state unavailable"})))
    elif staged == "unparseable":
        ui.page.route("**/api/recovery/s-cap*", lambda route: route.fulfill(
            status=200, content_type="application/json", body="{not json at all"))
    else:
        ui.page.route("**/api/recovery/s-cap*", lambda route: route.fulfill(
            status=200, content_type="application/json", body="{}"))
    _open_capped_fenced_thread(ui, monkeypatch)
    ui.page.wait_for_timeout(1000)                   # long enough for the probe to have answered
    assert ui.page.is_disabled(".stop-note button"), "an unread state is not a cleared one"
    assert (ui.page.locator(".recovery-note").count() > 0) is expect_note
    assert _Fixture.stream_requests == [], "and nothing is continued on the user's behalf"
    assert _Fixture.reconcile_posts == []


def test_a_late_cleared_fence_cannot_reach_into_a_newer_view_of_the_thread(ui, monkeypatch):
    """The first read answers "gone" - but it lands after a newer read of a newly fenced run."""
    _Fixture.recovery_states.clear()
    _Fixture.recovery_delay = 0.9                    # the answer arrives after the reader moves on
    monkeypatch.setitem(TRANSCRIPTS, "s-cap", _capped_fenced_transcript())
    ui.page.locator(".thread").filter(has_text="Migrate every module").first.click()
    ui.page.wait_for_timeout(150)
    ui.page.locator(".thread").filter(has_text="Read README.md").first.click()
    ui.page.wait_for_function(
        "() => document.getElementById('log').textContent.includes('per-category totals')",
        timeout=8000)
    _Fixture.recovery_states["s-cap"] = _fence("s-cap", "r3", FENCE_REASON)   # fenced again since
    ui.page.locator(".thread").filter(has_text="Migrate every module").first.click()
    ui.page.wait_for_selector(".recovery-note", timeout=8000)
    ui.page.wait_for_timeout(1200)                   # by now the stale 404 has certainly landed
    assert ui.page.locator(".recovery-note").count() == 1
    assert ui.page.is_disabled(".stop-note button"), "the stale clear belongs to the view that left"
    assert _Fixture.stream_requests == []
    assert _Fixture.reconcile_posts == []


# -------------------------------------- arriving in recovery is not deciding in recovery
#
# The row's first action is `completed` - an assertion that an external, possibly irreversible
# thing DID happen. Landing the keyboard there makes the reflex Enter the strongest claim on the
# page, for a person who was sent there to look. The row takes the focus instead; the actions keep
# their order, their wording and their confirmation, one Tab away.
def test_arriving_in_recovery_focuses_the_row_and_enter_decides_nothing(ui):
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
    ui.ask("Deploy the release to staging")
    dialogs = []
    ui.page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    ui.page.locator(".recovery-note button").click()
    ui.page.wait_for_selector(".activity-row.is-focus", timeout=8000)
    ui.page.wait_for_timeout(300)
    landed = ui.page.evaluate(
        "() => { var el = document.activeElement;"
        "        return [el.tagName, el.classList.contains('is-focus')]; }")
    assert landed[0] != "BUTTON", "arriving must not put the keyboard on a resolution"
    assert landed[1], "it lands on the row that was scrolled into view and highlighted"
    ui.page.keyboard.press("Enter")
    ui.page.wait_for_timeout(400)
    assert dialogs == [], "the reflex Enter raises no confirmation"
    assert _Fixture.reconcile_posts == [], "and asserts nothing about the outside world"
    # Every resolution is still on the row, in its order, and still one Tab from the keyboard.
    ui.page.keyboard.press("Tab")
    first = ui.page.evaluate(
        "() => [document.activeElement.tagName, document.activeElement.textContent]")
    assert first == ["BUTTON", "completed"], "the actions stay reachable, unchanged, and in order"
    assert ui.page.locator(".activity-row.is-focus button").count() == 3


# ------------------------------------ maintenance warnings are not a decision to make
#
# The recovery lane listed everything the snapshot carried in one "Needs You & recovery" column and
# painted the health badge red for any status other than `ok`. So an installation whose only finding
# was "the installed Collie differs from the running source" — advisory, nothing blocked — looked
# exactly like an interrupted deploy waiting on a person. Warning-only maintenance of the kinds the
# snapshot contract defines now folds into a plainly labelled optional section, complete and
# counted, while anything that could actually block the work stays where it was. Nothing here may
# dismiss a record, acknowledge one, or run a repair, test, retry or reconcile by itself.
DRIFT_CHECK = {"id": "doctor:version_drift", "kind": "doctor", "identity": "version_drift",
               "severity": "warning", "title": "Installed Collie differs from the running source",
               "detail": "Installed 0.9.1, this source tree is 0.9.2. Reinstall when convenient.",
               "command": "pip install -e .", "actions": ["inspect_doctor"]}
STALE_PUMP = {"id": "service:notification-pump", "kind": "service", "identity": "notification-pump",
              "severity": "warning", "title": "notification-pump is stale",
              "detail": "No fresh heartbeat for about 300 seconds", "actions": ["inspect_doctor"]}


def _open_recovery_tab(page):
    """Open the control panel on the recovery tab, the way a person reaches it from the toolbar."""
    page.click("#topbarMore > summary")
    page.click("#activityBtn")
    page.evaluate("() => { document.getElementById('topbarMore').open = false; }")  # click away
    page.wait_for_selector("#activityPanel:not([hidden])", timeout=8000)
    page.click('[data-control-tab="recovery"]')
    page.wait_for_function(
        "() => document.getElementById('controlSummary').textContent.includes('need a decision')",
        timeout=8000)
    page.wait_for_timeout(150)


def _fold_open(page):
    return page.evaluate("() => { var d = document.querySelector('.optional-lane details');"
                         "        return !!d && d.open; }")


def test_a_warning_only_snapshot_folds_its_optional_checks_and_does_not_look_like_a_failure(ui):
    _Fixture.recovery_extra_items = [DRIFT_CHECK, STALE_PUMP]
    _open_recovery_tab(ui.page)
    # The main lane is empty and says so; nothing there is asking anyone for anything.
    required = ui.page.locator(".activity-lane:not(.optional-lane)")
    assert required.count() == 1 and required.locator(".activity-row").count() == 0
    assert required.locator(".activity-empty").inner_text() == "None"
    # The warnings are listed, counted and named — folded shut on a page opened just now.
    summary = ui.page.locator(".optional-lane summary")
    assert summary.count() == 1
    label = summary.text_content()             # the lane heads are upper-cased by CSS, not copy
    assert "Optional checks (2)" in label and "nothing to decide" in label
    assert _fold_open(ui.page) is False, "a fresh page starts folded"
    assert ui.page.locator(".optional-lane .activity-row").count() == 2, "listed, not dropped"
    assert not ui.page.locator(".optional-lane .activity-row").first.is_visible()
    assert "2 optional" in ui.page.inner_text("#controlSummary")
    # Advisory is not red, and nothing in the fold borrows the failure token.
    badge = ui.page.locator("#activityHealth")
    assert badge.inner_text() == "warning"
    assert "bad" not in (badge.get_attribute("class") or ""), "a warning is not a red failure"
    assert ui.page.locator(".optional-lane .bad, #activityNotice.bad").count() == 0
    assert _Fixture.reconcile_posts == [] and _Fixture.repair_posts == [], \
        "showing maintenance neither resolves nor repairs anything"


def test_an_opened_fold_survives_a_read_only_refresh_and_keeps_every_detail(ui):
    _Fixture.recovery_extra_items = [DRIFT_CHECK, STALE_PUMP]
    _open_recovery_tab(ui.page)
    ui.page.click(".optional-lane summary")
    ui.page.wait_for_selector(".optional-lane .activity-row", state="visible", timeout=8000)
    drift = ui.page.locator(".optional-lane .activity-row").first
    body = drift.inner_text()
    assert DRIFT_CHECK["title"] in body and "warning" in body
    assert "this source tree is 0.9.2" in body, "the whole detail is kept, not summarised away"
    assert drift.locator(".activity-command").inner_text() == "pip install -e ."
    ui.page.click("#activityRefresh")                    # the same read-only GET the poll makes
    ui.page.wait_for_timeout(500)
    assert _fold_open(ui.page) is True, "a refresh must not re-hide what the reader opened"
    assert ui.page.locator(".optional-lane .activity-row").first.is_visible()
    assert _Fixture.reconcile_posts == [] and _Fixture.repair_posts == []


def test_a_brand_new_page_starts_folded_again_because_nothing_was_written_anywhere(server, browser):
    """The choice lives in the open page, so opening it stores no user state to clean up later."""
    _Fixture.recovery_extra_items = [DRIFT_CHECK]
    _Fixture.recovery_states = {}
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        _open_recovery_tab(page)
        page.click(".optional-lane summary")
        assert _fold_open(page) is True
        page.reload(wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        _open_recovery_tab(page)
        assert _fold_open(page) is False
        assert errors == [], "JS errors: %r" % errors
    finally:
        _Fixture.recovery_extra_items = []
        context.close()


def test_a_mixed_snapshot_keeps_the_real_decision_in_full_view_beside_the_fold(ui):
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
    _Fixture.recovery_extra_items = [DRIFT_CHECK]
    _open_recovery_tab(ui.page)
    decision = ui.page.locator(".activity-lane:not(.optional-lane) .activity-row")
    assert decision.count() == 1 and decision.is_visible()
    assert "Interrupted interactive action" in decision.inner_text()
    assert FENCE_REASON in decision.inner_text()
    assert [button.inner_text() for button in decision.locator("button").all()] == \
        ["completed", "Not fired", "cancel"], "every resolution, in its order and its wording"
    assert _fold_open(ui.page) is False, "the maintenance beside it is still only maintenance"
    assert ui.page.locator(".optional-lane .activity-row").count() == 1
    assert "bad" in (ui.page.get_attribute("#activityHealth", "class") or ""), \
        "a real decision still reads as one"
    assert _Fixture.reconcile_posts == [] and _Fixture.repair_posts == []


@pytest.mark.parametrize("item", [
    dict(DRIFT_CHECK, severity="error"),                        # severe
    dict(DRIFT_CHECK, severity="catastrophe"),                  # a severity nobody here knows
    dict(DRIFT_CHECK, severity="warning", kind="quarantine"),   # a kind nobody here knows
    dict(DRIFT_CHECK, severity="warning", kind="interactive"),  # work recovery, however labelled
    dict(DRIFT_CHECK, severity="warning", kind="notification",
         actions=["completed", "not_fired", "cancel"]),         # carries a reconciliation
])
def test_severe_unknown_and_reconcilable_items_are_never_folded_away(ui, item):
    _Fixture.recovery_extra_items = [item]
    _open_recovery_tab(ui.page)
    assert ui.page.locator(".optional-lane").count() == 0, "only known warning-only rows fold"
    row = ui.page.locator(".activity-lane .activity-row")
    assert row.count() == 1 and row.is_visible()
    assert item["title"] in row.inner_text()
    assert "bad" in (ui.page.get_attribute("#activityHealth", "class") or "")
    assert _Fixture.reconcile_posts == [] and _Fixture.repair_posts == []


def test_a_snapshot_status_this_page_does_not_recognise_stays_red(ui):
    """Folding rows is a reading aid; it never quietly downgrades a verdict from the server."""
    _Fixture.recovery_extra_items = [DRIFT_CHECK]
    _Fixture.recovery_status = "degraded"
    _open_recovery_tab(ui.page)
    badge = ui.page.locator("#activityHealth")
    assert badge.inner_text() == "degraded"
    assert "bad" in (badge.get_attribute("class") or "")
    assert ui.page.locator(".optional-lane").count() == 1, "the row is still listed, in the fold"


def test_being_sent_to_an_item_inside_the_fold_reveals_it_before_focusing_it(ui):
    """Focus on a row nobody can see is a dead end. Open the fold, then land on the row."""
    _Fixture.recovery_states["s-fence"] = _fence("s-fence", "r-fence", FENCE_REASON)
    ui.ask("Deploy the release to staging")
    # The snapshot now classifies this conversation's own item as warning-only maintenance.
    _Fixture.recovery_only_extra = True
    _Fixture.recovery_extra_items = [dict(DRIFT_CHECK, identity="s-fence",
                                          title="Interrupted interactive action")]
    dialogs = []
    ui.page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    ui.page.locator(".recovery-note button").click()
    ui.page.wait_for_selector(".activity-row.is-focus", timeout=8000)
    ui.page.wait_for_timeout(300)
    assert _fold_open(ui.page) is True, "the fold opened so the row could be seen"
    focused = ui.page.locator(".optional-lane .activity-row.is-focus")
    assert focused.count() == 1 and focused.is_visible()
    assert ui.page.evaluate(
        "() => document.activeElement.classList.contains('is-focus')") is True
    assert "not listed in recovery" not in ui.page.inner_text("#activityNotice")
    ui.page.keyboard.press("Enter")
    ui.page.wait_for_timeout(300)
    assert dialogs == [] and _Fixture.reconcile_posts == [] and _Fixture.repair_posts == []
    ui.page.click("#activityRefresh")                    # and the reveal is not undone by a poll
    ui.page.wait_for_timeout(500)
    assert _fold_open(ui.page) is True


def test_the_optional_section_speaks_the_reader_s_language(server, browser):
    _Fixture.lang = "zh"
    _Fixture.recovery_states = {}
    _Fixture.recovery_extra_items = [DRIFT_CHECK]
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    try:
        page.goto(server + "/?token=" + TOKEN, wait_until="load")
        page.wait_for_selector("#input", timeout=8000)
        _open_recovery_tab(page)
        summary = page.text_content(".optional-lane summary")
        assert "可选检查" in summary and "无需决定" in summary and "(1)" in summary
        page.click(".optional-lane summary")
        page.wait_for_selector(".optional-lane .activity-row", state="visible", timeout=8000)
        assert "主要任务可以继续" in page.inner_text(".optional-lane .optional-note")
        assert DRIFT_CHECK["detail"] in page.inner_text(".optional-lane"), \
            "the server's own words are quoted, not translated"
        assert errors == [], "JS errors: %r" % errors
    finally:
        _Fixture.lang = "en"
        _Fixture.recovery_extra_items = []
        context.close()
