"""The folder an unsent draft is aimed at belongs to the draft.

The composer already carried a draft's words, attachments and run setup across a thread
switch and a reload.  The Working folder did not: only a *confirmed* folder (one that a
`/api/verification` answer had come back for) was part of the saved setup.  A folder the
person had selected in the visible picker but that nothing had checked yet lived in the
DOM field alone — and Send read that field.  So:

    New task → select the fixture directory in the picker → type the task →
    open another thread and come back (or reload)

restored the words beside the *server default repository*, with the validity guard that
had been blocking Send cleared along with the selection.  The next Send then ran the task
in a project the person had never chosen for this draft.  Reproduced against the real
server on the mock provider, the run's session was written with `cwd` = the product repo.

These tests drive the real page over real HTTP.  The fixture server is the run-status
fixture plus a `/api/verification` with actual directories behind it: `cwd` resolves the
way `sessions.resolve_cwd` resolves it, and a folder that is gone answers 409
`workspace_missing` rather than quietly handing back somewhere else.
"""
import base64
import json
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
from http.server import ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import TRANSCRIPTS, TOKEN, _Fixture, browser   # noqa: F401

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/"
    "ScLbtAAAAABJRU5ErkJggg==")

ROOTS = {}          # label -> a real directory this fixture server will accept
DRAFT = "Tidy the fixture tree and leave the main project alone"


class _FolderFixture(_Fixture):
    """The run-status fixture with a folder question that can actually be answered."""

    session_cwd = {}                     # sid -> the workspace that thread was saved in
    verification_requests = []           # every folder question the page asked
    stream_queries = []                  # the full query of every /api/stream
    verification_delay = 0.0             # hold the check open, so "not checked yet" is observable
    answering = {}                       # thread -> the request that thread is in the middle of
    # The boot question is the one a new tab asks with no thread and no folder typed: "where does
    # a new task run?".  Refusing exactly that one, by count, reproduces a failure the page used
    # to answer by sending with no folder at all, without touching any other route.
    boot_questions = 0                   # how many boot questions the page has asked
    boot_failures = 0                    # how many of them to refuse before answering one
    boot_malformed = 0                   # how many to answer 200 with no folder named in it
    session_failures = 0                 # how many *thread* questions to refuse (not the boot one)
    boot_hold = None                     # released before the first answered boot question replies
    boot_seen = None                     # set once a held boot question has reached the server
    # The two halves of an answer the page cannot abort its way out of: a 200 whose headers arrive
    # and whose body never does, and a token refresh the check waits for *outside* its own request.
    body_hold = None                     # headers sent for a boot question, body held back
    token_hold = None                    # /api/session-token held open
    boot_stale = 0                       # how many boot questions answer 403, forcing a refresh
    # Long enough that a held question is a socket nobody ever answers rather than a slow one.
    # Every held handler is released by `_release_fixture`, so no thread outlives its test.
    boot_hold_wait = 30.0

    @staticmethod
    def _resolve(requested, saved):
        # sessions.resolve_cwd: an explicit directory wins, a thread keeps its saved one, and a
        # workspace that is not there is an error rather than a reason to run somewhere else.
        path = os.path.abspath(os.path.expanduser(requested or saved or ROOTS["default"]))
        if not os.path.isdir(path):
            raise ValueError("workspace does not exist or is not a directory: " + path)
        return path

    def _stream(self, query):
        _FolderFixture.stream_queries.append({key: value[0] for key, value in query.items()})
        return _Fixture._stream(self, query)

    def _headers_then_nothing(self, payload):
        """Answer the headers of a perfectly good 200 and hold its body back.

        `fetch` resolves here, so a deadline that ends when the response *starts* is already over
        while the page still has nothing to read.  The body is written once the hold is released
        so the handler can retire; by then the page has long since let this request go.
        """
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _FolderFixture.boot_seen.set()
        _FolderFixture.body_hold.wait(_FolderFixture.boot_hold_wait)
        try:
            self.wfile.write(body)
        except OSError:                  # the page gave up on its own request, as it should have
            pass

    def do_POST(self):
        _FolderFixture.answering[threading.get_ident()] = self.requestline
        return _Fixture.do_POST(self)

    def do_GET(self):
        _FolderFixture.answering[threading.get_ident()] = self.requestline
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/api/session-token" and _FolderFixture.token_hold is not None:
            # The refresh a 403 sends the page to. It is not the check's own request, so nothing
            # the check aborts reaches it; only a bound on the whole look ever ends this wait.
            _FolderFixture.boot_seen.set()
            _FolderFixture.token_hold.wait(_FolderFixture.boot_hold_wait)
        if path == "/api/verification":
            sid = (query.get("session") or [""])[0]
            requested = (query.get("cwd") or [""])[0]
            _FolderFixture.verification_requests.append({"session": sid, "cwd": requested})
            if sid and _FolderFixture.session_failures:
                _FolderFixture.session_failures -= 1
                return self._json({"error": "workspace check unavailable"}, 503)
            if not sid and not requested:
                _FolderFixture.boot_questions += 1
                if _FolderFixture.boot_failures:
                    _FolderFixture.boot_failures -= 1
                    return self._json({"error": "workspace check unavailable"}, 503)
                if _FolderFixture.boot_stale:
                    # A rotated process token: `authenticatedFetch` answers this itself, by
                    # refreshing and asking again — a wait of its own, outside this request.
                    _FolderFixture.boot_stale -= 1
                    return self._json({"error": "stale session token"}, 403)
                if _FolderFixture.body_hold is not None:
                    return self._headers_then_nothing(
                        {"session": "", "cwd": ROOTS["default"], "candidates": []})
                if _FolderFixture.boot_malformed:
                    # A 200 that names no folder: the shape the page used to read as a settled
                    # root, and then send with nothing to run in.
                    _FolderFixture.boot_malformed -= 1
                    return self._json({"session": "", "candidates": []})
                if _FolderFixture.boot_hold is not None:
                    _FolderFixture.boot_seen.set()
                    _FolderFixture.boot_hold.wait(_FolderFixture.boot_hold_wait)
            time.sleep(_FolderFixture.verification_delay)
            try:
                cwd = self._resolve(None if sid else requested,
                                    _FolderFixture.session_cwd.get(sid, "") if sid else "")
            except ValueError as exc:
                return self._json({"error": str(exc), "workspace_missing": True}, 409)
            return self._json({"session": sid, "cwd": cwd,
                               "candidates": [{"command": "pytest -q " + os.path.basename(cwd),
                                               "source": "detected"}]})
        if path.startswith("/api/session/"):
            sid = path.rsplit("/", 1)[-1]
            saved = dict(TRANSCRIPTS.get(sid, {"messages": []}))
            if sid in _FolderFixture.session_cwd:
                saved["cwd"] = _FolderFixture.session_cwd[sid]
                saved["workspace"] = {"mode": "local", "path": saved["cwd"]}
            return self._json(saved)
        return _Fixture.do_GET(self)


