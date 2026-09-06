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


def _script(text):
    """Pick a staged run from the request text, the way a router would pick a route."""
    q = (text or "").lower()
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
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if path == "/api/route":
            _Fixture.route_requests.append(path)
            return self._json({"kind": "chat"})
        return self._json({})

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path in ("/", "/index.html"):
            return self._file("index.html", "text/html; charset=utf-8")
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


@pytest.fixture
def ui(server, browser):
    _Fixture.stream_requests = []
    _Fixture.route_requests = []
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    page.wait_for_timeout(400)          # identity + model probes settle; onboarding must stay shut
    assert not page.is_visible("#obOverlay.open"), "fixture should look configured"
    yield Page(page, errors)
    assert errors == [], "JS errors: %r" % errors
    context.close()


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
