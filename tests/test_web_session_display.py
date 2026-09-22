"""The history view renders accepted attachment metadata, not host prompt prose."""
from harness import sessions, task_inbox, run_ownership
from test_web_task_inbox import web, _post, _get, CONFIG, PNG


def test_reopened_attachment_has_clean_display_and_unchanged_model_content(web):
    base, token, state = web
    sid = "clean-attachment"
    code, upload = _post(base, token, "/api/upload", {"media_type":"image/png", "data":PNG})
    assert code == 200
    request = "Review the screenshot. [IDE context attached by user] is text I typed."
    code, accepted = _post(base, token, "/api/task-inbox", {
        "session":sid, "id":"with-context", "text":request, "config":CONFIG,
        "images":[upload["id"]], "contexts":[{"kind":"selection", "path":"app.py",
            "startLine":7, "endLine":9, "content":"the exact selected source"}]})
    assert code == 200
    entry = task_inbox.get(sid, "with-context")
    expanded = run_ownership.entry_content(sid, entry)
    original = {"role":"user", "inbox_id":entry["id"], "content":expanded}
    sessions.save(sid, [original], cwd=str(state.parent))
    code, response = _get(base, token, "/api/session/"+sid)
    assert code == 200
    rendered = response["messages"][0]
    assert rendered["content"] == expanded
    assert rendered["display"]["text"] == request
    assert rendered["display"]["contexts"][0]["content"] == "the exact selected source"
    assert "display" not in sessions.load(sid)["messages"][0]
