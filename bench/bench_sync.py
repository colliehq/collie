"""bench_sync — cross-device resumable SWE runs via a private git repo.

The expensive artifact is PREDICTIONS: each is one Opus run. Resume already works from the
per-instance shard files (harness.swe writes preds/<run_id>/<instance>.json). This script syncs
those shards (+ eval results, ids, meta) through a private repo so a run paused on one machine
continues on another: `pull` on the new box, run, `push` when you pause.

Layout of the private repo (default remote: git@github.com:<you>/collie-bench, or COLLIE_BENCH_REMOTE):
    preds/<run_id>/<instance>.json     prediction shards (conflict-free: per-instance files)
    results/<run_id>/<instance>.json   eval verdicts (report.json content)
    ids/<name>.txt                     instance lists
    meta/<run_id>.json                 config: model/provider/harness_sha/host/updated
    README.md                          onboarding

Commands:
    python -m bench.bench_sync init      # clone the private repo to the local cache (once/device)
    python -m bench.bench_sync push      # local shards+results+ids -> repo -> git push
    python -m bench.bench_sync pull       # git pull -> repo shards -> local preds/ + logs/
    python -m bench.bench_sync status     # what's here vs there, per run_id

The snap `gh` on this box can't spawn git for clone (see memory env-snap-gh-broken), so we clone
with plain git + gh's credential helper. Set COLLIE_BENCH_REMOTE to override the default remote.
"""
import argparse
import glob
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCH_LOCAL = os.environ.get("COLLIE_BENCH_LOCAL", os.path.expanduser("~/collie-bench"))
# No hardcoded owner: point COLLIE_BENCH_REMOTE at your own private bench repo.
# The placeholder <you> is intentionally non-resolvable so an unconfigured run fails loudly.
DEFAULT_REMOTE = "https://github.com/<you>/collie-bench.git"
REMOTE = os.environ.get("COLLIE_BENCH_REMOTE", DEFAULT_REMOTE)
CRED_HELPER = "!gh auth git-credential"   # snap-gh workaround: plain git + gh credentials


def _run(args, cwd=None, check=True, capture=False):
    return subprocess.run(args, cwd=cwd, check=check, text=True,
                          capture_output=capture, timeout=300)


def _git(args, cwd, **kw):
    return _run(["git", "-c", "credential.helper=" + CRED_HELPER] + args, cwd=cwd, **kw)


def cmd_init(_):
    if os.path.isdir(os.path.join(BENCH_LOCAL, ".git")):
        print("already cloned at", BENCH_LOCAL); return 0
    os.makedirs(os.path.dirname(BENCH_LOCAL), exist_ok=True)
    print("cloning %s -> %s" % (REMOTE, BENCH_LOCAL))
    _git(["clone", REMOTE, BENCH_LOCAL], cwd=os.path.dirname(BENCH_LOCAL))
    _git(["config", "credential.helper", CRED_HELPER], cwd=BENCH_LOCAL)
    for d in ("preds", "results", "ids", "meta"):
        os.makedirs(os.path.join(BENCH_LOCAL, d), exist_ok=True)
    print("ready.")
    return 0


def _ensure_local():
    if not os.path.isdir(os.path.join(BENCH_LOCAL, ".git")):
        print("bench repo not cloned — run `python -m bench.bench_sync init` first", file=sys.stderr)
        sys.exit(2)


def _run_ids_from_preds():
    """run_ids that have a local prediction shard dir under preds/."""
    return sorted(os.path.basename(d) for d in glob.glob(os.path.join(ROOT, "preds", "*"))
                  if os.path.isdir(d))


def _copy_shards_to_repo():
    """local preds/<run>/*.json -> repo preds/<run>/ ; eval report.json -> repo results/<run>/."""
    n_pred = n_res = 0
    for run_id in _run_ids_from_preds():
        src = os.path.join(ROOT, "preds", run_id)
        dst = os.path.join(BENCH_LOCAL, "preds", run_id); os.makedirs(dst, exist_ok=True)
        for fn in os.listdir(src):
            if fn.endswith(".json"):
                _cp(os.path.join(src, fn), os.path.join(dst, fn)); n_pred += 1
    # eval verdicts: logs/run_evaluation/<run>/<agent>/<inst>/report.json -> results/<run>/<inst>.json
    for rep in glob.glob(os.path.join(ROOT, "logs", "run_evaluation", "*", "*", "*", "report.json")):
        parts = rep.split(os.sep)
        run_id, inst = parts[-4], parts[-2]
        dst = os.path.join(BENCH_LOCAL, "results", run_id); os.makedirs(dst, exist_ok=True)
        _cp(rep, os.path.join(dst, inst + ".json")); n_res += 1
    # ids lists
    for ids in glob.glob(os.path.join(ROOT, "data", "ids*.txt")):
        _cp(ids, os.path.join(BENCH_LOCAL, "ids", os.path.basename(ids)))
    return n_pred, n_res


