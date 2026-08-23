"""`collie run --runner` and `collie runners` — the CLI half of the worker axis.

Nothing here launches a worker, probes a host or calls a model. The questions
this file asks are about the command's own wiring — does the default path stay
free, does an unavailable worker refuse instead of substituting, does the
`verified` flag still come from the host verifier and nowhere else — and every one
of them would be answered less honestly by a real `codex exec` on whatever account
this machine happens to be logged into. So `runner_registry.probe_all` and
`runner_slice.run_adhoc` are the two seams, and the router is pinned too: a
heuristic that reclassified the task as chat would fail these tests for a reason
that has nothing to do with what they are testing.

The one thing deliberately NOT faked is the default path's probe: it is replaced
with a function that raises, because "we never called it" is the claim.
"""
from __future__ import annotations

import argparse
import json
import os

import pytest

from harness import cli, runner_registry, runner_select, runner_slice, settings
from harness import router, verification
from harness.recorder import RunResult
from harness.router import RunDecision
from harness.runner_specs import RunnerProbe, RunnerReceipt


NOW = 1_800_000_000.0


# --- fixtures ---------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Sessions, runs.db and the dashboard go to a temp dir, and settings answer defaults.

    Without the settings stub these tests would read the developer's own
    ~/.collie/settings.json, where a RUNNER of anything but `collie` would quietly
    invert what `test_default_path_never_probes` proves.
    """
    state = tmp_path / "state"
    (state / "data").mkdir(parents=True)
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(state / "sessions"))
    monkeypatch.setattr(cli, "_paths", lambda: (
        str(state / "data" / "memory.db"), str(state / "data" / "runs.db"),
        str(state / "data" / "dashboard.html"), str(state / "data" / "sandbox")))
    monkeypatch.setattr(settings, "get", lambda key, default=None: default)
    return state


def _decision(**over) -> RunDecision:
    """A build/code route — the only shape an external worker is allowed (H2)."""
    values = dict(
        provider="mock", model="mock-coder-v1", effort="default", speed="standard",
        billing_multiplier=1.0, intent="build", quality="balanced",
        verification="auto", workspace="current", strategy="single",
        route_kind="code", complexity="simple")
    values.update(over)
    return RunDecision(**values)


def _pin_router(monkeypatch, decision: RunDecision) -> None:
    """cmd_run imports resolve_run_decision at call time, so patch the module."""
    monkeypatch.setattr(router, "resolve_run_decision",
                        lambda *a, **kw: decision)


def _args(**over) -> argparse.Namespace:
    base = dict(task="rename the helper", cwd="", provider="mock", model=None,
                project="demo", mode=None, persona=None, goal=None, resume=None,
                cont=False, stream_json=False, json=True, print=False,
                web_search=False, intent="build", quality="balanced",
                verification="auto", effort=None, speed=None,
                verify_command=None, runner=None)
    base.update(over)
    return argparse.Namespace(**base)


def _probe(key: str, **over) -> RunnerProbe:
    values = dict(key=key, installed=True, executable_path="/usr/bin/" + key,
                  version="99.0.0", login="ok", billing_class="subscription_allowance",
                  billing_mode="subscription", probed_at=NOW)
    values.update(over)
    return RunnerProbe(**values)


def _receipt(runner: str, locator: str) -> RunnerReceipt:
    """A receipt in the shape run_adhoc attaches — settled, and NOT verified."""
    spec = runner_registry.SPECS[runner]
    return RunnerReceipt(
        runner=runner, runner_version="99.0.0", runner_protocol=spec.caps.protocol,
        runner_protocol_version="", billing_class="subscription_allowance",
        billing_mode="subscription", credential_family=spec.credential_family,
        decision={}, native_session={"runner": runner, "workspace": "/ws",
                                     "locator": locator, "protocol_version": "",
                                     "created_at": NOW, "workspace_digest": ""},
        usage={"known": False}, usage_known=False, cost_usd_reported=None,
        cost_usd_equivalent=None, model="", settled=True, recovery_required=False,
        mutated=True, events_digest="d" * 8, event_count=2, approvals=(),
        env_receipt={"allowed": ["PATH"], "stripped": ["OPENAI_API_KEY"]})


def _worker_result(runner: str, locator: str = "th_123", answer: str = "renamed it"):
    """What run_adhoc returns for a settled external turn: unknown usage, no messages."""
    result = RunResult(task_id="adhoc", harness=runner, model="", provider="codex",
                       input_tokens=None, output_tokens=None, total_tokens=None,
                       cache_read=None, cache_creation=None, turns=1, wall_ms=1200,
                       success=True, verified=False, cost_usd=None, answer=answer,
                       messages=[])
    setattr(result, runner_slice.RECEIPT_ATTR, _receipt(runner, locator))
    return result


def _fake_run_adhoc(monkeypatch, result, seen: dict):
    def run_adhoc(decision, task, workspace, **kwargs):
        seen.update(kwargs)
        seen["decision"] = decision
        seen["task"] = task
        seen["workspace"] = workspace
        return result
    monkeypatch.setattr(runner_slice, "run_adhoc", run_adhoc)


# --- collie run --runner ----------------------------------------------------
def test_run_with_external_runner_json_and_receipt(monkeypatch, tmp_path, capsys,
                                                   isolated_state):
    """A pinned worker runs, and BOTH ledgers say who did the work and who verified it.

    The receipt's `settled` is the worker's word for "I stopped cleanly"; the
    `verified` beside it is the host verifier's, produced after the worker exited.
    Conflating those two is the exact failure this whole layer exists to prevent,
    so the test asserts they came from different places.
    """
    _pin_router(monkeypatch, _decision(verification="required"))
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe("codex-exec")})
    seen: dict = {}
    _fake_run_adhoc(monkeypatch, _worker_result("codex-exec", locator="th_abc"), seen)
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda command, cwd, **kw: {
                            "command": command, "exit_code": 0, "passed": True,
                            "command_passed": True, "output": "2 passed",
                            "freshness": "fresh", "source": "user"})

    args = _args(cwd=str(tmp_path), runner="codex-exec",
                 verification="required", verify_command="python -m pytest -q")
    assert cli.cmd_run(args) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    # The selection is on the decision, and the outcome is on its own key.
    assert payload["decision"]["runner"]["runner"] == "codex-exec"
    assert payload["decision"]["runner"]["source"] == "user"
    assert payload["runner"]["runner"] == "codex-exec"
    assert payload["runner"]["settled"] is True
    assert payload["runner"]["usage_known"] is False
    # Unknown usage stays None all the way out: a 0 would read as a free run.
    assert payload["input_tokens"] is None and payload["cost_usd"] is None

    # `verified` came from the host verifier, not from the worker settling.
    assert payload["verification_evidence"]["passed"] is True

    # run_adhoc got the workspace, the spec's timeout and an emitter.
    assert seen["workspace"] == str(tmp_path)
    assert seen["timeout_s"] == runner_registry.SPECS["codex-exec"].default_timeout_s
    assert callable(seen["emit"]) and seen["resume_from"] is None
    # A Brain name is not portable: `mock-coder-v1` must not reach `codex exec`.
    assert seen["model"] == ""

    session_file = isolated_state / "sessions" / (payload["session"] + ".json")
    receipts = json.loads(session_file.read_text(encoding="utf-8"))["run_receipts"]
    assert receipts[-1]["verified"] is True
    assert receipts[-1]["runner"]["native_session"]["locator"] == "th_abc"
    assert receipts[-1]["runner"]["env_receipt"]["stripped"] == ["OPENAI_API_KEY"]

    # Now the same settled worker with a check that fails. The receipt still says
    # settled — the worker did stop cleanly — and `verified` goes to False, which is
    # the whole reversal: the worker's own word never sets it.
    monkeypatch.setattr(verification, "run_verification_command",
                        lambda command, cwd, **kw: {
                            "command": command, "exit_code": 1, "passed": False,
                            "command_passed": False, "output": "1 failed",
                            "freshness": "fresh", "source": "user"})
    assert cli.cmd_run(_args(cwd=str(tmp_path), runner="codex-exec",
                             verify_command="python -m pytest -q")) == 1
    failed = json.loads(capsys.readouterr().out.strip())
    assert failed["runner"]["settled"] is True
    assert "required check failed" in failed["error"]
    failed_receipts = json.loads(
        (isolated_state / "sessions" / (failed["session"] + ".json")).read_text(
            encoding="utf-8"))["run_receipts"]
    assert failed_receipts[-1]["verified"] is False


def test_run_runner_unavailable_exit_2(monkeypatch, tmp_path, capsys):
    """A pin that cannot run refuses; it never silently becomes a different worker.

    Substituting would change which account pays, so exit 2 with the reason is the
    only honest answer — and `run_adhoc` must not be reached at all.
    """
    _pin_router(monkeypatch, _decision())
    monkeypatch.setattr(runner_registry, "probe_all", lambda keys=None, **kw: {
        "codex-exec": _probe("codex-exec", installed=False, login="unknown",
                             executable_path="", version="",
                             detail="codex is not on PATH")})

    def refuse(*a, **kw):
        raise AssertionError("run_adhoc must not be reached after a refusal")
    monkeypatch.setattr(runner_slice, "run_adhoc", refuse)

    assert cli.cmd_run(_args(cwd=str(tmp_path), runner="codex-exec")) == 2
    captured = capsys.readouterr()
    assert "codex-exec" in captured.err and "codex is not on PATH" in captured.err
    assert captured.out.strip() == ""      # no JSON result for a run that never ran


def test_run_external_rejects_goal_persona(monkeypatch, tmp_path, capsys):
    """--persona/--goal are harness features; dropping them silently is not an option."""
    _pin_router(monkeypatch, _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe("codex-exec")})

    def refuse(*a, **kw):
        raise AssertionError("no worker may start once the run is refused")
    monkeypatch.setattr(runner_slice, "run_adhoc", refuse)

    for over in ({"goal": "ship the release"}, {"persona": "reviewer"}):
        args = _args(cwd=str(tmp_path), runner="codex-exec", **over)
        assert cli.cmd_run(args) == 2
        captured = capsys.readouterr()
        assert "--persona/--goal" in captured.err and "codex-exec" in captured.err
        assert captured.out.strip() == ""


def test_default_path_never_probes(monkeypatch, tmp_path, capsys):
    """RUNNER=collie with no --runner costs exactly nothing.

    Both probe entry points are replaced with functions that raise: the claim is
    not "the probe was cheap", it is "no probe happened", and a passing assertion
    on elapsed time would not have said that.
    """
    _pin_router(monkeypatch, _decision())

    def never(*a, **kw):
        raise AssertionError("the default path must not probe any external worker")
    monkeypatch.setattr(runner_registry, "probe", never)
    monkeypatch.setattr(runner_registry, "probe_all", never)
    monkeypatch.setattr(runner_registry, "make_runner", never)
    monkeypatch.setattr(runner_slice, "run_adhoc", never)

    class FakeHarness:
        def __init__(self):
            self.memory = self.recorder = type(
                "Closer", (), {"close": lambda self: None,
                               "finish_run": lambda self, res: None})()
            self.provider = argparse.Namespace(actual_speed="standard")
            self.mode = "act"; self.force_edit = True; self.max_turns = 20
            self._max_turns_hard_cap = None; self.self_verify = False
            self.verify_max = 2; self.verify_gate = False; self.require_assert = False

        def run(self, task_id, task, history=None):
            return RunResult(task_id=task_id, harness="collie", model="mock-coder-v1",
                             answer="done", messages=[])

    monkeypatch.setattr(cli, "make_harness", lambda *a, **kw: FakeHarness())
    assert cli.cmd_run(_args(cwd=str(tmp_path))) == 0

    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["decision"]["runner"]["runner"] == "collie"
    assert payload["decision"]["runner"]["source"] == "configured"
    assert payload["runner"] is None       # no external worker, no worker receipt
    # And the synthesized probe says outright that nobody inspected a host.
    assert "in-process" in payload["decision"]["runner"]["probe"]["detail"]


def test_run_pinned_model_travels_to_the_same_vendor(monkeypatch, tmp_path, capsys):
    """An explicit --model is the user's instruction and reaches the worker verbatim."""
    _pin_router(monkeypatch, _decision(provider="codex-oauth", model="gpt-5.6-sol"))
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe("codex-exec")})
    seen: dict = {}
    _fake_run_adhoc(monkeypatch, _worker_result("codex-exec"), seen)

    assert cli.cmd_run(_args(cwd=str(tmp_path), runner="codex-exec")) == 0
    capsys.readouterr()
    # Same credential family as the router's provider, so the routed model travels.
    assert seen["model"] == "gpt-5.6-sol"


