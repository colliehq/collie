"""Saved winner review and application through the actual CLI and HTTP surfaces."""
import json
import os
from pathlib import Path
import subprocess
import sys

from harness import pack_artifacts, pack_review, sessions, session_owner
from test_web_task_inbox import web, _post, _get


def saved_case(web):
    base, token, state = web
    workspace = state.parent / "repo"
    attempt = state.parent / "attempt"
    workspace.mkdir(); attempt.mkdir()
    for directory in (workspace, attempt):
        (directory/"a.txt").write_text("before\n", encoding="utf-8")
        (directory/"notes.txt").write_text("original notes\n", encoding="utf-8")
    baseline = pack_artifacts.capture_baseline(str(attempt))
    (attempt/"a.txt").write_text("after\n", encoding="utf-8")
    record = pack_artifacts.save_artifact(pack_artifacts.create_artifact(
        str(attempt), baseline, workspace=str(workspace), metadata={"task":"Improve a.txt"}))
    sessions.save("pack-review", [{"role":"user", "content":"Improve a.txt"}], cwd=str(workspace))
    sessions.append_run_receipt("pack-review", {"pack":True,
        "artifact":pack_artifacts.summarize_artifact(record), "applied":False})
    return workspace, record


def test_http_review_apply_conflict_and_other_conversation_are_distinct(web):
    base, token, state = web
    workspace, record = saved_case(web)
    ident = record["id"]
    query = "/api/pack-artifact?session=pack-review&id="+ident
    code, data = _get(base, token, query)
    assert code == 200 and data["check"]["ok"]
    assert "-before" in data["files"][0]["diff"] and "+after" in data["files"][0]["diff"]
    assert (workspace/"a.txt").read_text() == "before\n"

    body = {"session":"pack-review", "id":ident}
    lease = session_owner.acquire("pack-review")
    try:
        code, data = _post(base, token, "/api/pack-artifact/apply", body)
        assert code == 409 and "running" in data["error"]
    finally:
        lease.release()
    code, data = _get(base, token, query.replace("pack-review", "other-thread"))
    assert code == 404
    (workspace/"a.txt").write_text("human edit\n")
    code, data = _post(base, token, "/api/pack-artifact/apply", body)
    assert code == 409 and data["code"] == "conflict"
    assert (workspace/"a.txt").read_text() == "human edit\n"
    (workspace/"a.txt").write_text("before\n")
    (workspace/"notes.txt").write_text("unrelated human edit\n")
    code, data = _post(base, token, "/api/pack-artifact/apply", body)
    assert code == 200 and data["applied"]
    assert (workspace/"a.txt").read_text() == "after\n"
    assert (workspace/"notes.txt").read_text() == "unrelated human edit\n"
    code, again = _post(base, token, "/api/pack-artifact/apply", body)
    assert code == 200 and again["applied"] and not again["changed"]


def test_http_apply_refuses_an_unresolved_prior_tool_boundary(web):
    base, token, state = web
    workspace, record = saved_case(web)
    sessions.checkpoint("pack-review", [], cwd=str(workspace), run_id="interrupted",
                        state="executing_tool", detail={"tool":"bash"})
    code, data = _post(base, token, "/api/pack-artifact/apply",
                       {"session":"pack-review", "id":record["id"]})
    assert code == 409 and "interrupted" in data["error"]
    assert (workspace/"a.txt").read_text() == "before\n"


def test_saved_cli_works_in_a_fresh_process_without_model_calls(web):
    base, token, state = web
    workspace, record = saved_case(web)
    environment = dict(os.environ, COLLIE_PROVIDER="definitely-not-a-provider",
                       COLLIE_RUNNER="not-a-worker")
    repo = Path(__file__).resolve().parents[1]
    def command(*args):
        done = subprocess.run([sys.executable, "-m", "harness.cli", "pack", "--saved",
            record["id"], "--json", *args], cwd=repo, env=environment,
            capture_output=True, text=True, timeout=20)
        assert done.returncode == 0, (done.stdout, done.stderr)
        return json.loads(done.stdout)
    reviewed = command()
    assert reviewed["check"]["ok"] and "+after" in reviewed["files"][0]["diff"]
    assert command("--apply")["applied"]
    assert (workspace/"a.txt").read_text() == "after\n"


def test_large_binary_preview_never_blocks_review_or_changes_apply_bytes(web):
    base, token, state = web
    workspace, record = saved_case(web)
    # The baseline/apply store handles binary data; only the human preview is bounded.
    attempt = state.parent/"binary-attempt"; attempt.mkdir()
    baseline = pack_artifacts.capture_baseline(str(attempt))
    raw = b"\0binary" * 20000
    (attempt/"new.bin").write_bytes(raw)
    bundle = pack_artifacts.save_artifact(pack_artifacts.create_artifact(
        str(attempt), baseline, workspace=str(workspace)))
    reviewed = pack_review.review(bundle["id"])
    assert reviewed["check"]["ok"] and reviewed["files"][0]["note"]
    assert pack_artifacts.apply_artifact(bundle["id"])["applied"]
    assert (workspace/"new.bin").read_bytes() == raw