class _WatchedServer(ThreadingHTTPServer):
    """A handler that raises answers nothing at all — so say which request that was.

    `socketserver` turns a crashed handler into an anonymous dump on the process's stderr and
    closes the connection with no response.  The page's `fetch` then rejects, and for the boot
    check that means the Working folder summary simply stays blank: exactly the surface Windows CI
    photographed for this suite, with the rest of the page loaded and no JS error.  The dump alone
    cannot be tied to a test, so the request that died is kept here for the one that lost it.
    """

    crashes = []                         # (request line, traceback) per handler that raised

    def handle_error(self, request, client_address):
        _WatchedServer.crashes.append(
            (_FolderFixture.answering.get(threading.get_ident(), "?"), traceback.format_exc()))


@pytest.fixture(scope="module")
def folder_server():
    base = tempfile.mkdtemp(prefix="collie-draft-folder-")
    for label in ("default", "a", "b"):
        ROOTS[label] = os.path.join(base, label)
        os.makedirs(ROOTS[label])
    httpd = _WatchedServer(("127.0.0.1", 0), _FolderFixture)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    shutil.rmtree(base, ignore_errors=True)


class Surface:
    """One loaded task surface, driven the way the picker is driven by hand."""

    def __init__(self, page, errors):
        self.page = page
        self.errors = errors

    def select_folder(self, path, confirm=True):
        self.page.evaluate("document.getElementById('taskWorkspace').open = true")
        self.page.fill("#taskFolder", path)
        if confirm:
            self.page.click("#taskFolderUse")

    def folder(self):
        """What the composer says this task will run in, from the collapsed summary."""
        return self.page.inner_text("#taskWorkspacePath")

    def field(self):
        return self.page.input_value("#taskFolder")

    def unchecked(self):
        return self.page.is_visible("#taskWorkspacePending")

    def send(self, text=None):
        if text is not None:
            self.page.fill("#input", text)
        self.page.press("#input", "Enter")

    def reload(self):
        self.page.reload(wait_until="load")
        self.page.wait_for_selector("#input")
        self.page.wait_for_timeout(500)

    def open_thread(self, label, sid):
        self.page.locator(".thread").filter(has_text=label).first.click()
        expect(self.page).to_have_url(re.compile("session=" + sid))
        self.page.wait_for_timeout(400)

    def new_task(self):
        self.page.locator("#newChat").click()
        self.page.wait_for_timeout(400)


def _why_no_folder(page, errors):
    """What the page and the server have to say about a Working folder that never arrived.

    The summary is blank both when the boot check has not answered *yet* and when it answered
    with a failure the panel only writes into its collapsed status line, so the report has to
    carry that line, the questions the server was actually asked, and any handler that died
    without answering one of them.  Without those three a blank span is unattributable.
    """
    try:
        panel = page.evaluate("""() => ({
            status: document.getElementById('taskWorkspaceStatus').textContent,
            pendingHidden: document.getElementById('taskWorkspacePending').hidden,
            field: document.getElementById('taskFolder').value,
        })""")
    except Exception as unreadable:      # a report may never replace the failure it explains
        panel = "unreadable: %s" % unreadable
    return ("boot /api/verification: asked %r, page holds %r, JS errors %r\n"
            "server handler crashes: %s"
            % (_FolderFixture.verification_requests, panel, errors,
               "\n".join("%s\n%s" % row for row in _WatchedServer.crashes) or "none"))