def test_run_external_stream_json_emitter_reaches_the_slice(monkeypatch, tmp_path,
                                                            capsys):
    """--stream-json replaces the shim's no-op emitter, and the slice gets that one.

    Without this the external path would accept the flag and stream nothing, which
    is worse than refusing it: an editor extension would sit on an empty pipe.
    """
    _pin_router(monkeypatch, _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe("codex-exec")})
    seen: dict = {}

    def run_adhoc(decision, task, workspace, **kwargs):
        seen.update(kwargs)
        kwargs["emit"]("runner", {"event": "native", "type": "turn.completed"})
        return _worker_result("codex-exec")
    monkeypatch.setattr(runner_slice, "run_adhoc", run_adhoc)

    assert cli.cmd_run(_args(cwd=str(tmp_path), runner="codex-exec",
                             json=False, stream_json=True)) == 0
    captured = capsys.readouterr()
    frames = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    assert frames[0]["type"] == "decision"
    assert frames[0]["runner"]["runner"] == "codex-exec"
    assert any(f["type"] == "runner" and f.get("event") == "native" for f in frames)
    # stdout stays the single JSON object a --json consumer pipes.
    assert json.loads(captured.out.strip())["runner"]["runner"] == "codex-exec"


def test_run_resume_continues_the_workers_own_thread(monkeypatch, tmp_path, capsys):
    """--resume hands back the locator THAT worker minted, and skips the recap.

    Collie's transcript is not the worker's conversation, so replaying it into a
    thread the worker already remembers would repeat the whole session at it. A
    locator from a different worker is ignored for the same reason: it names a
    conversation that, over there, never happened.
    """
    from harness import sessions

    _pin_router(monkeypatch, _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe("codex-exec")})
    seen: dict = {}
    _fake_run_adhoc(monkeypatch, _worker_result("codex-exec"), seen)

    sid = "20260822-000000-aaaa"
    sessions.append_run_receipt(sid, {"runner": _receipt("claude-code", "cc_1").to_dict()})
    sessions.append_run_receipt(sid, {"runner": _receipt("codex-exec", "th_prev").to_dict()})

    assert cli.cmd_run(_args(cwd=str(tmp_path), runner="codex-exec", resume=sid)) == 0
    capsys.readouterr()
    assert seen["resume_from"]["locator"] == "th_prev"
    assert seen["history_note"] is None

    # The same session on a worker that never saw it starts fresh instead.
    seen.clear()
    _fake_run_adhoc(monkeypatch, _worker_result("codex-exec"), seen)
    sessions.append_run_receipt(sid, {"runner": _receipt("claude-code", "cc_2").to_dict()})
    other = "20260822-000000-bbbb"
    sessions.append_run_receipt(other, {"decision": {}, "model": ""})
    assert cli.cmd_run(_args(cwd=str(tmp_path), runner="codex-exec", resume=other)) == 0
    capsys.readouterr()
    assert seen["resume_from"] is None


