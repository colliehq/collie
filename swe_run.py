"""SWE-bench Verified head-to-head: collie vs Claude Code on a small sample.

Prediction phase needs no Docker; EVALUATION needs Docker (Docker Desktop WSL
integration ON for this distro). Usage:
    DEEPSEEK_API_KEY=... .venv/bin/python swe_run.py --n 5
    ... --ids pallets__flask-5014 psf__requests-1234   # explicit instances
    ... --predict-only                                  # skip eval (no Docker yet)
"""
import json
import os
import sys

from harness import swe


def arg(flag, default=None):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else default


def main():
    from datasets import load_dataset
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    if "--ids" in sys.argv:
        # stop at the FIRST following flag — filtering flags but keeping their VALUES turned
        # `--ids X Y --agents collie --model M` into ids=[X, Y, 'collie', 'M'] -> KeyError
        ids = []
        for a in sys.argv[sys.argv.index("--ids") + 1:]:
            if a.startswith("--"):
                break
            ids.append(a)
    else:
        n = int(arg("--n", "5"))
        # lighter repos (avoid django/sympy/scikit-learn giant images); spread across them
        pref = ["pallets/flask", "psf/requests", "pylint-dev/pylint", "sphinx-doc/sphinx",
                "pytest-dev/pytest", "pydata/xarray", "mwaskom/seaborn"]
        by_repo = {}
        for r in ds:
            if r["repo"] in pref:
                by_repo.setdefault(r["repo"], []).append(r["instance_id"])
        picked, i = [], 0
        while len(picked) < n and any(by_repo.values()):     # round-robin across repos
            repo = pref[i % len(pref)]; i += 1
            if by_repo.get(repo):
                picked.append(by_repo[repo].pop(0))
        ids = picked or [r["instance_id"] for r in ds][:n]
    print("instances (%d):" % len(ids), ids, flush=True)

    # --agents collie,claude,hermes,pi,opencode,aider   (default: collie,claude)
    agents = [a.strip() for a in arg("--agents", "collie,claude").split(",") if a.strip()]
    for a in agents:
        if a not in swe.AGENTS:
            print("unknown agent %r (known: %s)" % (a, ", ".join(swe.AGENTS))); return

    # --sub: run every harness on the FLAT Claude Max/Pro SUBSCRIPTION (free-pool) at --model
    # (default claude-sonnet-5). Sets the per-harness env so each draws the subscription via the
    # first-party CLI identity path (lean prompt -> flat pool). aider has NO
    # Claude-Code-identity path and cannot draw the subscription — it is dropped from a --sub run.
    if "--sub" in sys.argv:
        mdl = arg("--model", "claude-sonnet-5")
        os.environ["COLLIE_PROVIDER"] = "anthropic-oauth"; os.environ["COLLIE_MODEL"] = mdl
        os.environ["HERMES_PROVIDER"] = "anthropic"; os.environ["HERMES_MODEL"] = mdl
        os.environ["SWE_PI_PROVIDER"] = "anthropic"; os.environ["SWE_PI_MODEL"] = mdl
        os.environ["SWE_OPENCODE_MODEL"] = "anthropic/" + mdl
        _cli_model = {"claude": "sonnet", "hermes": mdl, "pi": mdl,
                      "opencode": "anthropic/" + mdl}    # collie reads env, not the model arg
        if "aider" in agents:
            print("NOTE: aider has no subscription path — dropping it from this --sub run.")
            agents = [a for a in agents if a != "aider"]
        print("SUBSCRIPTION mode: all harnesses on flat-pool %s. Ensure pi/opencode are logged in "
              "(`pi` /login, `opencode auth login`) — collie/claude/hermes use ~/.claude." % mdl)
    else:
        _cli_model = {}

    for agent in agents:
        print("\n=== PREDICT %s ===" % agent, flush=True)
        swe.build_predictions(ids, agent=agent, out_path="preds/%s.jsonl" % agent,
                              model=_cli_model.get(agent))

    if "--predict-only" in sys.argv:
        print("\npredict-only: skipping eval (enable Docker, then re-run without --predict-only)")
        return

    workers = int(arg("--workers", "2"))    # lower to 1 when the box hosts other RAM-heavy jobs
    for agent in agents:
        print("\n=== EVALUATE %s (max_workers=%d) ===" % (agent, workers), flush=True)
        swe.evaluate("preds/%s.jsonl" % agent, run_id="%s_swe" % agent,
                     instance_ids=ids, max_workers=workers)

    print("\n=== RESOLVE-RATE ===", flush=True)
    for agent in agents:
        rep = "%s.%s_swe.json" % (agent, agent)
        if os.path.exists(rep):
            d = json.load(open(rep))
            print("  %-8s resolved %d/%d" % (
                agent, d.get("resolved_instances", 0), d.get("total_instances", 0)))
        else:
            print("  %-8s (report %s not found)" % (agent, rep))


if __name__ == "__main__":
    main()