# The page's folder check gives up after ten seconds, and it retries a refusal three times, so a
# test of that deadline would spend forty seconds waiting for nothing.  This shrinks that one delay
# in this tab, in the browser: the page keeps its own bound and has no test hook for it, and the
# only other ten-second `setTimeout` it schedules is a mission card's poll, which no folder test
# has on screen.  The deadline being tested is still the page's, only sooner.
_SOONER_DEADLINE = """
(() => {
  const native = window.setTimeout;
  window.setTimeout = function (fn, delay) {
    const rest = Array.prototype.slice.call(arguments, 2);
    return native.apply(window, [fn, delay === 10000 ? 500 : delay].concat(rest));
  };
})()
"""


def _boot(server, browser, cold=False, sooner=False):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    if sooner:
        page.add_init_script(_SOONER_DEADLINE)
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    if not cold:
        try:
            expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["default"])
        except AssertionError as failure:
            report = _why_no_folder(page, errors)
            context.close()
            raise AssertionError("%s\n%s" % (failure, report)) from None
    return context, Surface(page, errors)


def _reset_fixture():
    """Everything one test may have left behind on the module-scoped server."""
    _Fixture.stream_requests = []
    _Fixture.queue_entries = {}; _Fixture.queue_posts = []; _Fixture.queue_starts = []
    _Fixture.uploads = []
    _Fixture.queue_ack = threading.Event(); _Fixture.queue_ack.set()
    _Fixture.queue_seen = threading.Event(); _Fixture.queue_release = threading.Event()
    _Fixture.queue_deliver = threading.Event()
    _Fixture.queue_fail_once = False; _Fixture.queue_fail_all = False
    _Fixture.upload_delay = 0.0; _Fixture.upload_fail_once = False
    _Fixture.busy_delay = 0.0; _Fixture.start_delay = 0.0
    _Fixture.queue_active = False; _Fixture.queue_status_extra = {}
    _FolderFixture.verification_requests = []
    _FolderFixture.stream_queries = []
    _FolderFixture.verification_delay = 0.0
    _FolderFixture.session_cwd = {"s-read": ROOTS["a"], "s-cap": ROOTS["a"]}
    _FolderFixture.boot_questions = 0; _FolderFixture.boot_failures = 0
    _FolderFixture.boot_malformed = 0; _FolderFixture.session_failures = 0
    _FolderFixture.boot_hold = None; _FolderFixture.boot_seen = None
    _FolderFixture.body_hold = None; _FolderFixture.token_hold = None
    _FolderFixture.boot_stale = 0
    _WatchedServer.crashes = []          # a crashed handler belongs to the test that lost its answer


def _release_fixture():
    _Fixture.queue_release.set(); _Fixture.queue_ack.set()
    _FolderFixture.verification_delay = 0.0
    _FolderFixture.boot_failures = 0; _FolderFixture.boot_malformed = 0
    _FolderFixture.session_failures = 0; _FolderFixture.boot_stale = 0
    for held in (_FolderFixture.boot_hold, _FolderFixture.body_hold, _FolderFixture.token_hold):
        if held is not None:
            held.set()                   # never leave a handler thread parked on a dead test


@pytest.fixture
def ui(folder_server, browser):
    _reset_fixture()
    context, surface = _boot(folder_server, browser)
    yield surface
    _release_fixture()
    assert surface.errors == [], "JS errors: %r" % surface.errors
    context.close()


@pytest.fixture
def cold(folder_server, browser):
    """A tab that has not loaded yet, so the boot check can be made to fail before it is asked."""
    _reset_fixture()
    opened = []

    def boot(failures=0, hold=False, malformed=0, body=False, stale=0, sooner=False):
        _FolderFixture.boot_failures = failures
        _FolderFixture.boot_malformed = malformed
        _FolderFixture.boot_stale = stale
        if hold or body or stale:
            _FolderFixture.boot_seen = threading.Event()
        if hold:
            _FolderFixture.boot_hold = threading.Event()
        if body:
            _FolderFixture.body_hold = threading.Event()
        if stale:
            _FolderFixture.token_hold = threading.Event()
        context, surface = _boot(folder_server, browser, cold=True, sooner=sooner)
        opened.append((context, surface))
        return surface

    yield boot
    _release_fixture()
    for context, surface in opened:
        assert surface.errors == [], "JS errors: %r" % surface.errors
        context.close()


# ------------------------------------------------------------------ the defect
def test_a_reload_keeps_the_folder_the_unsent_draft_was_written_for(ui):
    ui.select_folder(ROOTS["b"])
    expect(ui.page.locator("#taskWorkspacePath")).to_have_text(ROOTS["b"])
    ui.page.fill("#input", DRAFT)
    ui.page.wait_for_timeout(150)

    ui.reload()

    expect(ui.page.locator("#input")).to_have_value(DRAFT)
    assert ui.folder() == ROOTS["b"], "the restored draft was put back over the server default"
    assert ui.field() == ROOTS["b"]
    ui.send()
    ui.page.wait_for_timeout(600)
    assert _FolderFixture.stream_queries, "nothing was sent"
    assert _FolderFixture.stream_queries[-1].get("cwd") == ROOTS["b"], \
        "the send targeted a project this draft was never aimed at"