def test_run_external_bad_workspace_is_an_error_not_a_traceback(monkeypatch, tmp_path,
                                                                capsys):
    """The slice refuses a workspace that is not a directory; the CLI has to say so."""
    _pin_router(monkeypatch, _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe("codex-exec")})

    def missing(*a, **kw):
        raise ValueError("workspace does not exist or is not a directory: /nope")
    monkeypatch.setattr(runner_slice, "run_adhoc", missing)

    assert cli.cmd_run(_args(cwd=str(tmp_path / "nope"), runner="codex-exec")) == 2
    assert "cannot run on codex-exec" in capsys.readouterr().err


def test_run_external_human_output_survives_unknown_usage(monkeypatch, tmp_path,
                                                          capsys):
    """The plain-text summary names the worker and prints `?` where nobody measured.

    `%d` against the None a worker leaves for unreported usage would end the run
    with a TypeError after the work was already done and billed.
    """
    _pin_router(monkeypatch, _decision())
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe("codex-exec")})
    _fake_run_adhoc(monkeypatch, _worker_result("codex-exec"), {})

    assert cli.cmd_run(_args(cwd=str(tmp_path), runner="codex-exec", json=False)) == 0
    out = capsys.readouterr().out
    assert "worker=%s" % runner_registry.SPECS["codex-exec"].label in out
    assert "in=? out=?" in out and "turns=1" in out
    assert "renamed it" in out


