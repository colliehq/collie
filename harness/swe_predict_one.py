"""Predict ONE SWE-bench instance in a fresh process, then exit.

Run as: python -m harness.swe_predict_one <spec.json> <out.json>

Why a subprocess per instance: the agents load ONNX embedders (jina-v3 ~2GB,
bge-small) whose C++ arena memory is NOT returned to the OS by Python gc. Looping
over instances in one process accumulated ~30GB and risked OOM. Isolating each
instance means peak memory = one instance (~3-4GB) and the OS reclaims everything
on exit. The parent (swe.build_predictions) reads out.json and appends+flushes.

spec.json: {instance_id, repo, base_commit, problem_statement, agent, provider, model}
out.json:  {instance_id, model_patch}
"""
import json
import subprocess
import sys
import tempfile
import time

from . import plat, swe


def main():
    spec = json.load(open(sys.argv[1], encoding="utf-8"))
    out_json = sys.argv[2]
    wd = tempfile.mkdtemp(prefix="swe_%s_" % spec["instance_id"].replace("/", "_"))
    patch, ok = "", False
    # Per-instance metrics for EVERY agent (requirement: all harness comparisons report
    # resolve + tokens + wall + turns). wall_s is timed here (universal + reliable); collie's
    # tokens/turns come from its RunResult; CLI agents (hermes/cc) fill tokens/turns best-effort.
    metrics = {"agent": spec.get("agent"), "wall_s": 0.0, "tokens": 0, "turns": 0}
    _t0 = time.time()
    # Real-env verification (COLLIE_E2E=1): expose run_in_env against THIS instance's installed
    # container so the model can actually run its repro (the bare checkout has no deps). The image
    # travels in the spec so each instance gets its own; no-op unless COLLIE_E2E is set.
    import os as _os
    if _os.environ.get("COLLIE_E2E") in ("1", "true", "on") and spec.get("docker_image"):
        _os.environ["COLLIE_E2E_IMAGE"] = spec["docker_image"]
    try:
        swe.prepare_repo(spec["repo"], spec["base_commit"], wd)
        agent = spec["agent"]
        if agent == "collie":
            res = swe.predict_collie(wd, spec["problem_statement"],
                                     provider=spec.get("provider", "deepseek"),
                                     model=spec.get("model"))
            # A totally-failed run (provider/auth/network blip producing ZERO output tokens) yields
            # an empty patch that looks identical to a genuine no-edit answer. Committing it freezes
            # the instance as a permanent score-0 that never retries (rebench 2026_03: one ~5-min
            # API blip silently killed 6 instances, 3 of them actually solvable). Treat "no API turn
            # ever succeeded" as a transient failure -> not-completed -> retried next run. A genuine
            # explored-but-didn't-edit run has many output tokens, so it is NOT caught here.
            if res is not None and getattr(res, "output_tokens", 1) == 0:
                raise RuntimeError("no successful API turn (output_tokens=0) — transient, retry")
            if res is not None:
                metrics["tokens"] = int(getattr(res, "total_tokens", 0)
                                        or getattr(res, "output_tokens", 0) or 0)
                metrics["turns"] = int(getattr(res, "turns", 0) or 0)
        else:
            fn = swe.AGENTS.get(agent)      # hermes MUST run hermes, not fall through to claude
            if fn is None:
                raise ValueError("unknown SWE agent %r" % agent)
            m = fn(wd, spec["problem_statement"], model=(spec.get("model") or ""))
            if isinstance(m, dict):          # CLI agents may return {tokens, turns}
                metrics["tokens"] = int(m.get("tokens", 0) or 0)
                metrics["turns"] = int(m.get("turns", 0) or 0)
        patch = swe.make_patch(wd)
        ok = True                            # ran to completion (an empty patch is a real answer)
    except Exception as e:
        sys.stderr.write("swe_predict_one ERROR %s: %s\n" % (spec["instance_id"], e))
    finally:
        plat.rmtree(wd)
    if not ok:
        # a TRANSIENT failure (clone/API/disk) must NOT leave a success-shaped empty out.json —
        # the parent keys "completed" on os.path.exists(out.json) and would freeze this instance as
        # a permanent score-0 instead of retrying. Write nothing + exit non-zero -> parent retries.
        sys.exit(1)
    metrics["wall_s"] = round(time.time() - _t0, 1)
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"instance_id": spec["instance_id"], "model_patch": patch,
                   "metrics": metrics}, f)


if __name__ == "__main__":
    main()