def test_a_selection_the_server_has_not_answered_for_yet_is_still_the_drafts_folder(ui):
    """The exact reproduction: the picker was used, the check had not come back (or was never
    asked for), and the switch away used to drop the selection *and* the guard with it."""
    page = ui.page
    ui.select_folder(ROOTS["b"], confirm=False)     # typed in the picker, not checked
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)
    assert ui.folder() == ROOTS["b"], "the summary hid the folder the composer would send to"
    assert ui.unchecked(), "an unchecked selection has to say so"

    ui.open_thread("Read README.md", "s-read")
    ui.new_task()

    expect(page.locator("#input")).to_have_value(DRAFT)
    assert ui.field() == ROOTS["b"], "the selection did not survive the round trip"
    ui.reload()
    assert ui.field() == ROOTS["b"], "the selection did not survive the reload"
    ui.send()
    page.wait_for_timeout(600)
    assert _FolderFixture.stream_queries[-1].get("cwd") == ROOTS["b"]


def test_an_unchecked_selection_waits_for_the_check_instead_of_running_somewhere_else(ui):
    """While the answer is in flight the folder is not yet known to exist. That is a reason to
    wait, never a reason to fall back to the default repository.

    Send used to be *refused* here and to ask for a Use folder click; it now waits for that same
    read-only check and starts the request itself (see `test_web_folder_send`)."""
    page = ui.page
    _FolderFixture.verification_delay = 1.5
    ui.select_folder(ROOTS["b"])
    page.wait_for_timeout(200)
    assert ui.unchecked()
    ui.send(DRAFT)
    page.wait_for_timeout(300)
    assert not _FolderFixture.stream_queries, "sent under a folder nothing had checked"
    expect(page.locator("#input")).to_have_value(DRAFT), "the words are kept where they are"

    expect(page.locator("#taskWorkspacePending")).to_be_hidden(timeout=6000)
    assert ui.folder() == ROOTS["b"]
    expect(page.locator("#input")).to_have_value("", timeout=6000)
    page.wait_for_timeout(600)
    assert len(_FolderFixture.stream_queries) == 1
    assert _FolderFixture.stream_queries[-1].get("cwd") == ROOTS["b"]


def test_a_selected_folder_that_is_gone_is_refused_out_loud_not_replaced(ui):
    """A folder that vanished between the selection and the reload must be said out loud. The
    silent alternative is a task running in whatever the server would have defaulted to."""
    page = ui.page
    doomed = os.path.join(ROOTS["b"], "disposable")
    os.makedirs(doomed, exist_ok=True)
    ui.select_folder(doomed)
    expect(page.locator("#taskWorkspacePath")).to_have_text(doomed)
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)
    shutil.rmtree(doomed)

    ui.reload()

    expect(page.locator("#input")).to_have_value(DRAFT)
    assert ui.folder() == doomed, "the missing folder was swapped for the default"
    expect(page.locator("#taskWorkspaceStatus")).to_contain_text("does not exist")
    expect(page.locator("#taskWorkspaceNote")).to_contain_text("Folder missing")
    ui.send()
    page.wait_for_timeout(400)
    assert not _FolderFixture.stream_queries, "a task was started in a folder that is not there"
    expect(page.locator("#input")).to_have_value(DRAFT)

    # Restoring it is enough; the draft is still whole and still aimed where it was written for.
    os.makedirs(doomed)
    page.click("#taskFolderUse")
    expect(page.locator("#taskWorkspaceStatus")).to_have_text("", timeout=6000)
    ui.send()
    page.wait_for_timeout(600)
    assert _FolderFixture.stream_queries[-1].get("cwd") == doomed


# --------------------------------------------- the folder a new tab has not been told yet
def test_a_refused_boot_check_is_asked_again_and_the_send_runs_in_the_answer(cold):
    """A new tab has a folder before it has a selection: the one the server resolves for it.

    When that first question was refused nothing said so — the summary showed an empty space
    where a directory belongs, no selection existed to object to, and the next Send dispatched
    with no folder at all, running the task in whatever the server defaulted to.  The question
    is asked again on its own, and until it is answered the summary says it is unsettled.
    """
    ui = cold(failures=1, hold=True)
    page = ui.page
    assert _FolderFixture.boot_seen.wait(8), "the refused boot check was never asked again"
    pending = page.locator("#taskWorkspacePending")
    expect(pending).to_be_visible()
    assert re.search("checking|not confirmed", pending.inner_text(), re.I), \
        "an unsettled folder was presented as settled: %r" % pending.inner_text()
    assert ui.folder() == "", "a folder no answer named was shown as this task's"

    _FolderFixture.boot_hold.set()
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["default"], timeout=8000)
    expect(pending).to_be_hidden()
    assert _FolderFixture.boot_questions == 2, \
        "asked %d times, not once refused and once answered" % _FolderFixture.boot_questions

    ui.send(DRAFT)
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(600)
    assert len(_FolderFixture.stream_queries) == 1, \
        "one Send started %d requests" % len(_FolderFixture.stream_queries)
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["default"]
    assert _FolderFixture.stream_queries[0].get("q") == DRAFT