# --- collie runners ---------------------------------------------------------
def test_runners_list_json(capsys):
    """The table lists every declared key, including the ones a later phase owns."""
    assert cli.cmd_runners(argparse.Namespace(
        action="list", key="", live=False, json=True, runners="", docker=False,
        report="")) == 0
    payload = json.loads(capsys.readouterr().out.strip())

    keys = [row["key"] for row in payload["runners"]]
    assert keys == list(runner_registry.SPECS)
    assert payload["live"] is False
    # A phase-2 key is visible and unselectable: `collie runners` is the inventory,
    # `--runner` is the menu, and they are deliberately not the same list.
    later = [row for row in payload["runners"] if row["phase"] > payload["phase"]]
    assert later and all(row["key"] not in runner_registry.option_keys()
                         for row in later)
    assert all(row["probe"]["key"] == row["key"] for row in payload["runners"])


def test_runners_probe_json(monkeypatch, capsys):
    """`probe KEY` answers about one runner, and an unknown key is an error not silence."""
    monkeypatch.setattr(runner_registry, "probe_all",
                        lambda keys=None, **kw: {"codex-exec": _probe("codex-exec")})
    base = dict(action="probe", live=False, json=True, runners="", docker=False,
                report="")
    assert cli.cmd_runners(argparse.Namespace(key="codex-exec", **base)) == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert [p["key"] for p in payload["probes"]] == ["codex-exec"]
    assert payload["probes"][0]["usable"] is True

    # probe_all() skips keys it does not know, which would print an empty, cheerful
    # list for a typo. The single-key question has to fail instead.
    assert cli.cmd_runners(argparse.Namespace(key="codex-exex", **base)) == 2
    assert "unknown runner" in capsys.readouterr().err


