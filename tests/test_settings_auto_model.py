def test_settings_round_trip_does_not_invent_or_pin_a_model(tmp_path, monkeypatch):
    from harness import settings
    from harness.router import resolve_run_decision

    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_PATH", str(path))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    for key in ("COLLIE_MODEL", "COLLIE_PROVIDER"):
        monkeypatch.delenv(key, raising=False)

    settings.save({"LANG": "zh", "PROVIDER": "codex-oauth"})
    displayed = settings.all_values()
    assert displayed["MODEL"] == ""

    # The Settings modal posts all displayed controls. A synthetic default here
    # used to turn an unrelated language edit into a permanent model pin.
    displayed["LANG"] = "en"
    saved = settings.update(displayed)
    assert "MODEL" not in saved
    assert settings.get("MODEL", "") == ""

    tiny = resolve_run_decision(
        "Fix this tiny typo", settings.get("PROVIDER", ""),
        model=settings.get("MODEL", "") or None, route_kind="code")
    hard = resolve_run_decision(
        "Fix this security race across multiple files", settings.get("PROVIDER", ""),
        model=settings.get("MODEL", "") or None, route_kind="code")
    assert tiny.model == "gpt-5.6-luna"
    assert hard.model == "gpt-5.6-sol"


def test_reusable_profile_authority_accepts_only_bounded_values(tmp_path, monkeypatch):
    import pytest
    from harness import settings

    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_PATH", str(path))
    monkeypatch.setattr(settings, "_cache", {"mtime": -1.0, "data": {}})
    saved = settings.save({"PROFILE_AGE_BAND": "18", "MAX_AUTO_AUTH_RISK": "medium"})
    assert saved["PROFILE_AGE_BAND"] == "18"
    with pytest.raises(ValueError):
        settings.save({"PROFILE_AGE_BAND": "17"})
    with pytest.raises(ValueError):
        settings.save({"MAX_AUTO_AUTH_RISK": "high"})