def test_a_boot_check_that_keeps_failing_keeps_the_draft_instead_of_sending_at_nothing(cold):
    """Nothing starts while the folder is unknown — and the person is told, in their own words,
    rather than watching a task run somewhere nobody chose.  Repeated Sends are still one Send."""
    ui = cold(failures=99)
    page = ui.page
    page.fill("#input", DRAFT)
    page.locator("#fileInput").set_input_files(
        {"name": "shot.png", "mimeType": "image/png", "buffer": PNG})
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)

    for _ in range(3):
        ui.send()
        page.wait_for_timeout(200)
    page.wait_for_timeout(900)

    assert _FolderFixture.stream_queries == [], "a task was started before the folder was known, in %r" % [
        row.get("cwd") or "<no folder asked for: wherever the server defaults to>"
        for row in _FolderFixture.stream_queries]
    assert _Fixture.queue_posts == [], "work was admitted to a queue with nowhere to run it"
    assert _Fixture.uploads == [], "an attachment was uploaded for a request that cannot start"
    expect(page.locator("#input")).to_have_value(DRAFT)
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    assert ui.folder() == "", "a folder nothing confirmed was presented as this task's"
    expect(page.locator("#taskWorkspacePending")).to_be_visible()
    expect(page.locator("#taskWorkspaceNote")).to_contain_text("Could not check")

    # Bounded: a check that will not answer is reported once, not asked forever.
    asked = _FolderFixture.boot_questions
    page.wait_for_timeout(2000)
    assert _FolderFixture.boot_questions == asked, \
        "the page is still polling: %d more questions" % (_FolderFixture.boot_questions - asked)


def test_a_folder_chosen_while_the_boot_check_is_open_is_where_the_send_goes(cold):
    """The late answer is about a folder the person has already moved off. It may not aim
    anything: not the summary, and not the request the next Send starts."""
    ui = cold(failures=1, hold=True)
    page = ui.page
    assert _FolderFixture.boot_seen.wait(8), "the refused boot check was never asked again"
    page.fill("#input", DRAFT)
    ui.select_folder(ROOTS["b"])
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["b"], timeout=8000)

    _FolderFixture.boot_hold.set()                  # the answer about "wherever" lands now
    page.wait_for_timeout(700)
    assert ui.folder() == ROOTS["b"], "the stale answer moved the draft to the default folder"
    assert ui.field() == ROOTS["b"]

    ui.send()
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(600)
    assert len(_FolderFixture.stream_queries) == 1
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["b"], \
        "the send took the folder from an answer the person had moved off"


def test_a_thread_opened_while_the_boot_check_is_open_still_runs_in_its_own_folder(cold):
    """A conversation carries its own confirmed workspace. An unsettled new-task folder is not
    its problem: it opens, it continues, and the late answer cannot speak for it."""
    ui = cold(failures=1, hold=True)
    page = ui.page
    assert _FolderFixture.boot_seen.wait(8), "the refused boot check was never asked again"
    ui.open_thread("Read README.md", "s-read")
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["a"], timeout=8000)

    _FolderFixture.boot_hold.set()
    page.wait_for_timeout(700)
    assert ui.folder() == ROOTS["a"], "the new task's late answer was applied to a conversation"
    assert not ui.unchecked(), "a saved workspace was presented as unsettled"

    ui.send("Continue from where you stopped")
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(600)
    assert len(_FolderFixture.stream_queries) == 1
    assert _FolderFixture.stream_queries[0].get("session") == "s-read"
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["a"]


def _until(page, predicate, timeout=8000, step=100):
    """Wait on the condition itself rather than on a guess at how long it takes."""
    waited = 0
    while waited < timeout:
        if predicate():
            return True
        page.wait_for_timeout(step); waited += step
    return predicate()


def test_repeated_sends_against_a_held_boot_check_ask_once_and_start_one_run(cold):
    """The rootless twin of the typed-folder case: the boot question is open, not refused.

    Every Send is the same submission waiting on that same look, so it is asked once, the answer
    releases all of them together, and exactly one request is started.
    """
    ui = cold(hold=True)
    page = ui.page
    assert _FolderFixture.boot_seen.wait(8), "the boot question never reached the server"
    page.fill("#input", DRAFT)
    for _ in range(3):
        page.press("#input", "Enter")
        page.click("#send")
        page.wait_for_timeout(120)

    assert _FolderFixture.stream_queries == [], "started before the folder was known"
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert _FolderFixture.boot_questions == 1, \
        "six Sends asked the open question %d times" % _FolderFixture.boot_questions
    pending = page.locator("#taskWorkspacePending")
    expect(pending).to_be_visible()
    assert "checking" in pending.inner_text().lower(), \
        "a look that is merely open was reported as a failed one: %r" % pending.inner_text()

    _FolderFixture.boot_hold.set()
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(700)
    assert len(_FolderFixture.stream_queries) == 1, \
        "six Sends became %d requests" % len(_FolderFixture.stream_queries)
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["default"]
    assert _FolderFixture.stream_queries[0].get("q") == DRAFT
    assert _FolderFixture.boot_questions == 1, "the shared look was asked again after it answered"


