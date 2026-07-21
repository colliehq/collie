"""Executed-repro ORACLE for best-of-2 candidate selection (the executed-oracle selector, now with a
valid premise: assert-verify puts correct patches into the distribution on variance instances).

Per instance, given base_patch and assert_patch:
  1. build the SWE-bench instance container (repo @ base_commit + deps),
  2. author a repro.py (Opus) that reproduces the issue and ASSERTS the correct post-fix result,
  3. validate it FAILS on the unfixed base (else it's a useless oracle -> tie),
  4. run each candidate against it (git apply -> run repro -> reset),
  5. select: exactly-one-pass -> that one; both-pass or both-fail (TIE) -> PREFER BASE.

Prefer-base-on-tie makes best-of-2 structurally unable to regress vs baseline (a weak/invalid
oracle degrades to "always base", never worse). Uses the flat subscription (Opus) for repro
authoring; Docker for execution. No gold-test access.

  python -m bench.exec_select --instance astropy__astropy-14369 \
      --base preds/fresh_collie_base.jsonl --assert preds/fresh_collie_assert.jsonl
"""
import argparse, json, logging, os, re, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.disable(logging.INFO)
_log = logging.getLogger("exec_select"); _log.addHandler(logging.NullHandler())


def _load(path):
    return {json.loads(l)["instance_id"]: json.loads(l).get("model_patch", "") for l in open(path)}


def _author_repro(problem_statement, model="claude-opus-4-8"):
    """One-shot Opus (flat subscription) -> a standalone repro.py that asserts correct behavior."""
    from harness.providers import make_provider
    prov = make_provider("anthropic-oauth", model)
    sysmsg = (
        "You write a MINIMAL standalone Python reproduction script for a GitHub issue in a repo "
        "checked out+installed at /testbed. CRITICAL: the script must run to completion with NO "
        "SETUP/IMPORT ERROR — exit 0 iff the bug is FIXED, exit 1 (AssertionError) iff still buggy. "
        "A setup crash (ModuleNotFoundError, misconfig) makes it useless. Rules: (1) do NOT invent "
        "fake modules/apps (never add a 'test_app' to INSTALLED_APPS). For Django, call "
        "settings.configure(INSTALLED_APPS=['django.contrib.contenttypes','django.contrib.auth']) and "
        "define any model with `class Meta: app_label='contenttypes'`, or use django.test.utils."
        "isolate_apps — models must belong to an INSTALLED app. For libs (numpy/sympy/etc.) just "
        "import and call. (2) construct the smallest scenario from the issue, compute the actual "
        "result, then `assert actual == expected, (actual, expected)` with `expected` = the CORRECT "
        "post-fix behavior the issue states/implies. (3) It must FAIL on the current buggy code and "
        "PASS once fixed. No pytest, no network, no CLI args, no file writes. Wrap risky setup so an "
        "import/config error is impossible. Output ONLY python in one ```python block.")
    user = "ISSUE:\n" + problem_statement[:6000] + "\n\nWrite repro.py:"
    try:
        comp = prov.complete(sysmsg, [{"role": "user", "content": user}], [])
        txt = comp.text or ""
    except Exception as e:
        return None
    m = re.search(r"```(?:python)?\s*(.*?)```", txt, re.S)
    code = (m.group(1) if m else txt).strip()
    return code or None


def _exec(container, cmd, timeout=120):
    from swebench.harness.docker_utils import exec_run_with_timeout
    out, timed, _ = exec_run_with_timeout(container, "/bin/bash -lc " + json_quote(cmd), timeout)
    return out if isinstance(out, str) else out.decode("utf-8", "ignore")


def json_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _b64(s):
    import base64
    return base64.b64encode(s.encode()).decode()


def _run_candidate(container, patch, repro_text):
    """git reset -> apply patch -> write repro (base64, robust) -> run -> reset. True if repro PASSED."""
    _exec(container, "cd /testbed && git checkout -- . && git clean -fdq")
    if patch.strip():
        _exec(container, "cd /testbed && printf %%s '%s' | base64 -d > /tmp/cand.diff && "
              "(git apply --whitespace=nowarn /tmp/cand.diff 2>&1 || "
              "git apply --3way --whitespace=nowarn /tmp/cand.diff 2>&1)" % _b64(patch))
    _exec(container, "printf %%s '%s' | base64 -d > /tmp/repro_oracle.py" % _b64(repro_text))
    out = _exec(container, "cd /testbed && python /tmp/repro_oracle.py 2>&1; echo EXIT=$?")
    _exec(container, "cd /testbed && git checkout -- . && git clean -fdq")
    m = re.search(r"EXIT=(\d+)\s*$", out)
    return (m and m.group(1) == "0"), out


