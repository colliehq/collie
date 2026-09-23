import os

from harness import sessions
from test_web_task_inbox import web, _get, _call


def test_a_new_tasks_boot_question_is_answered_with_a_resolved_folder(web):
    """The question a new tab asks before anything is typed: no thread, no chosen folder.

    The page reads a 200 here as "this is where a new task runs" and lets Send start there, so
    the route has to mean it: a 200 names one absolute, existing directory, and a root it cannot
    resolve is a refusal rather than an answer with nothing in it.
    """
    base, token, state = web
    code, data = _call(base + "/api/verification")
    assert code == 200, data
    assert isinstance(data["cwd"], str) and data["cwd"].strip(), \
        "a 200 with no folder in it would be read as a settled working folder"
    assert os.path.isabs(data["cwd"]) and os.path.isdir(data["cwd"])
    assert data["cwd"] == os.path.abspath(data["cwd"]), "the answer is the resolved path"
    assert _get(base, token, "/api/verification")[1]["cwd"] == data["cwd"]


def test_new_folder_selection_is_authenticated_and_never_retargets_a_saved_thread(web):
    from urllib.parse import urlencode
    base, token, state = web
    folder = state / 'chosen'; folder.mkdir()
    query = urlencode({'cwd':str(folder)})
    assert _call(base+'/api/verification?'+query)[0] == 403
    code, data = _get(base,token,'/api/verification?'+query)
    assert code == 200 and data['cwd'] == str(folder)
    sessions.save('pinned',[],cwd=str(state))
    code,data = _get(base,token,'/api/verification?session=pinned&'+query)
    assert code == 200 and data['cwd'] == str(state)


def test_check_discovery_follows_saved_project_and_requires_auth(web):
    base, token, state = web
    repo = state.parent / "actual-project"
    repo.mkdir()
    (repo / "package.json").write_text('{"scripts":{"test":"node --test"}}', encoding="utf-8")
    sessions.save("project", [], cwd=str(repo))
    code, denied = _call(base + "/api/verification?session=project")
    assert code == 403
    code, data = _get(base, token, "/api/verification?session=project")
    assert code == 200 and data["cwd"] == str(repo)
    assert any(row["command"] == "npm run test" for row in data["candidates"])


def test_missing_or_invalid_project_never_uses_launcher_directory(web):
    base, token, state = web
    code, _ = _get(base, token, "/api/verification?session=missing")
    assert code == 404
    sessions.save("gone", [], cwd=str(state / "deleted"))
    code, data = _get(base, token, "/api/verification?session=gone")
    assert code == 409 and "session workspace" in data["error"]
    (state / "sessions" / "bad.json").write_text("{", encoding="utf-8")
    code, _ = _get(base, token, "/api/verification?session=bad")
    assert code == 409