def test_the_send_after_the_retry_budget_runs_out_asks_once_more_and_starts_one_run(cold):
    """The automatic asking stops; the page does not. A Send after the budget is spent asks one
    more time on its own, and a server that has recovered gets exactly one request."""
    ui = cold(failures=4)                     # the first look and all three of its retries
    page = ui.page
    expect(page.locator("#taskWorkspaceNote")).to_contain_text("Could not check", timeout=8000)
    assert _until(page, lambda: _FolderFixture.boot_questions >= 4), \
        "the refusal was not retried: %d questions" % _FolderFixture.boot_questions
    spent = _FolderFixture.boot_questions
    assert spent == 4, "asked %d times, not once plus a bounded three" % spent
    page.wait_for_timeout(1500)
    assert _FolderFixture.boot_questions == spent, "the page is still polling"

    ui.send(DRAFT)
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(700)
    assert _FolderFixture.boot_questions == spent + 1, \
        "the Send asked %d times" % (_FolderFixture.boot_questions - spent)
    assert len(_FolderFixture.stream_queries) == 1, \
        "the recovered folder started %d requests" % len(_FolderFixture.stream_queries)
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["default"]
    assert _FolderFixture.stream_queries[0].get("q") == DRAFT
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["default"])
    expect(page.locator("#taskWorkspacePending")).to_be_hidden()
    expect(page.locator("#taskWorkspaceStatus")).to_have_text("")


def test_a_boot_answer_that_names_no_folder_is_not_a_settled_folder(cold):
    """A 200 with nothing in it says nothing about where a new task runs.  Counting it as an
    answer is exactly how a Send left with no folder at all — so it is a refusal here."""
    ui = cold(malformed=99)
    page = ui.page
    expect(page.locator("#taskWorkspacePending")).to_be_visible(timeout=8000)
    expect(page.locator("#taskWorkspaceNote")).to_contain_text("Could not check", timeout=8000)
    expect(page.locator("#taskWorkspaceStatus")).to_contain_text("did not answer with a folder")
    page.fill("#input", DRAFT)
    ui.send()
    page.wait_for_timeout(900)
    assert _FolderFixture.stream_queries == [], "a task was started on an answer naming no folder"
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert ui.folder() == "", "a folder no answer named was shown as this task's"

    _FolderFixture.boot_malformed = 0                 # the server starts answering properly
    ui.send()
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(700)
    assert len(_FolderFixture.stream_queries) == 1
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["default"]
    expect(page.locator("#taskWorkspacePending")).to_be_hidden()


def test_a_boot_check_that_never_answers_keeps_the_draft_and_says_so(cold):
    """A socket accepted and never answered is the one refusal that never arrives.

    Without a deadline the look stayed open for good: the summary said "checking" forever, every
    Send joined a wait that could not end, and nothing was ever reported.  The look gives up on
    its own, and the draft, its attachment and the panel are what is left to act on.
    """
    ui = cold(hold=True)
    page = ui.page
    assert _FolderFixture.boot_seen.wait(8), "the boot question never reached the server"
    page.fill("#input", DRAFT)
    page.locator("#fileInput").set_input_files(
        {"name": "shot.png", "mimeType": "image/png", "buffer": PNG})
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    ui.send()
    page.wait_for_timeout(500)
    assert _FolderFixture.stream_queries == [], "started while the folder question hung"

    # The deadline belongs to the page (~10s), so this is its own answer and not a test timeout.
    expect(page.locator("#taskWorkspaceStatus")).to_contain_text("did not answer in time",
                                                                timeout=20000)
    expect(page.locator("#input")).to_have_value(DRAFT)
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    assert _FolderFixture.stream_queries == [], "a task was started with nowhere to run it"
    assert _Fixture.uploads == [], "an attachment was uploaded for a request that cannot start"
    assert _Fixture.queue_posts == [], "work was admitted to a queue with nowhere to run it"
    assert page.get_attribute("#taskWorkspace", "open") is not None, "the reason was hidden"
    assert page.locator("dialog[open]").count() == 0, "a modal was put in front of the composer"


def test_an_answer_that_starts_and_never_ends_is_given_up_on_as_well(cold):
    """The headers of a good 200 arrive and its body never does.

    `fetch` has resolved by then, so a deadline that ends when the response *starts* is already
    spent while the page still has nothing to read: it waits on `r.json()` for good, the summary
    says "checking" forever and every Send joins a wait that cannot end.  The bound is the whole
    look, so this is reported like any other check nobody answered, and the draft with its
    attachment is what is left to act on.  The answer that finally lands aims nothing.
    """
    ui = cold(body=True, sooner=True)
    page = ui.page
    assert _FolderFixture.boot_seen.wait(8), "the boot question never reached the server"
    pending = page.locator("#taskWorkspacePending")
    # Every look ends on its own: the first and the three it retries, each one a reply that began
    # and stopped.  "not confirmed" rather than "checking" is how the page says none is open.
    assert _until(page, lambda: _FolderFixture.boot_questions == 4), \
        "the bounded looks were %d" % _FolderFixture.boot_questions
    expect(pending).to_contain_text("not confirmed", timeout=8000)
    expect(page.locator("#taskWorkspaceStatus")).to_contain_text("did not answer in time")

    page.fill("#input", DRAFT)
    page.locator("#fileInput").set_input_files(
        {"name": "shot.png", "mimeType": "image/png", "buffer": PNG})
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    ui.send()
    page.wait_for_timeout(1500)                  # longer than the look the Send waits on
    assert _FolderFixture.boot_questions == 5, \
        "the Send asked %d times" % (_FolderFixture.boot_questions - 4)
    expect(pending).to_contain_text("not confirmed")
    expect(page.locator("#taskWorkspaceStatus")).to_contain_text("did not answer in time")
    expect(page.locator("#input")).to_have_value(DRAFT)
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    assert ui.folder() == "", "a folder no answer named was shown as this task's"
    assert _FolderFixture.stream_queries == [], "a task was started on a reply that never ended"
    assert _Fixture.uploads == [], "an attachment was uploaded for a request that cannot start"
    assert _Fixture.queue_posts == [], "work was admitted to a queue with nowhere to run it"

    _FolderFixture.body_hold.set()               # every held body lands now, all at once
    page.wait_for_timeout(700)
    assert _FolderFixture.boot_questions == 5, "the page went back to polling"
    assert _FolderFixture.stream_queries == [], "a late body started the request its look lost"
    assert ui.folder() == "", "a late body aimed the draft after its look was given up on"

    ui.send()                                    # the server answers in full now
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(700)
    assert len(_FolderFixture.stream_queries) == 1, \
        "the settled folder started %d requests" % len(_FolderFixture.stream_queries)
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["default"]
    assert _FolderFixture.stream_queries[0].get("q") == DRAFT


