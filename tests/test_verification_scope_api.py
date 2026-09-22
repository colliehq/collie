from harness import sessions
from test_web_task_inbox import web, _get, _call


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
