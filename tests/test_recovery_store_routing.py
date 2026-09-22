"""Real default-layout journals must be visible and use the executor's lease."""
import json
import os
import subprocess
import sys

from harness import cli, sessions, session_owner
from test_web_task_inbox import web, _get, _post


def test_default_state_layout_is_visible_in_new_process(tmp_path):
    env = dict(os.environ)
    env["COLLIE_STATE_DIR"] = str(tmp_path)
    env.pop("COLLIE_SESSIONS_DIR", None)
    env.pop("COLLIE_DATA_DIR", None)
    code = '''
import json
from harness import sessions
from harness.controlplane import activity
sessions.checkpoint("fence", [{"role":"user","content":"private instruction"}],
    state="external_action", detail={"tool_name":"send"})
print(json.dumps({"store":sessions.store_root(),"activity":activity()}))
'''
    result = subprocess.run([sys.executable, "-c", code], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert os.path.normpath(data["store"]) == str(tmp_path / "data" / "sessions")
    assert data["activity"]["sessions"][0]["recovery_required"] is True


def test_recovery_api_and_lease_use_the_same_default_store(web, monkeypatch):
    base, token, state = web
    monkeypatch.delenv("COLLIE_SESSIONS_DIR")
    monkeypatch.setattr(cli, "DATA", str(state / "data"))
    sessions.checkpoint("fence", [], state="external_action", detail={"tool_name":"send"})
    code, listing = _get(base, token, "/api/recovery")
    assert code == 200 and listing["sessions"][0]["session_id"] == "fence"
    lease = session_owner.try_acquire("fence", label="running")
    assert lease is not None
    request = {"session":"fence", "resolution":"not_fired", "confirmed":True}
    try:
        code, response = _post(base, token, "/api/recovery/reconcile", request)
        assert code == 409 and "running" in response["error"]
        assert sessions.recovery_state("fence")["recovery_required"] is True
    finally:
        lease.release()
    code, response = _post(base, token, "/api/recovery/reconcile", request)
    assert code == 200 and response["ok"]
    assert not sessions.recovery_state("fence").get("recovery_required")
    assert not (state / "sessions").exists()


def test_explicit_other_installation_does_not_follow_current_overrides(web):
    _, _, state = web
    other = state.parent / "other"
    assert sessions.store_root(str(other)) == str(other / "data" / "sessions")
    assert sessions.store_root(str(state)) == str(state / "sessions")
