import pytest

from test_channel_service import service  # noqa: F401
from test_channel_web import request, web  # noqa: F401


def test_brief_routes_require_authentication(web):
    assert request(web, "/api/brief", authenticated=False)[0] == 403
    assert request(web, "/api/brief", body={"action": "preview"}, authenticated=False)[0] == 403
    status, result, headers = request(web, "/api/brief?timezone=UTC&language=zh")
    assert status == 200, result
    assert result["brief"]["language"] == "zh"
    from harness.daily_brief import SOURCE_NAMES
    assert len(result["sources"]) == len(SOURCE_NAMES)
    assert result["brief"]["id"] and result["email"]["text"]
    assert headers["Cache-Control"] == "no-store"


def test_brief_preview_is_reachable_and_invalid_action_is_visible(web):
    status, result, _ = request(web, "/api/brief", body={"action": "preview", "timezone": "UTC"})
    assert status == 200 and result["email"]["available"], result
    assert request(web, "/api/brief", body={"action": "send"})[0] == 400
    status, content, headers = request(web, "/brief", authenticated=False)
    assert status == 200 and b'name="collie-token"' in content
    assert "script-src 'self' 'sha256-" in headers["Content-Security-Policy"]


def test_schedule_settings_are_authenticated_opt_in_and_do_not_send(web):
    _, _, (host, adapter) = web
    endpoint = "/api/brief/preferences"
    assert request(web, endpoint, authenticated=False)[0] == 403
    assert request(web, endpoint, body={"enabled": True}, authenticated=False)[0] == 403
    status, prefs, headers = request(web, endpoint)
    assert status == 200 and prefs["enabled"] is False
    assert headers["Cache-Control"] == "no-store"
    body = {"enabled": True, "connection": "mail", "timezone": "America/Los_Angeles",
            "at": "07:30", "language": "zh"}
    status, prefs, _ = request(web, endpoint, body=body)
    assert status == 200 and prefs["enabled"] is True, prefs
    assert prefs["language"] == "zh" and prefs["timezone"] == "America/Los_Angeles"
    assert "@example.test" in prefs["destination_masked"]
    assert not adapter.sent and not host.results("mail")
    assert request(web, endpoint, body={**body, "recipient": "stranger@example.test"})[0] == 400
    status, prefs, _ = request(web, endpoint, body={"enabled": False})
    assert status == 200 and prefs["enabled"] is False
    assert not adapter.sent


def test_send_me_one_now_is_authenticated_explicit_and_refused_without_an_account(web,
                                                                                 monkeypatch):
    _, _, (host, adapter) = web
    from harness import daily_brief_schedule, morning_report
    built = []
    monkeypatch.setattr(morning_report, "build",
                        lambda **kw: built.append(kw) or pytest.fail("must not build"))
    endpoint = "/api/brief/send-now"
    assert request(web, endpoint, body={"confirm": True}, authenticated=False)[0] == 403
    assert request(web, endpoint, authenticated=True)[0] in (400, 404, 405)   # a GET sends nothing
    # A POST without the explicit confirmation is refused before anything is read.
    status, answer, _ = request(web, endpoint, body={})
    assert status == 400 and "confirm" in answer["error"]
    # Confirmed, but no account was ever saved: refused with the reason, nothing built.
    status, answer, _ = request(web, endpoint, body={"confirm": True})
    assert status == 400 and "save" in answer["error"]
    assert built == [] and not adapter.sent


def test_send_me_one_now_sends_through_the_saved_account(web, monkeypatch):
    _, _, (host, adapter) = web
    from harness import daily_brief_schedule
    calls = []

    def fake_send_now(root, **kwargs):
        calls.append(root)
        return {"state": "submitted", "sent": True, "detail": "accepted", "delivery_known": False}

    monkeypatch.setattr(daily_brief_schedule, "send_now", fake_send_now)
    status, answer, _ = request(web, "/api/brief/send-now", body={"confirm": True})
    assert status == 200 and answer["state"] == "submitted" and len(calls) == 1