def test_runners_compat_writes_report(tmp_path, capsys):
    """compat --report writes both halves: the JSON the registry reads, the md a person does."""
    target = tmp_path / "reports" / "runner-compat"
    code = cli.cmd_runners(argparse.Namespace(
        action="compat", key="", live=False, json=False, runners="collie",
        docker=False, report=str(target)))
    out = capsys.readouterr().out
    assert code == 0                                  # no FAIL cell in the matrix

    report = json.loads((tmp_path / "reports" / "runner-compat.json").read_text(
        encoding="utf-8"))
    assert report["schema"] and list(report["runners"]) == ["collie"]
    assert report["live"] is False and report["totals"]["FAIL"] == 0
    assert os.path.exists(str(target) + ".md")
    assert "Runner conformance" in out                # the markdown went to stdout too
    # Not-run is reported as such rather than as a pass.
    assert all(cell["status"] in ("PASS", "SKIP", "UNVERIFIED")
               for cell in report["runners"]["collie"]["checks"].values())


# --- parser -----------------------------------------------------------------
def test_run_parser_offers_only_arrived_runners(tmp_path):
    """`--runner`'s choices come from the registry, so a phase-2 key cannot be typed in.

    Read off the same helper argparse uses rather than parsing a command line: the
    parser is built inside `main()`, which applies saved settings to os.environ as a
    side effect, and a choices assertion is not worth leaking that into the suite.
    """
    offered = cli._runner_option_keys()
    assert "collie" in offered and "codex-exec" in offered
    assert offered == list(runner_registry.option_keys())
    assert "codex-app-server" not in offered          # declared, phase 2, unselectable

    # `auto` is the fourth choice and means "choose from RUNNER_POOL", not a worker.
    request = runner_select.request_from_run(
        argparse.Namespace(runner="auto", web_search=False, mode=None),
        _decision(), settings, cwd=str(tmp_path), has_approver=False)
    assert request.configured == "auto" and request.pin == ""
