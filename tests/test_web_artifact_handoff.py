"""Web contracts for editable Plan and selectable read-only Review handoffs."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest


@pytest.fixture
def artifact_server(monkeypatch, tmp_path):
    from harness import plantool, webapp

    monkeypatch.setenv("COLLIE_PLAN_DIR", str(tmp_path / "plans"))
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(plantool, "_DIR", str(tmp_path / "plans"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1], webapp.TOKEN
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)


def _json(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_plan_api_cas_user_approval_and_build_handoff(artifact_server):
    from harness.plantool import PlanArtifactStore

    base, token = artifact_server
    store = PlanArtifactStore()
    seeded = store.update("web:session-one", {
        "title": "Add export", "files": ["app.py"], "risks": ["format drift"],
        "checks": [{"command": "pytest -q"}],
        "todos": [{"id": "write", "content": "Implement export", "status": "pending"}],
    }, expected_revision=0, actor="model")

    code, got = _json(base + "/api/plan?session=session-one&token=" + token)
    assert code == 200 and got["artifact"]["revision"] == seeded["revision"]
    assert got["artifact"]["scope"] == "web:session-one"

    # State changes are CSRF authenticated and revision-CAS guarded.
    code, denied = _json(base + "/api/plan", "POST", {
        "session": "session-one", "revision": seeded["revision"], "title": "drive-by"})
    assert code == 403 and denied["error"] == "forbidden"

    code, saved = _json(base + "/api/plan?token=" + token, "POST", {
        "session": "session-one", "revision": seeded["revision"], "title": "Export safely",
        "files": ["app.py", "tests/test_app.py"], "risks": ["format drift"],
        "checks": [{"command": "pytest -q"}],
        "todos": [{"id": "write", "content": "Implement export", "status": "pending"}]})
    assert code == 200 and saved["artifact"]["title"] == "Export safely"
    assert saved["artifact"]["approved"] is False

    code, conflict = _json(base + "/api/plan?token=" + token, "POST", {
        "session": "session-one", "revision": seeded["revision"], "title": "stale"})
    assert code == 409 and conflict["conflict"] is True

    revision = saved["artifact"]["revision"]
    code, approved = _json(base + "/api/plan/approve?token=" + token, "POST", {
        "session": "session-one", "revision": revision})
    assert code == 200 and approved["artifact"]["approved"] is True
    assert approved["artifact"]["history"][-1]["actor"] == "user"
    assert approved["handoff"]["intent"] == "build"
    assert approved["handoff"]["session"] == "session-one"
    assert "Export safely" in approved["handoff"]["prompt"]


def test_review_artifact_is_structured_selectable_and_build_handoff(artifact_server):
    from harness import sessions, webapp

    base, token = artifact_server
    answer = """Findings:\n- [high] src/auth.py:41 - token check can be bypassed\n"""
    findings = webapp._review_findings(answer)
    assert findings == [{
        "id": findings[0]["id"], "path": "src/auth.py", "line": 41,
        "severity": "high", "message": "token check can be bypassed"}]
    sessions.save("review-one", [{"role": "assistant", "content": answer}], answer=answer)
    sessions.append_run_receipt("review-one", {
        "run": "r-review", "decision": {"intent": "review"},
        "review_findings": findings})

    code, artifact = _json(base + "/api/review?session=review-one&token=" + token)
    assert code == 200 and artifact["readonly"] is True
    assert artifact["findings"][0]["path"] == "src/auth.py"

    code, bad = _json(base + "/api/review/handoff?token=" + token, "POST", {
        "session": "review-one", "finding_ids": ["finding-tampered"]})
    assert code == 400 and "select" in bad["error"]

    code, handoff = _json(base + "/api/review/handoff?token=" + token, "POST", {
        "session": "review-one", "finding_ids": [findings[0]["id"]]})
    assert code == 200 and handoff["handoff"]["intent"] == "build"
    assert handoff["handoff"]["session"] == "review-one"
    assert "src/auth.py:41" in handoff["handoff"]["prompt"]


def test_review_run_persists_structured_artifact_without_write_authority(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from harness import cli, sessions, settings, webapp

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(webapp, "_provider", lambda: "mock")
    monkeypatch.setattr(settings, "apply", lambda: None)
    monkeypatch.setattr(settings, "get", lambda *args, **kwargs: "")
    closer = SimpleNamespace(close=lambda: None)
    seen = {}
    answer = ('Review complete.\n```json\n{"findings":[{"path":"lib/x.py","line":9,'
              '"severity":"high","message":"unchecked input"}]}\n```')

    class FakeHarness:
        def __init__(self, gate):
            self.gate = gate; self.composer = SimpleNamespace(identity="")
            self.memory = self.recorder = closer
            self.mode = "act"; self.force_edit = True; self.max_turns = 20
            self._max_turns_hard_cap = None; self.self_verify = False
            self.verify_max = 2; self.verify_gate = False; self.require_assert = False

        def run(self, task_id, message, history=None, **kwargs):
            seen.update(mode=self.mode, gate=self.gate.mode.value)
            return SimpleNamespace(
                answer=answer, error="", model="mock", prefix_tokens=0, input_tokens=0,
                output_tokens=0, total_tokens=0, turns=1, tool_calls=0, wall_ms=1,
                cost_usd=0, verified=False, canceled=False,
                messages=[{"role": "user", "content": message},
                          {"role": "assistant", "content": answer}])

    monkeypatch.setattr(cli, "make_harness", lambda *args, **kwargs: FakeHarness(kwargs["gate"]))
    events = []
    fake = object.__new__(webapp.Handler)
    fake._sse_open = lambda: None
    fake._sse = lambda kind, data: events.append((kind, data))
    with webapp.Handler._runs_lock:
        webapp.Handler._runs.clear(); webapp.Handler._cancel_events.clear()

    webapp.Handler._serve_stream(fake, {
        "q": ["review auth"], "session": ["review-stream"], "intent": ["review"],
        "quality": ["balanced"], "verification": ["auto"],
        "workspace": ["current"], "strategy": ["single"]})

    assert seen == {"mode": "review", "gate": "review"}
    done = next(data for kind, data in events if kind == "done")
    assert done["review_findings"][0]["path"] == "lib/x.py"
    saved = sessions.load("review-stream")
    assert saved["run_receipts"][-1]["review_findings"][0]["line"] == 9


def test_webui_contract_has_editable_plan_and_real_build_launch():
    page = (Path(__file__).parents[1] / "harness" / "webui" / "index.html").read_text("utf-8")
    for endpoint in ("/api/plan", "/api/plan/approve", "/api/review/handoff"):
        assert endpoint in page
    for field in ('data-plan="title"', 'data-plan="files"', 'data-plan="risks"',
                  'data-plan="checks"', "handoff-todos"):
        assert field in page
    assert "Approve &amp; build" in page
    assert 'config.intent = "build"' in page
    assert "runStream(handoff.prompt" in page
    assert 'data-finding="' in page and "Build selected fixes" in page
