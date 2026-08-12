"""Tiny end-to-end subscription product smoke for coding-agent adapters.

This is intentionally not a capability benchmark.  It gives Collie, Claude Code, and Codex the
same clean one-file repository, keeps the grader outside their workspace, and answers only three
questions: did the official subscription route launch, did the product leave a patch, and does
that patch pass a hidden deterministic contract?  A failure here is an adapter/plumbing blocker;
success merely makes a larger SWE-bench run worth spending quota on.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bench.paired_eval import run_claude, run_codex  # noqa: E402
from bench.subscription_guard import (              # noqa: E402
    SubscriptionGuardError,
    check_subscription_guard,
)
from harness import swe                              # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "agent_smoke"
RESULT = ROOT / "bench" / "results" / "subscription-smoke.json"
TEMP_ROOT = ROOT / ".bench-tmp"
TASK = (
    "Fix clamp.py. clamp(value, lower, upper) must return lower when value is below lower, "
    "upper when it is above upper, and value otherwise. Preserve the ValueError for an inverted "
    "interval. Edit the source code now; do not only describe the fix."
)
_HIDDEN_CHECK = """
from clamp import clamp
assert clamp(-2, 0, 10) == 0
assert clamp(12, 0, 10) == 10
assert clamp(4, 0, 10) == 4
assert clamp(0, 0, 10) == 0
assert clamp(10, 0, 10) == 10
try:
    clamp(1, 2, 0)
except ValueError:
    pass
else:
    raise AssertionError("inverted interval must raise ValueError")