def test_a_held_token_refresh_does_not_hold_the_folder_check_open(cold):
    """The check's own request is answered — 403 — and the waiting moves out of its reach.

    `authenticatedFetch` answers a stale process token by refreshing it and asking again, and that
    refresh is not this look's request: giving up on its own fetch does not end it.  So a deadline
    made of an abort alone waits forever on a token rather than on a folder.  The bound is the
    whole look, the draft stays put, and the token, the credentials and every other request are
    left as they were — the next look, once the refresh answers, settles the folder.
    """
    ui = cold(stale=99, sooner=True)
    page = ui.page
    assert _FolderFixture.boot_seen.wait(8), "the refresh the 403 sent the page to never arrived"
    page.fill("#input", DRAFT)
    ui.send()

    expect(page.locator("#taskWorkspaceStatus")).to_contain_text("did not answer in time",
                                                                timeout=8000)
    expect(page.locator("#taskWorkspacePending")).to_be_visible()
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert _FolderFixture.stream_queries == [], "a task was started while a token was awaited"
    assert ui.folder() == "", "a folder no answer named was shown as this task's"

    _FolderFixture.token_hold.set(); _FolderFixture.boot_stale = 0
    ui.send()
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(700)
    assert len(_FolderFixture.stream_queries) == 1, \
        "the settled folder started %d requests" % len(_FolderFixture.stream_queries)
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["default"]
    assert _FolderFixture.stream_queries[0].get("q") == DRAFT


def test_a_refused_check_inside_a_conversation_is_not_asked_again(ui):
    """The automatic asking belongs to a new task's unknown root, and a conversation has none:
    it was opened in its own saved workspace, and nothing here reads that state for it. A thread
    whose check is refused must therefore cost one question, not a budget of them."""
    page = ui.page
    _FolderFixture.session_failures = 9
    ui.open_thread("Read README.md", "s-read")
    page.wait_for_timeout(1800)             # longer than the whole 250/500/750 ms budget
    asked = [row for row in _FolderFixture.verification_requests if row["session"] == "s-read"]
    assert len(asked) == 1, "a conversation's refused check was retried %d times" % (len(asked) - 1)

    _FolderFixture.session_failures = 0
    ui.send("Continue from where you stopped")
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(600)
    assert len(_FolderFixture.stream_queries) == 1
    assert _FolderFixture.stream_queries[0].get("session") == "s-read"
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["a"]


def test_a_look_that_is_merely_open_is_not_reported_as_a_failed_one(ui):
    """Reload a tab whose folder is already confirmed.  Nothing has failed, so the summary says
    the folder is being checked — for the whole round trip, not after it."""
    page = ui.page
    ui.select_folder(ROOTS["b"])
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["b"])
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)

    _FolderFixture.verification_delay = 1.5
    ui.reload()

    pending = page.locator("#taskWorkspacePending")
    expect(pending).to_be_visible()
    assert "checking" in pending.inner_text().lower(), \
        "the remembered folder was reported as unconfirmed while its look was open: %r" \
        % pending.inner_text()
    expect(page.locator("#taskWorkspaceNote")).not_to_contain_text("Could not check")
    expect(page.locator("#taskWorkspaceStatus")).to_have_text("")
    _FolderFixture.verification_delay = 0.0
    expect(pending).to_be_hidden(timeout=8000)
    assert ui.folder() == ROOTS["b"]
    ui.send()
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(700)
    assert len(_FolderFixture.stream_queries) == 1
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["b"]


