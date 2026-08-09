"""Slack listener integration checks for the lease-based Presence client."""
from __future__ import annotations

import threading

from harness import presence
from harness import slackbot


class _Worker:
    def __init__(self, alive=True):
        self.alive = alive

    def is_alive(self):
        return self.alive


def test_presence_is_opt_in_and_partial_configuration_fails_closed(capsys):
    ready = threading.Event()
    worker = _Worker()
    assert slackbot._start_presence("", {}, "T123", "U123", ready, worker) is None
    assert capsys.readouterr().err == ""

    # Never echo the supplied credential when explaining a partial setup.
    secret = "presence-secret-that-must-not-be-printed"
    assert slackbot._start_presence("", {"presence_token": secret},
                                    "T123", "U123", ready, worker) is None
    err = capsys.readouterr().err
    assert "missing Worker URL" in err
    assert secret not in err


def test_presence_uses_stable_slack_ids_and_samples_real_readiness(monkeypatch):
    made = []

    class FakePresenceClient:
        def __init__(self, base_url, pack, dog, token, **kwargs):
            self.base_url = base_url
            self.pack = pack
            self.dog = dog
            self.token = token
            self.health_fn = kwargs["health_fn"]
            self.started = False

        def start(self):
            self.started = True

        def stop(self):
            pass

    def factory(*args, **kwargs):
        client = FakePresenceClient(*args, **kwargs)
        made.append(client)
        return client

    monkeypatch.setattr(presence, "PresenceClient", factory)
    ready = threading.Event()
    worker = _Worker()
    client = slackbot._start_presence(
        "wss://presence.example/", {"presence_token": "dog-token"},
        "T_WORKSPACE", "U_BOT", ready, worker,
    )
    assert client is made[0] and client.started
    assert (client.base_url, client.pack, client.dog) == (
        "wss://presence.example", "T_WORKSPACE", "U_BOT")
    assert client.health_fn() is False
    ready.set()
    assert client.health_fn() is True
    worker.alive = False
    assert client.health_fn() is False


def test_presence_environment_token_overrides_private_kennel(monkeypatch):
    captured = {}

    class FakePresenceClient:
        def __init__(self, _url, _pack, _dog, token, **_kwargs):
            captured["token"] = token

        def start(self):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(presence, "PresenceClient", FakePresenceClient)
    monkeypatch.setenv("COLLIE_PRESENCE_TOKEN", "environment-token")
    slackbot._start_presence(
        "wss://presence.example", {"presence_token": "kennel-token"},
        "T1", "U1", threading.Event(), _Worker(),
    )
    assert captured["token"] == "environment-token"


def test_setup_saves_presence_and_stable_slack_identity(tmp_path, monkeypatch, capsys):
    store = tmp_path / "slack.json"
    monkeypatch.setattr(slackbot, "STORE", str(store))
    slackbot.save_kennel({
        "Moss": {"app_id": "A123", "bot_token": "xoxb-old"},
    })
    monkeypatch.setattr(
        slackbot, "api",
        lambda method, _token, **_params: {
            "ok": True, "user": "moss", "team": "Collie",
            "team_id": "T123", "user_id": "U123",
        } if method == "auth.test" else {"ok": True},
    )
    # Avoid writing an avatar outside this isolated setup test.
    from harness import avatar
    monkeypatch.setattr(avatar, "write", lambda _name: "")

    rc = slackbot.setup([
        "--name", "Moss", "--app-token", "xapp-new",
        "--presence-url", "wss://presence.example/",
        "--presence-token", "dog-secret",
    ])
    dog = slackbot.load_kennel()["Moss"]
    assert rc == 0
    assert dog["presence_url"] == "wss://presence.example"
    assert dog["presence_token"] == "dog-secret"
    assert dog["team_id"] == "T123"
    assert dog["bot_user_id"] == "U123"
    assert slackbot.setup(["--list"]) == 0
    listing = capsys.readouterr().out
    assert "pack T123 · dog U123" in listing
