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
import os
import re
import shutil
import tempfile
import threading
import time
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

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/api/verification":
            sid = (query.get("session") or [""])[0]
            requested = (query.get("cwd") or [""])[0]
            _FolderFixture.verification_requests.append({"session": sid, "cwd": requested})
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


@pytest.fixture(scope="module")
def folder_server():
    base = tempfile.mkdtemp(prefix="collie-draft-folder-")
    for label in ("default", "a", "b"):
        ROOTS[label] = os.path.join(base, label)
        os.makedirs(ROOTS[label])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _FolderFixture)
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


def _boot(server, browser, cold=False):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(server + "/?token=" + TOKEN, wait_until="load")
    page.wait_for_selector("#input", timeout=8000)
    if not cold:
        expect(page.locator("#taskWorkspacePath")).to_have_text(ROOTS["default"])
    return context, Surface(page, errors)


@pytest.fixture
def ui(folder_server, browser):
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
    context, surface = _boot(folder_server, browser)
    yield surface
    _Fixture.queue_release.set(); _Fixture.queue_ack.set()
    _FolderFixture.verification_delay = 0.0
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


def test_an_unchecked_selection_blocks_send_instead_of_running_somewhere_else(ui):
    """While the answer is in flight the folder is not yet known to exist. That is a reason to
    wait, never a reason to fall back to the default repository."""
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
    ui.send()
    page.wait_for_timeout(600)
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