def select_instance(iid, base_patch, assert_patch, model="claude-opus-4-8"):
    import docker
    from pathlib import Path, PurePosixPath
    from swebench.harness.test_spec.test_spec import make_test_spec
    from swebench.harness.docker_build import (build_container, build_env_images,
                                               setup_logger, close_logger)
    from swebench.harness.docker_utils import cleanup_container, write_to_container
    from datasets import load_dataset
    inst = next(r for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
                if r["instance_id"] == iid)
    spec = make_test_spec(inst, base_image_tag="latest", env_image_tag="latest",
                          instance_image_tag="latest")
    client = docker.from_env()
    log_dir = Path("logs/execsel"); log_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(iid, log_dir / ("%s.log" % iid.replace("/", "__")))
    # ensure base+env images exist (they were pruned after eval) before build_container
    build_env_images(client, [inst], force_rebuild=False, max_workers=2,
                     env_image_tag="latest", instance_image_tag="latest")
    container = None
    result = {"instance_id": iid, "decision": "base", "reason": "", "base_pass": None, "assert_pass": None}
    try:
        container = build_container(spec, client, "execsel", logger, nocache=False, force_rebuild=False)
        container.start()
        repro = _author_repro(inst["problem_statement"], model)
        if not repro:
            result["reason"] = "repro-author-failed -> tie -> base"; return result
        # persist the repro + outputs for diagnosis
        dbg = Path("logs/execsel"); dbg.mkdir(parents=True, exist_ok=True)
        open(dbg / ("%s.repro.py" % iid.replace("/", "__")), "w").write(repro)
        # validate: repro must FAIL on unfixed base (else useless oracle)
        base_clean, out0 = _run_candidate(container, "", repro)
        open(dbg / ("%s.unfixed.out" % iid.replace("/", "__")), "w").write(out0)
        if base_clean:
            result["reason"] = "repro passes on UNFIXED base (useless oracle) -> tie -> base"; return result
        bp, obp = _run_candidate(container, base_patch, repro)
        ap, oap = _run_candidate(container, assert_patch, repro)
        open(dbg / ("%s.assert.out" % iid.replace("/", "__")), "w").write(oap)
        open(dbg / ("%s.base.out" % iid.replace("/", "__")), "w").write(obp)
        result["base_pass"], result["assert_pass"] = bp, ap
        if ap and not bp:
            result["decision"] = "assert"; result["reason"] = "only assert passes repro"
        elif bp and not ap:
            result["decision"] = "base"; result["reason"] = "only base passes repro"
        else:
            result["decision"] = "base"; result["reason"] = "tie (both %s) -> prefer base" % ("pass" if bp else "fail")
    except Exception as e:
        result["reason"] = "ERROR %s -> base" % str(e)[:120]
    finally:
        if container is not None:
            try: cleanup_container(client, container, logger)
            except Exception: pass
        try: close_logger(logger)
        except Exception: pass
    return result


def select_n(iid, candidates, model="claude-opus-4-8"):
    """best-of-N: author a repro, validate it FAILS on unfixed base, then run EACH candidate;
    pick the FIRST candidate whose repro PASSES. If none pass (weak/invalid repro) -> index 0."""
    import docker
    from pathlib import Path
    from swebench.harness.test_spec.test_spec import make_test_spec
    from swebench.harness.docker_build import (build_container, build_env_images,
                                               setup_logger, close_logger)
    from swebench.harness.docker_utils import cleanup_container
    from datasets import load_dataset
    inst = next(r for r in load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
                if r["instance_id"] == iid)
    spec = make_test_spec(inst, base_image_tag="latest", env_image_tag="latest",
                          instance_image_tag="latest")
    client = docker.from_env()
    log_dir = Path("logs/execsel"); log_dir.mkdir(parents=True, exist_ok=True)
    logger = setup_logger(iid, log_dir / ("%s.n.log" % iid.replace("/", "__")))
    build_env_images(client, [inst], force_rebuild=False, max_workers=2,
                     env_image_tag="latest", instance_image_tag="latest")
    container = None
    res = {"instance_id": iid, "pick": 0, "passes": [], "reason": ""}
    try:
        container = build_container(spec, client, "execseln", logger, nocache=False, force_rebuild=False)
        container.start()
        repro = _author_repro(inst["problem_statement"], model)
        if not repro:
            res["reason"] = "repro-author-failed -> pick 0"; return res
        base_clean, _ = _run_candidate(container, "", repro)
        if base_clean:
            res["reason"] = "repro passes on unfixed base (useless) -> pick 0"; return res
        passes = []
        for i, c in enumerate(candidates):
            ok, _ = _run_candidate(container, c or "", repro)
            passes.append(bool(ok))
        res["passes"] = passes
        pick = next((i for i, ok in enumerate(passes) if ok), 0)
        res["pick"] = pick
        res["reason"] = ("candidate %d passes repro" % pick) if any(passes) else "none pass -> pick 0"
    except Exception as e:
        res["reason"] = "ERROR %s -> pick 0" % str(e)[:120]
    finally:
        if container is not None:
            try: cleanup_container(client, container, logger)
            except Exception: pass
        try: close_logger(logger)
        except Exception: pass
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance"); ap.add_argument("--ids")
    ap.add_argument("--base", required=True); ap.add_argument("--assert", dest="asrt", required=True)
    ap.add_argument("--model", default="claude-opus-4-8")
    ap.add_argument("--out", default="preds/bestof2.jsonl")
    a = ap.parse_args()
    base, asrt = _load(a.base), _load(a.asrt)
    ids = [a.instance] if a.instance else [l.strip() for l in open(a.ids) if l.strip()]
    fout = open(a.out, "a")
    for iid in ids:
        r = select_instance(iid, base.get(iid, ""), asrt.get(iid, ""), a.model)
        chosen = asrt.get(iid, "") if r["decision"] == "assert" else base.get(iid, "")
        print("[%s] -> %s  (%s)  base_pass=%s assert_pass=%s" % (
            iid.split("__")[-1], r["decision"].upper(), r["reason"], r["base_pass"], r["assert_pass"]), flush=True)
        fout.write(json.dumps({"instance_id": iid, "model_name_or_path": "collie_bestof2",
                               "model_patch": chosen, "_decision": r}) + "\n"); fout.flush()
    fout.close()


if __name__ == "__main__":
    main()