"""


def _git(argv: list[str], cwd: str) -> None:
    result = subprocess.run(["git"] + argv, cwd=cwd, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("git %s failed: %s" % (argv[0], (result.stderr or "")[-300:]))


def prepare_fixture(workdir: str) -> None:
    if not FIXTURE.is_dir():
        raise RuntimeError("smoke fixture missing: %s" % FIXTURE)
    shutil.copytree(FIXTURE, workdir, dirs_exist_ok=True)
    _git(["init", "--quiet"], workdir)
    _git(["config", "user.email", "smoke@collie.run"], workdir)
    _git(["config", "user.name", "Collie Smoke"], workdir)
    _git(["add", "-A"], workdir)
    _git(["commit", "--quiet", "-m", "baseline"], workdir)


def grade(workdir: str) -> dict:
    result = subprocess.run([sys.executable, "-c", _HIDDEN_CHECK], cwd=workdir,
                            capture_output=True, text=True, timeout=30)
    return {"outcome": "graded", "resolved": result.returncode == 0,
            "returncode": result.returncode,
            "detail": ((result.stderr or result.stdout or "").strip())[-500:]}


def run_collie_smoke(workdir: str, model: str, max_turns: int) -> dict:
    old = {name: os.environ.get(name) for name in
           ("COLLIE_DATA_DIR", "COLLIE_SWE_VERIFY", "COLLIE_CODE_SEARCH",
            "COLLIE_MAX_TURNS")}
    state = tempfile.mkdtemp(prefix="collie-product-smoke-state-")
    os.environ.update({"COLLIE_DATA_DIR": os.path.join(state, "data"),
                       "COLLIE_SWE_VERIFY": "0", "COLLIE_CODE_SEARCH": "0",
                       "COLLIE_MAX_TURNS": str(max_turns)})
    started = time.time()
    error, usage = "", {}
    try:
        result = swe.predict_collie(workdir, TASK, provider="claude-cli", model=model,
                                    max_turns=max_turns, benchmark_safe=True)
        error = (getattr(result, "error", "") or "").strip()
        usage = {name: getattr(result, name) for name in
                 ("turns", "input_tokens", "output_tokens", "total_tokens", "cache_read",
                  "cache_creation", "cache_miss_tokens", "cost_usd")
                 if getattr(result, name, None) is not None}
        if "cost_usd" in usage:
            usage["api_equivalent_cost_usd"] = usage.pop("cost_usd")
    except Exception as exc:
        error = "%s: %s" % (type(exc).__name__, exc)
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(state, ignore_errors=True)
    patch = swe.make_patch(workdir)
    return {"harness": "collie", "rep": 1, "secs": round(time.time() - started, 1),
            "patch": patch, "patch_bytes": len(patch), "error": error, "usage": usage}


def _parse_arms(value: str) -> list[str]:
    arms = [item.strip() for item in value.split(",") if item.strip()]
    if not arms or len(arms) != len(set(arms)) or not set(arms) <= {
            "collie", "claude", "codex"}:
        raise ValueError("--arms must contain unique values from collie,claude,codex")
    return arms


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="subscription_smoke")
    parser.add_argument("--arms", default="collie,claude,codex")
    parser.add_argument("--model", default="opus")
    parser.add_argument("--codex-model", default="gpt-5.6-sol")
    parser.add_argument("--max-turns", type=int, default=6)
    parser.add_argument("--codex-credits-remaining", type=float, default=None)
    parser.add_argument("--codex-auto-reload-off", action="store_true")
    parser.add_argument("--codex-evidence-observed-at", default="")
    args = parser.parse_args(argv)
    try:
        arms = _parse_arms(args.arms)
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.max_turns <= 12:
        parser.error("--max-turns must be between 1 and 12")

    receipts = []
    try:
        if set(arms) & {"collie", "claude"}:
            receipts.append(check_subscription_guard("claude-code"))
        if "codex" in arms:
            receipts.append(check_subscription_guard("codex-cli", account_evidence={
                "credits_remaining": args.codex_credits_remaining,
                "auto_reload": False if args.codex_auto_reload_off else None,
                "observed_at_utc": args.codex_evidence_observed_at,
            }))
    except SubscriptionGuardError as exc:
        print(json.dumps(exc.receipt, ensure_ascii=False, indent=1))
        print("refusing to run: " + exc.reason)
        return 2

    instance = {"problem_statement": TASK, "instance_id": "collie-agent-smoke-clamp-v1"}
    rows = []
    # Windows' TEMP can resolve through an 8.3 path (for example SINING~1). Claude Code then sees
    # a different-looking cwd during its permission check and refuses an otherwise allowed edit.
    # Keep disposable fixtures under this repository's canonical long path.
    TEMP_ROOT.mkdir(exist_ok=True)
    try:
        for arm in arms:
            with tempfile.TemporaryDirectory(prefix="collie-product-smoke-repo-",
                                             dir=TEMP_ROOT) as workdir:
                prepare_fixture(workdir)
                if arm == "collie":
                    row = run_collie_smoke(workdir, args.model, args.max_turns)
                elif arm == "claude":
                    row = run_claude(instance, workdir, args.model, 1,
                                     max_turns=args.max_turns)
                else:
                    row = run_codex(instance, workdir, args.codex_model, 1)
                row["grader"] = (grade(workdir) if not row.get("error") else
                                 {"outcome": "not_graded_adapter_error", "resolved": False})
                row["expected_marginal_charge_usd"] = 0
                row["metered_fallback_disabled"] = True
                rows.append(row)
                print("%-7s patch=%-5d resolved=%-5s %5.1fs %s" %
                      (arm, row["patch_bytes"], row["grader"]["resolved"], row["secs"],
                       (row.get("error") or "")[:90]), flush=True)
    finally:
        try:
            TEMP_ROOT.rmdir()
        except OSError:
            pass

    payload = {
        "schema_version": 1,
        "scope": "adapter_smoke",
        "track": "subscription_native_product",
        "claim": "plumbing_only",
        "publishable": False,
        "task": instance["instance_id"],
        "models": {"collie": args.model, "claude": args.model,
                   "codex": args.codex_model},
        "subscription_guard_receipts": receipts,
        "billing": {"expected_marginal_charge_usd": 0,
                    "metered_fallback_disabled": True,
                    "api_equivalent_cost_is_not_a_charge": True},
        "rows": rows,
    }
    RESULT.parent.mkdir(parents=True, exist_ok=True)
    RESULT.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print("written to %s" % RESULT)
    return 1 if any(row.get("error") or not row["grader"]["resolved"] for row in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