def test_a_retained_submission_put_back_in_the_composer_is_checked_again(ui):
    """Putting a held submission back makes it a new task's draft again, folder included.

    That folder is a claim nothing has checked since, so the page asks — where it used to mark
    the restored draft "not confirmed" for good, with a Send gated on a check nobody scheduled.
    """
    page = ui.page
    ui.select_folder(ROOTS["b"])
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["b"])
    page.locator("#fileInput").set_input_files(
        {"name": "shot.png", "mimeType": "image/png", "buffer": PNG})
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    _Fixture.upload_delay = 1.5; _Fixture.upload_fail_once = True
    ui.send(DRAFT)
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.fill("#input", "A different task, written while that one was leaving")
    row = page.locator(".task-queue-row.retained")
    expect(row).to_have_count(1, timeout=8000)          # nowhere else holds those words now
    assert _FolderFixture.stream_queries == [], "a failed upload still started a run"

    page.fill("#input", "")                             # the composer is free again
    _Fixture.upload_delay = 0.0
    _FolderFixture.verification_delay = 1.0
    asked = len(_FolderFixture.verification_requests)
    row.get_by_role("button", name="Put in composer").click()

    expect(page.locator("#input")).to_have_value(DRAFT, timeout=8000)
    assert ui.field() == ROOTS["b"], "the restored submission lost the folder it was made for"
    pending = page.locator("#taskWorkspacePending")
    expect(pending).to_be_visible()
    assert "checking" in pending.inner_text().lower(), \
        "the restored folder was reported as unconfirmed: %r" % pending.inner_text()
    expect(page.locator("#taskWorkspaceNote")).not_to_contain_text("Could not check")
    _FolderFixture.verification_delay = 0.0
    expect(pending).to_be_hidden(timeout=8000)
    assert len(_FolderFixture.verification_requests) > asked, "the restore never asked again"

    ui.send()
    expect(page.locator("#input")).to_have_value("", timeout=8000)
    page.wait_for_timeout(700)
    assert len(_FolderFixture.stream_queries) == 1, \
        "the restored submission started %d requests" % len(_FolderFixture.stream_queries)
    assert _FolderFixture.stream_queries[0].get("cwd") == ROOTS["b"]
    assert _FolderFixture.stream_queries[0].get("q") == DRAFT


# ------------------------------------------- what a draft's folder may never touch
def test_a_new_task_draft_never_re_aims_the_conversation_it_is_parked_beside(ui):
    """Thread A was accepted in its own workspace. Reading it while a draft for folder B waits
    must show A, follow up in A, and leave A's saved folder alone."""
    page = ui.page
    ui.select_folder(ROOTS["b"], confirm=False)
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)

    ui.open_thread("Read README.md", "s-read")
    assert ui.folder() == ROOTS["a"], "the draft's folder followed the person into the thread"
    assert not ui.unchecked()
    assert page.get_attribute("#taskFolder", "readonly") is not None, "a thread's folder is its own"
    assert page.is_hidden("#taskFolderUse")
    expect(page.locator("#input")).to_have_value("")

    ui.send("Continue from where you stopped")
    page.wait_for_timeout(700)
    assert _FolderFixture.stream_queries[-1].get("cwd") == ROOTS["a"]
    assert _FolderFixture.stream_queries[-1].get("session") == "s-read"
    assert all(row["cwd"] == "" for row in _FolderFixture.verification_requests if row["session"]), \
        "a draft's folder was offered as a thread's"

    ui.new_task()
    expect(page.locator("#input")).to_have_value(DRAFT)
    assert ui.field() == ROOTS["b"]


def test_a_started_task_keeps_its_folder_and_the_next_new_task_starts_clean(ui):
    """The folder was chosen for one task. Once that task exists it belongs to it — and the next
    new task begins where a new task begins, not in the last one's tree."""
    page = ui.page
    ui.select_folder(ROOTS["b"])
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["b"])
    ui.send(DRAFT)
    page.wait_for_timeout(900)
    assert _FolderFixture.stream_queries[-1].get("cwd") == ROOTS["b"]

    ui.new_task()
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["default"], timeout=6000)
    expect(page.locator("#input")).to_have_value("")
    assert not ui.unchecked()


def test_a_cleared_draft_keeps_the_folder_and_a_fresh_tab_does_not(ui, folder_server, browser):
    """Clearing the words is not un-choosing the folder — the choice stays with this tab's new
    task, including across a reload. A tab that never made the choice starts at the default."""
    page = ui.page
    ui.select_folder(ROOTS["b"])
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["b"])
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)
    page.fill("#input", "")                 # deliberately emptied
    page.wait_for_timeout(150)

    ui.reload()
    expect(page.locator("#input")).to_have_value("")
    assert ui.folder() == ROOTS["b"]
    ui.send("Something else for the same folder")
    page.wait_for_timeout(600)
    assert _FolderFixture.stream_queries[-1].get("cwd") == ROOTS["b"]

    cold, other = _boot(folder_server, browser, cold=True)
    try:
        expect(other.page.locator("#taskWorkspacePath")).to_have_text(ROOTS["default"], timeout=6000)
        assert other.errors == [], other.errors
    finally:
        cold.close()


def test_the_folder_rides_with_the_draft_that_still_has_its_attachment(ui):
    """Folder, words and file are one draft. Moving away and back keeps all three, and the
    reattach safeguard is untouched by the folder travelling with them."""
    page = ui.page
    ui.select_folder(ROOTS["b"])
    expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["b"])
    page.locator("#fileInput").set_input_files(
        {"name": "shot.png", "mimeType": "image/png", "buffer": PNG})
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    page.fill("#input", DRAFT)
    page.wait_for_timeout(150)

    ui.open_thread("Migrate every module", "s-cap")
    expect(page.locator("#attachStrip .thumb")).to_have_count(0)
    ui.new_task()
    expect(page.locator("#input")).to_have_value(DRAFT)
    expect(page.locator("#attachStrip .thumb")).to_have_count(1)
    assert ui.folder() == ROOTS["b"]

    ui.reload()
    # Storage holds text and settings only, so the file is gone and says so — while the folder
    # the draft was written for is still the folder it will be sent to.
    expect(page.locator("#draftNotice")).to_contain_text("Reattach files")
    assert ui.folder() == ROOTS["b"]
