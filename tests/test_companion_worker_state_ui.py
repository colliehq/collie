"""What the companion surfaces say about a background worker, checked against rendered pages.

`aggregate_health` serialises one row per *desired* worker: `state` is the last thing the
supervisor wrote for it ("starting", "running", "backoff", "unhealthy", "circuit_open", "failed",
"dead", "disabled", "external"), defaulting to "missing" when there is no heartbeat row at all,
and `fresh` only says whether that row is still inside its TTL.

The ambient dock and the remote Pack page both rendered that as `fresh ? Running : Recovery
required`, which borrowed the *session* recovery vocabulary and said two untrue things with it: a
worker that never reported became an "uncertain external action" for a person to inspect and
reconcile (nothing was sent, and a heartbeat is not reconcilable), and a worker that had just
reported its own failure was shown as Running.  The Pack members lane had the same contradiction
from the other side: it badged a fresh "failed" beat as a healthy "Fresh".

These tests drive the real ambient.html and remote.html over real HTTP and read the rendered DOM.
Only the health and activity snapshots are staged; no live model, relay or browser backend is
involved.
"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

playwright_api = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_api.sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
WEBUI = os.path.join(os.path.dirname(HERE), "harness", "webui")
TOKEN = "fixture-token"

# One genuine session recovery decision, which is the thing that *is* an uncertain external action:
# it must stay visible and confirm-gated no matter what the worker lane says.
ACTIVITY = {
    "sessions": [{"session_id": "s-deploy", "state": "external_action", "recovery_required": True,
                  "reason": "run_shell ./deploy.sh may already have run"}],
    "task_runs": [], "missions": [], "automations": [], "errors": {},
}
HEALTH = {"ok": True, "status": "ready", "work": {"missions_active": 0, "task_runs_active": 0,
                                                  "automations_active": 0},
          "workers": {}, "services": {}, "supervisor": {"installed": False}}


def beat(state, fresh, age_s=3.0, pid=4242):
    """Exactly the shape harness.ops.aggregate_health serialises for one desired worker."""
    return {"state": state, "fresh": fresh, "age_s": age_s, "pid": pid, "detail": {}}


NO_HEARTBEAT = beat("missing", False, age_s=None, pid=0)


class _Fixture(BaseHTTPRequestHandler):
    health = {}
    activity = {}
    lang = "en"
    posts = []              # nothing a health read does may ever land here
    unauthenticated = []    # (path) for every guarded GET that arrived without the page token

    def log_message(self, *_a):
        pass

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
        # The product server injects the per-process token into every first-party HTML document.
        meta = ('<meta name="collie-token" content="%s">\n' % TOKEN).encode()
        body = body.replace(b'<meta charset="utf-8">', b'<meta charset="utf-8">\n' + meta, 1)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        _Fixture.posts.append((urlparse(self.path).path, json.loads(raw or b"{}")))
        return self._json({"ok": True})

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path in ("/api/activity", "/api/healthz") and \
                (query.get("token") or [""])[0] != TOKEN:
            _Fixture.unauthenticated.append(path)
            return self._json({"error": "forbidden"}, 403)
        if path == "/ambient":
            return self._file("ambient.html", "text/html; charset=utf-8")
        if path == "/remote":
            return self._file("remote.html", "text/html; charset=utf-8")
        if path == "/logo.svg":
            return self._file("logo.svg", "image/svg+xml")
        if path == "/api/activity":
            return self._json(_Fixture.activity)
        if path == "/api/healthz":
            return self._json(_Fixture.health)
        if path == "/api/settings":
            return self._json({"values": {"LANG": _Fixture.lang}})
        if path == "/api/whoami":
            return self._json({"name": "Collie", "machine": "fixture", "os": "fixture-os",
                               "repo": "product", "version": "test", "avatar": "/logo.svg"})
        if path == "/api/runs":
            return self._json({"runs": []})
        if path == "/api/approvals":
            return self._json({"approvals": []})
        if path == "/api/online":
            return self._json({"mode": "off", "nodes": [], "trusted_devices": []})
        if path == "/api/run-capabilities":
            return self._json({"provider": "mock", "model": "mock"})
        if path == "/api/remote/status":
            return self._json({"available": True, "enabled": False, "connected": False,
                               "devices": [], "link": "", "paircode": ""})
        if path == "/api/remote/pending":
            return self._json({"pending": None})
        return self._json({}, 404)


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        br = p.chromium.launch()
        yield br
        br.close()


@pytest.fixture
def page(server, browser):
    _Fixture.posts, _Fixture.unauthenticated, _Fixture.lang = [], [], "en"
    _Fixture.activity, _Fixture.health = dict(ACTIVITY), dict(HEALTH)
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    page.errors = []
    page.on("pageerror", lambda error: page.errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.dismiss())   # a confirm nobody accepted
    yield page
    context.close()


def _rows(page, selector, state_class):
    return page.eval_on_selector_all(
        selector + " .%s" % {"ops": "ops-row", "activity": "activityrow",
                             "pack": "packrow"}[state_class],
        "(rows, cls) => rows.map(row => ({"
        "  title: row.querySelector(cls.name).textContent,"
        "  meta: (row.querySelector(cls.meta) || {}).textContent || '',"
        "  state: row.querySelector(cls.state).textContent,"
        "  tone: row.querySelector(cls.state).className}))",
        {"ops": {"name": ".ops-row-name", "meta": ".ops-row-meta", "state": ".ops-row-state"},
         "activity": {"name": ".activityname", "meta": ".activitymeta",
                      "state": ".activitystate"},
         "pack": {"name": ".packrowname", "meta": ".packrowmeta",
                  "state": ".packrowstate"}}[state_class])


def ambient_workers(page, server, workers, services=None):
    """Open the ambient Activity panel against a health snapshot carrying these worker rows."""
    _Fixture.health = dict(HEALTH, workers=workers, services=services or {})
    page.goto(server + "/ambient", wait_until="load")
    page.wait_for_selector("#opsState", timeout=8000)
    page.click("#opsState")
    page.wait_for_selector("#ambientOther .ops-row", timeout=8000)
    return _rows(page, "#ambientOther", "ops")


def remote_workers(page, server, workers, services=None):
    """Load the remote Pack page against the same snapshot; it renders the workers twice."""
    _Fixture.health = dict(HEALTH, workers=workers, services=services or {})
    page.goto(server + "/remote", wait_until="load")
    page.wait_for_selector("#remoteOtherActivity .activityrow", timeout=8000)
    return _rows(page, "#remoteOtherActivity", "activity")


def pack_members(page):
    return _rows(page, "#packmembers", "pack")


# --------------------------------------------------------------- the ambient dock's Services lane
def test_ambient_does_not_call_a_silent_worker_a_recovery_decision(page, server):
    """A stock server with no job workers: cautionary, but nothing for a person to reconcile."""
    rows = ambient_workers(page, server, {"jobd": NO_HEARTBEAT, "web": NO_HEARTBEAT})
    assert [row["state"] for row in rows] == ["No heartbeat", "No heartbeat"]
    assert "Recovery required" not in page.inner_text("#ambientOther"), \
        "nothing was sent, so there is no uncertain external action to inspect"
    assert all("warn" in row["tone"] for row in rows), "cautionary, not a green Running"
    assert all("no heartbeat recorded" in row["meta"] for row in rows)
    assert "Background service status updates automatically." in page.inner_text("#ambientOther")
    # The genuine session decision is still there, still asking a person to inspect first.
    recovery = page.inner_text("#ambientRecovery")
    assert "s-deploy" in recovery and "deploy.sh" in recovery
    assert page.locator("#ambientRecovery .ops-row-actions button").count() == 3
    assert _Fixture.posts == [], "reading a health page decides nothing"


def test_ambient_does_not_report_a_fresh_failure_as_running(page, server):
    """The worker reported its own failure seconds ago. Freshness is not health."""
    rows = ambient_workers(page, server, {"ambient": beat("failed", True),
                                          "bridge": beat("dead", True),
                                          "jobd": beat("circuit_open", True)})
    assert [row["state"] for row in rows] == ["Failed", "Failed", "Restarts paused"]
    assert all("bad" in row["tone"] for row in rows), "an actionable failure, stated as one"
    assert "Running" not in page.inner_text("#ambientOther")
    assert _Fixture.posts == []


def test_ambient_keeps_a_failure_that_then_went_quiet(page, server):
    """Going silent after reporting a failure does not withdraw the failure."""
    rows = ambient_workers(page, server, {"jobd": beat("failed", False, age_s=240.0)})
    assert rows[0]["state"] == "Failed" and "bad" in rows[0]["tone"]
    assert "heartbeat 240s ago" in rows[0]["meta"], "and the silence is still visible"


def test_ambient_says_stale_without_claiming_more_than_it_knows(page, server):
    rows = ambient_workers(page, server, {"jobd": beat("running", False, age_s=95.0),
                                          "web": beat("starting", True)})
    assert rows[0]["state"] == "Heartbeat stale" and "warn" in rows[0]["tone"]
    assert "heartbeat 95s ago" in rows[0]["meta"]
    assert "last known state: Running" in rows[0]["meta"]
    assert rows[1]["state"] == "Starting", "a fresh transient state is shown as itself"
    assert "bad" not in rows[1]["tone"], "and is not an alarm"


def test_ambient_reports_a_healthy_worker_and_a_service_plainly(page, server):
    rows = ambient_workers(page, server, {"web": beat("running", True, age_s=2.4)},
                           services={"web": {"ok": True}})
    assert rows[0]["state"] == "Running" and rows[0]["tone"].strip() == "ops-row-state"
    assert "heartbeat 2s ago" in rows[0]["meta"]
    assert rows[1]["title"].startswith("web · Service") and rows[1]["state"] == "Running"


@pytest.mark.parametrize("state", ["shutdown_timeout", "__proto__", "constructor"])
def test_ambient_shows_an_unknown_state_as_uncertain_not_as_healthy(page, server, state):
    rows = ambient_workers(page, server, {"jobd": beat(state, True)})
    assert rows[0]["state"] == state.replace("_", " "), "the producer's own word, not a guess"
    assert "warn" in rows[0]["tone"] and "unrecognised worker state" in rows[0]["meta"]
    assert page.errors == [], "JS errors: %r" % page.errors


@pytest.mark.parametrize("lang,failed,missing,stale,note", [
    ("zh", "失败", "没有心跳", "心跳已过期", "后台服务状态会自动更新"),
    ("zh-tw", "失敗", "沒有心跳", "心跳已過期", "背景服務狀態會自動更新"),
])
def test_ambient_worker_states_speak_the_readers_language(page, server, lang, failed, missing,
                                                          stale, note):
    _Fixture.lang = lang
    ambient_workers(page, server, {"ambient": beat("failed", True), "bridge": NO_HEARTBEAT,
                                   "jobd": beat("running", False, age_s=95.0),
                                   "web": beat("running", True)})
    lane = page.inner_text("#ambientOther")
    assert failed in lane and missing in lane and stale in lane and note in lane
    assert "运行中" in lane or "執行中" in lane, "the healthy worker reads as running"
    assert "Recovery required" not in lane and "需要恢复" not in lane and "需要復原" not in lane


# ------------------------------------------- the remote Pack page: Activity lane AND members lane
def test_remote_activity_lane_does_not_call_a_silent_worker_a_recovery_decision(page, server):
    rows = remote_workers(page, server, {"jobd": NO_HEARTBEAT, "web": NO_HEARTBEAT})
    assert [row["state"] for row in rows] == ["No heartbeat", "No heartbeat"]
    assert "Recovery required" not in page.inner_text("#remoteOtherActivity")
    assert all("warn" in row["tone"] for row in rows)
    assert all("no heartbeat recorded" in row["meta"] for row in rows)
    assert "Background service status updates automatically." in \
        page.inner_text("#remoteOtherActivity")
    recovery = page.inner_text("#remoteRecovery")
    assert "s-deploy" in recovery and "deploy.sh" in recovery
    assert page.locator("#remoteRecovery .activityactions button").count() == 3
    assert _Fixture.posts == [] and _Fixture.unauthenticated == []


def test_remote_activity_lane_does_not_report_a_fresh_failure_as_running(page, server):
    rows = remote_workers(page, server, {"ambient": beat("failed", True),
                                         "bridge": beat("dead", True),
                                         "jobd": beat("circuit_open", True)})
    assert [row["state"] for row in rows] == ["Failed", "Failed", "Restarts paused"]
    assert all("bad" in row["tone"] for row in rows)
    assert "Running" not in page.inner_text("#remoteOtherActivity")


def test_remote_activity_lane_says_stale_and_keeps_the_last_state(page, server):
    rows = remote_workers(page, server, {"jobd": beat("running", False, age_s=95.0),
                                         "web": beat("starting", True)})
    assert rows[0]["state"] == "Heartbeat stale" and "warn" in rows[0]["tone"]
    assert "last known state: Running" in rows[0]["meta"]
    assert rows[1]["state"] == "Starting"


@pytest.mark.parametrize("state", ["shutdown_timeout", "__proto__", "constructor"])
def test_remote_shows_an_unknown_state_as_uncertain_on_both_lanes(page, server, state):
    rows = remote_workers(page, server, {"jobd": beat(state, True)})
    assert rows[0]["state"] == state.replace("_", " ")
    assert "warn" in rows[0]["tone"] and "unrecognised worker state" in rows[0]["meta"]
    member = [row for row in pack_members(page) if row["title"] == "jobd"][0]
    assert member["state"] == state.replace("_", " "), "the members lane agrees, verbatim"
    assert "bad" not in member["tone"], "unknown is uncertainty, not a declared failure"
    assert page.errors == [], "JS errors: %r" % page.errors


def test_remote_members_lane_does_not_badge_a_fresh_failure_as_healthy(page, server):
    """The Pack members lane reported freshness *as* health, so a fresh failure read green."""
    remote_workers(page, server, {"jobd": beat("failed", True, age_s=2.0),
                                  "web": beat("running", True, age_s=2.0)})
    members = {row["title"]: row for row in pack_members(page)}
    assert members["jobd"]["state"] == "Failed" and "bad" in members["jobd"]["tone"]
    assert "Fresh" not in members["jobd"]["state"], "a fresh beat is not a healthy one"
    assert "heartbeat 2s ago" in members["jobd"]["meta"], "freshness stays visible as the age"
    assert members["web"]["state"] == "Running" and "bad" not in members["web"]["tone"]


def test_remote_members_lane_and_activity_lane_never_contradict_each_other(page, server):
    """One page, one vocabulary: the same beat may not read Failed in one lane and healthy below."""
    workers = {"ambient": beat("failed", True), "bridge": NO_HEARTBEAT,
               "jobd": beat("running", False, age_s=95.0), "web": beat("running", True)}
    lane = {row["title"].split(" · ")[0]: row for row in remote_workers(page, server, workers)}
    members = {row["title"]: row for row in pack_members(page)}
    for name in workers:
        assert lane[name]["state"] == members[name]["state"], name
        assert ("bad" in lane[name]["tone"]) == ("bad" in members[name]["tone"]), name
    assert members["bridge"]["state"] == "No heartbeat"
    assert "no heartbeat recorded" in members["bridge"]["meta"]
    assert _Fixture.posts == [], "rendering either lane writes nothing"


def test_remote_members_lane_keeps_reporting_this_collie_and_the_supervisor(page, server):
    """The worker fix may not cost the lane its other rows."""
    _Fixture.health = dict(HEALTH, workers={"web": beat("running", True)},
                           supervisor={"installed": True, "running": False, "status": "stopped"})
    page.goto(server + "/remote", wait_until="load")
    page.wait_for_selector("#packmembers .packrow", timeout=8000)
    members = pack_members(page)
    assert members[0]["state"] == "Responding" and "fixture" in members[0]["meta"]
    assert members[-1]["state"] == "Stopped" and "bad" in members[-1]["tone"]


@pytest.mark.parametrize("lang,failed,missing,note", [
    ("zh", "失败", "没有心跳", "后台服务状态会自动更新"),
    ("zh-tw", "失敗", "沒有心跳", "背景服務狀態會自動更新"),
])
def test_remote_worker_states_speak_the_readers_language(page, server, lang, failed, missing, note):
    _Fixture.lang = lang
    remote_workers(page, server, {"ambient": beat("failed", True), "bridge": NO_HEARTBEAT,
                                  "web": beat("running", True)})
    lane = page.inner_text("#remoteOtherActivity")
    assert failed in lane and missing in lane and note in lane
    assert "Recovery required" not in lane and "需要恢复" not in lane and "需要復原" not in lane
    members = page.inner_text("#packmembers")
    assert failed in members and missing in members


@pytest.mark.parametrize("surface", ["ambient", "remote"])
def test_dismissing_each_recovery_decision_sends_no_post(page, server, surface):
    dialogs = []
    page.on("dialog", lambda dialog: dialogs.append(dialog.message))
    if surface == "ambient":
        ambient_workers(page, server, {"web": NO_HEARTBEAT})
        buttons = page.locator("#ambientRecovery .ops-row-actions button")
    else:
        remote_workers(page, server, {"web": NO_HEARTBEAT})
        buttons = page.locator("#remoteRecovery .activityactions button")
    assert buttons.count() == 3
    for index in range(3):
        buttons.nth(index).click()
    assert len(dialogs) == 3 and _Fixture.posts == []
