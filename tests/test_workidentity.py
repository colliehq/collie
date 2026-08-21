import json


def test_google_voice_connection_stores_only_masked_metadata(tmp_path, monkeypatch):
    from harness import browserbridge, workidentity

    calls = []

    def fake_call(command, timeout=60):
        calls.append(dict(command))
        if command["action"] == "attach":
            return {"ok": True, "data": {"attached": True}}
        if command["action"] == "voice_identity":
            return {"ok": True, "data": {"connected": True, "last4": "1234"}}
        return {"ok": True, "data": {"released": True}}

    monkeypatch.setattr(browserbridge, "_call", fake_call)
    row = workidentity.connect_google_voice("1234", str(tmp_path))
    raw = (tmp_path / "work-identities.json").read_text(encoding="utf-8")
    assert row["connected"] and row["account"] == "•••-•••-1234"
    assert "1234" in raw and "verification_code.read_and_fill" in raw
    assert "voice.messages.send" in raw and "voice.calls.place_receive" in raw
    assert "password" not in raw.lower() and "code" not in json.loads(raw)["google_voice"]
    assert calls[0]["action"] == "attach" and calls[0]["origin"] == "https://voice.google.com"


def test_collie_mail_is_discovered_as_a_connected_work_identity(tmp_path):
    from harness import workidentity

    (tmp_path / "mail.json").write_text(json.dumps({
        "handle": {"name": "daming", "verified": True, "priv": "do-not-copy"},
        "dogs": {"rowan": {"address": "rowan.daming@collie.run",
                            "priv": "private-dog-key", "cursor": 0}},
    }), encoding="utf-8")
    rows = workidentity.public_connections(str(tmp_path))
    mail = next(row for row in rows if row["id"] == "collie_mail")
    assert mail["connected"] and mail["account"] == "rowan.daming@collie.run"
    assert "signup.email_use" in mail["scopes"]
    assert "private" not in json.dumps(mail).lower() and "do-not-copy" not in json.dumps(mail)


def test_mission_receives_discovered_work_mailbox_context(tmp_path):
    from harness.actions import ActionStore
    from harness.mission import MissionDriver, MissionStore, create_mission, world_leash

    (tmp_path / "mail.json").write_text(json.dumps({
        "dogs": {"rowan": {"address": "rowan.daming@collie.run", "priv": "secret"}},
    }), encoding="utf-8")
    store = MissionStore(str(tmp_path / "jobs.db"))
    actions = ActionStore(str(tmp_path / "actions.db"))
    seen = {}

    def decide(_goal, case, _primitives):
        seen.update(case)
        return {"action": "needs_human", "args": {"summary": "done"}}

    create_mission(store, "mail-context", "create an authorized service account",
                   leash=world_leash(autonomous=True))
    MissionDriver(store, actions, decide, []).advance("mail-context")
    encoded = json.dumps(seen.get("_connected_work_identities"), sort_keys=True)
    assert "{{work_identity:collie_mail:account}}" in encoded
    assert "rowan.daming@collie.run" not in encoded and "secret" not in encoded
    store.close()
    actions.close()


def test_browser_resolves_mail_reference_without_persisting_address(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from harness.primitives import _browse_verify, _real_browse
    from harness.verifier import VERIFIED

    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    (tmp_path / "mail.json").write_text(json.dumps({
        "dogs": {"rowan": {"address": "rowan.daming@collie.run", "priv": "secret"}},
    }), encoding="utf-8")
    ref = "{{work_identity:collie_mail:account}}"
    seen = []
    execute = _real_browse(
        runner=lambda goal: seen.append(goal) or "Email field filled",
        form_reader=lambda: [{"label": "Email", "value": "never-return-this",
                              "sensitive": True, "filled": True}])
    rec = SimpleNamespace(args={"goal": "Fill Email with %s; do not submit." % ref,
                                "expect": {"Email": ref}, "read_only": False},
                          job_id="mail-ref")
    result = execute(rec)
    assert "rowan.daming@collie.run" in seen[0]
    assert "rowan.daming@collie.run" not in json.dumps(result)
    assert result["form"][0]["value"] == "[redacted]"
    assert _browse_verify(rec, result).status == VERIFIED


def test_verification_fill_never_returns_or_records_the_code():
    from types import SimpleNamespace
    from harness.primitives import _real_verification_fill
    from harness.webact import FakeActuator

    class CodeForm(FakeActuator):
        def snapshot(self):
            return {"url": "https://example.test/verify",
                    "snapshot": '[e7] textbox "Verification code"'}

    actuator = CodeForm()
    seen = []

    def reader(service, max_age_seconds=600):
        seen.append((service, max_age_seconds))
        return "654321", {"source": "google_voice", "account": "•••-•••-1234",
                          "received_at": 10}

    execute = _real_verification_fill(actuator, reader)
    result = execute(SimpleNamespace(
        args={"service": "Example", "field": "Verification code"}, job_id="m1"))
    encoded = json.dumps(result)
    assert result["filled"] and result["case"]["verification_code_filled"]
    assert seen == [("Example", 600)] and "654321" not in encoded
    assert actuator.calls[-1] == ("type_ref", "e7", "[sensitive]", False)
