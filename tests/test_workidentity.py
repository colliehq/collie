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