def _write_meta():
    """Per-run meta from the shard _meta blocks — model/provider/harness_sha/host, and counts."""
    for run_id in sorted(os.path.basename(d) for d in
                         glob.glob(os.path.join(BENCH_LOCAL, "preds", "*")) if os.path.isdir(d)):
        shards = glob.glob(os.path.join(BENCH_LOCAL, "preds", run_id, "*.json"))
        meta = {"run_id": run_id, "n_predictions": len(shards)}
        for s in shards[:1]:
            m = (json.load(open(s)).get("_meta") or {})
            meta.update({k: m.get(k) for k in ("harness_sha", "provider", "model")})
        shas = set()
        for s in shards:
            m = json.load(open(s)).get("_meta") or {}
            if m.get("harness_sha"): shas.add(m["harness_sha"])
        meta["harness_shas"] = sorted(shas)          # >1 => predictions MIXED across harness versions
        n_res = len(glob.glob(os.path.join(BENCH_LOCAL, "results", run_id, "*.json")))
        meta["n_results"] = n_res
        json.dump(meta, open(os.path.join(BENCH_LOCAL, "meta", run_id + ".json"), "w"), indent=2)


def _cp(a, b):
    with open(a, "rb") as f: data = f.read()
    tmp = b + ".tmp"
    with open(tmp, "wb") as f: f.write(data)
    os.replace(tmp, b)


def cmd_push(_):
    _ensure_local()
    _git(["pull", "--quiet", "--no-edit"], cwd=BENCH_LOCAL, check=False)   # merge others first
    n_pred, n_res = _copy_shards_to_repo()
    _write_meta()
    _git(["add", "-A"], cwd=BENCH_LOCAL)
    st = _git(["status", "--porcelain"], cwd=BENCH_LOCAL, capture=True).stdout.strip()
    if not st:
        print("nothing new to push (%d preds, %d results already synced)" % (n_pred, n_res)); return 0
    import socket
    _git(["commit", "-q", "-m", "sync from %s: %d preds, %d results" %
          (socket.gethostname(), n_pred, n_res)], cwd=BENCH_LOCAL)
    _git(["push", "--quiet"], cwd=BENCH_LOCAL)
    print("pushed: %d prediction shards, %d result shards" % (n_pred, n_res))
    return 0


def cmd_pull(_):
    _ensure_local()
    _git(["pull", "--quiet", "--no-edit"], cwd=BENCH_LOCAL)
    n_pred = n_res = 0
    for run_dir in glob.glob(os.path.join(BENCH_LOCAL, "preds", "*")):
        if not os.path.isdir(run_dir): continue
        run_id = os.path.basename(run_dir)
        dst = os.path.join(ROOT, "preds", run_id); os.makedirs(dst, exist_ok=True)
        for fn in os.listdir(run_dir):
            if fn.endswith(".json"):
                _cp(os.path.join(run_dir, fn), os.path.join(dst, fn)); n_pred += 1
    # results -> reconstruct logs tree so eval-resume + stats see prior verdicts (agent = collie)
    for run_dir in glob.glob(os.path.join(BENCH_LOCAL, "results", "*")):
        if not os.path.isdir(run_dir): continue
        run_id = os.path.basename(run_dir)
        for fn in os.listdir(run_dir):
            if not fn.endswith(".json"): continue
            inst = fn[:-5]
            dst = os.path.join(ROOT, "logs", "run_evaluation", run_id, "collie", inst)
            os.makedirs(dst, exist_ok=True)
            _cp(os.path.join(run_dir, fn), os.path.join(dst, "report.json")); n_res += 1
    # ids
    for ids in glob.glob(os.path.join(BENCH_LOCAL, "ids", "*.txt")):
        _cp(ids, os.path.join(ROOT, "data", os.path.basename(ids)))
    # rebuild the jsonl views so eval can run immediately
    from harness import swe
    for run_dir in glob.glob(os.path.join(ROOT, "preds", "*")):
        if os.path.isdir(run_dir):
            swe.assemble_jsonl(run_dir + ".jsonl")
    print("pulled: %d prediction shards, %d result shards (jsonl views rebuilt)" % (n_pred, n_res))
    return 0


def cmd_status(_):
    _ensure_local()
    _git(["fetch", "--quiet"], cwd=BENCH_LOCAL, check=False)
    print("bench repo: %s" % BENCH_LOCAL)
    metas = glob.glob(os.path.join(BENCH_LOCAL, "meta", "*.json"))
    if not metas:
        print("  (no runs synced yet)")
    for m in sorted(metas):
        d = json.load(open(m))
        warn = "  ⚠ MIXED harness versions" if len(d.get("harness_shas", [])) > 1 else ""
        print("  %-28s preds=%-3d results=%-3d model=%s sha=%s%s" % (
            d["run_id"], d.get("n_predictions", 0), d.get("n_results", 0),
            d.get("model"), ",".join(d.get("harness_shas", [])), warn))
    return 0


def main():
    ap = argparse.ArgumentParser(prog="bench_sync")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in [("init", cmd_init), ("push", cmd_push), ("pull", cmd_pull), ("status", cmd_status)]:
        sub.add_parser(name).set_defaults(fn=fn)
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
