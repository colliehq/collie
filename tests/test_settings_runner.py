"""The Worker axis in the Settings panel: RUNNER / RUNNER_POOL.

These keys decide *whose* login and billing route a task runs on, so the tests pin the parts a
silent edit could change without anyone noticing: the default must stay `collie` (that is the
"nothing changes unless you ask" promise), the option values must stay exactly the runner keys the
registry knows about, and both keys must be translated — an untranslated billing-relevant control
is how a Chinese user ends up consenting to something they could not read.
"""
from __future__ import annotations

from harness import settings

RUNNER_OPTION_KEYS = ("collie", "auto", "codex-exec", "claude-code")


def _row(key):
    return next(r for r in settings.SCHEMA if r["key"] == key)


def test_runner_keys_exist_in_schema():
    keys = [r["key"] for r in settings.SCHEMA]
    assert "RUNNER" in keys
    assert "RUNNER_POOL" in keys
    # exactly once each — a duplicated row would render two controls writing the same field
    assert keys.count("RUNNER") == 1
    assert keys.count("RUNNER_POOL") == 1


def test_runner_defaults_are_collie_only():
    assert _row("RUNNER")["default"] == "collie"
    assert _row("RUNNER_POOL")["default"] == "collie"
    # get() has no knowledge of SCHEMA defaults — callers pass them — so an unset key must read as
    # "nothing configured", which callers turn into collie.
    assert settings.get("RUNNER", _row("RUNNER")["default"]) == "collie"
    assert settings.get("RUNNER_POOL", _row("RUNNER_POOL")["default"]) == "collie"


def test_runner_option_values_match_the_static_runner_list():
    row = _row("RUNNER")
    assert row["type"] == "select"
    assert tuple(o["value"] for o in row["options"]) == RUNNER_OPTION_KEYS


def test_runner_pool_is_free_text():
    row = _row("RUNNER_POOL")
    assert row["type"] == "text"
    assert "options" not in row


def test_runner_rows_are_localized():
    for key in ("RUNNER", "RUNNER_POOL"):
        assert key in settings._ZH
        row = _row(key)
        assert row.get("label_zh")
        assert row.get("hint_zh")
    # every selectable worker is named in Chinese too, not just the control
    assert all(o.get("label_zh") for o in _row("RUNNER")["options"])


def test_hints_explain_worker_vs_brain_and_the_pool_consent():
    runner_hint = _row("RUNNER")["hint"]
    assert "Provider/Model" in runner_hint  # says which axis this is NOT
    assert "Worker pool" in runner_hint     # says where auto is allowed to look
    pool_hint = _row("RUNNER_POOL")["hint"]
    assert "consent" in pool_hint and "billing" in pool_hint
    assert "never picked automatically" in pool_hint


def test_runner_settings_carry_no_credentials():
    # the pool is a list of runner names; nothing here may invite a token or key
    for key in ("RUNNER", "RUNNER_POOL"):
        row = _row(key)
        blob = " ".join(str(v) for v in row.values()).lower()
        for word in ("api key", "token", "password", "secret"):
            assert word not in blob
