"""Risk overrides — the only way down the ladder, and only the user may take it."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from harness.gate import Gate, Mode
from harness.overrides import RiskOverrideStore
from harness.risk import RiskClass, classify


@pytest.fixture()
def store(tmp_path):
    return RiskOverrideStore(str(tmp_path / "ov.json"))


def test_no_rules_changes_nothing(store):
    assert store.lookup("mcp__fs__read_file") is None
    assert classify("mcp__fs__read_file", overrides=store.resolver()) is RiskClass.EXTERNAL


def test_relaxing_a_trusted_read_only_server(store):
    """The case this exists for: stop being asked about a server you have read."""
    store.set("mcp__fs__read_*", RiskClass.READ)
    assert classify("mcp__fs__read_file", overrides=store.resolver()) is RiskClass.READ
    assert classify("mcp__fs__write_file", overrides=store.resolver()) is RiskClass.EXTERNAL


def test_most_specific_rule_wins(store):
    store.set("mcp__fs__*", RiskClass.READ)
    store.set("mcp__fs__delete_file", RiskClass.EXTERNAL)
    r = store.resolver()
    assert classify("mcp__fs__list_dir", overrides=r) is RiskClass.READ
    assert classify("mcp__fs__delete_file", overrides=r) is RiskClass.EXTERNAL


def test_exact_beats_glob_regardless_of_length(store):
    store.set("mcp__very_long_server_name__*", RiskClass.READ)
    store.set("mcp__x__y", RiskClass.EXEC)
    assert classify("mcp__x__y", overrides=store.resolver()) is RiskClass.EXEC


def test_an_override_can_also_tighten(store):
    """It is not only a relaxation mechanism — a user may decide bash is too much here."""
    store.set("bash", RiskClass.EXTERNAL)
    assert classify("bash", overrides=store.resolver()) is RiskClass.EXTERNAL


def test_setting_the_same_pattern_replaces_it(store):
    store.set("mcp__a__*", RiskClass.READ)
    store.set("mcp__a__*", RiskClass.EXEC)
    assert [r.risk for r in store.list()] == [RiskClass.EXEC]


def test_unset(store):
    store.set("mcp__a__*", RiskClass.READ)
    assert store.unset("mcp__a__*") is True
    assert store.unset("mcp__a__*") is False
    assert store.lookup("mcp__a__thing") is None


def test_revocation_takes_effect_without_a_restart(store):
    """The resolver reads on every call on purpose: a revoked trust that only applies
    at next launch is not a revocation."""
    store.set("mcp__a__*", RiskClass.READ)
    r = store.resolver()
    assert classify("mcp__a__x", overrides=r) is RiskClass.READ
    store.unset("mcp__a__*")
    assert classify("mcp__a__x", overrides=r) is RiskClass.EXTERNAL


# -- robustness -------------------------------------------------------------
def test_a_corrupt_store_overrides_nothing(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text("{not json", encoding="utf-8")
    assert RiskOverrideStore(str(p)).lookup("anything") is None


def test_one_malformed_rule_does_not_drop_the_others(tmp_path):
    p = tmp_path / "ov.json"
    p.write_text(json.dumps({"rules": [
        {"pattern": "mcp__a__*", "risk": "nonsense"},
        {"pattern": "mcp__b__*", "risk": "read"},
        {"no_pattern": True},
    ]}), encoding="utf-8")
    s = RiskOverrideStore(str(p))
    assert s.lookup("mcp__a__x") is None
    assert s.lookup("mcp__b__x") is RiskClass.READ


def test_survives_a_reopen(tmp_path):
    p = str(tmp_path / "ov.json")
    RiskOverrideStore(p).set("mcp__a__*", RiskClass.READ)
    assert RiskOverrideStore(p).lookup("mcp__a__x") is RiskClass.READ


# -- through the gate -------------------------------------------------------
def test_the_gate_honours_an_override(tmp_path, store):
    store.set("mcp__fs__read_*", RiskClass.READ)
    g = Gate(cwd=tmp_path, mode=Mode.PROJECT, risk_overrides=store.resolver())
    assert g.evaluate("mcp__fs__read_file", {"path": "x"}).allowed
    assert g.evaluate("mcp__fs__delete_all", {}).needs_user


def test_relaxing_does_not_reach_past_the_read_only_modes(tmp_path, store):
    """An override changes the CLASS, not the mode. plan stays read-only."""
    store.set("bash", RiskClass.EXEC)
    g = Gate(cwd=tmp_path, mode=Mode.PLAN, risk_overrides=store.resolver())
    d = g.evaluate("bash", {"command": "ls"})
    assert not d.allowed and not d.needs_user


def test_no_tool_can_write_this_store():
    """The invariant. If something collie loaded could reclassify itself, the gate would
    be decorative — so there is deliberately no tool, and the registry must not grow one."""
    from harness.tools import default_registry
    reg = default_registry(code_search=True, web_search=True, exec_code=True, delegate=True)
    for name in reg.names():
        tool = reg.get(name)
        src = "%s %s" % (getattr(tool, "description", ""), type(tool).__module__)
        assert "risk_overrides" not in src and "RiskOverrideStore" not in src, name
