"""pack — best-of-N with EXECUTION-BASED selection.

collie's thesis is "don't trust the model's claim, run the code." Pack mode applies that to candidate
selection: run the task N independent times in isolated copies of the working tree, then pick the
winner by what actually PASSES — an optional check command (exit 0 = pass), then the harness's own
verification verdict (edited + a repro ran green), then a cheap quality tiebreak. Only the winning
tree is (optionally) copied back. If a check is given and NOTHING passes it, pack refuses to apply a
losing attempt — a no-op beats shipping a wrong edit.

CLI:  collie pack "task" -n 3 --check "python -m pytest -q" [--apply]
"""
import os
import shutil
import subprocess
import tempfile

_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
         ".pytest_cache", ".collie", "dist", "build", ".tox"}


def _ignore(_dir, names):
    return [n for n in names if n in _SKIP]


def _isolate(cwd):
    """A throwaway copy of the working tree (heavy/vcs dirs excluded) for one attempt."""
    dst = tempfile.mkdtemp(prefix="collie_pack_")
    shutil.copytree(cwd, os.path.join(dst, "w"), ignore=_ignore, symlinks=True,
                    dirs_exist_ok=True)
    return os.path.join(dst, "w")


def _run_check(cmd, cwd, timeout=300):
    from . import plat
    _cmdargs, _use_shell = plat.shell_argv(cmd)              # POSIX predicate on every OS
    try:
        p = subprocess.run(_cmdargs, shell=_use_shell, cwd=cwd, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return p.returncode == 0, (p.stdout or "")[-2000:]
    except subprocess.TimeoutExpired:
        return False, "(check timed out after %ds)" % timeout
    except Exception as e:
        return False, "(check failed to run: %s)" % e


def select(attempts, have_check):
    """Pure selection over attempts (list of dicts with keys: check_pass bool|None, verified bool,
    answer str, turns int, error str, idx int). Returns (winner_idx or None, reason).

    Order of preference:
      1. if a check was given: only check-passing attempts are eligible; if none pass -> no winner.
      2. among eligible: prefer verified (repro ran green), then a real answer, then fewer turns.
    """
    pool = attempts
    if have_check:
        passing = [a for a in attempts if a.get("check_pass")]
        if not passing:
            return None, "no attempt passed the check command"
        pool = passing

    def key(a):
        return (
            0 if a.get("verified") else 1,                         # verified first
            0 if (a.get("answer") or "").strip() and not a.get("error") else 1,  # real answer
            a.get("turns", 10**6),                                 # cheaper run
            a.get("idx", 0),                                       # deterministic tiebreak
        )
    best = min(pool, key=key)
    why = []
    if have_check:
        why.append("passed check")
    if best.get("verified"):
        why.append("verified (repro green)")
    why.append("%d turns" % best.get("turns", 0))
    return best["idx"], ", ".join(why)


def _copy_back(src, dst):
    """Copy the winning tree back over the real cwd (heavy/vcs dirs skipped). Opt-in (--apply)."""
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_root, exist_ok=True)
        for f in files:
            try:
                shutil.copy2(os.path.join(root, f), os.path.join(target_root, f))
            except OSError:
                pass


def run_pack(task, cwd, n=3, check=None, provider=None, model=None,
             apply=False, emit=None):
    """Run N isolated attempts, select the winner by execution, optionally apply it back."""
    from .cli import make_harness
    from . import settings
    provider = provider or settings.get("PROVIDER", "anthropic")   # env > settings.json > API default
    n = max(1, min(8, int(n)))
    have_check = bool(check)
    attempts, dirs = [], []
    for i in range(n):
        iso = _isolate(cwd)
        dirs.append(iso)
        rec = {"idx": i, "dir": iso}
        try:
            h = make_harness(iso, provider=provider, model=model, project="pack",
                             code_search=True, exec_code=True)
            res = h.run("pack%d" % i, task)
            rec.update(answer=res.answer or "", verified=bool(getattr(res, "verified", False)),
                       turns=res.turns, error=res.error or "", cost_usd=res.cost_usd)
            try:
                h.memory.close(); h.recorder.close()
            except Exception:
                pass
        except Exception as e:
            rec.update(answer="", verified=False, turns=0, error="%s: %s" % (type(e).__name__, e),
                       cost_usd=0.0)
        if have_check:
            ok, tail = _run_check(check, iso)
            rec["check_pass"] = ok
            rec["check_tail"] = tail
        if emit:
            emit(i, rec)
        attempts.append(rec)

    winner_idx, reason = select(attempts, have_check)
    applied = False
    if apply and winner_idx is not None:
        _copy_back(dirs[winner_idx], cwd)
        applied = True

    result = {"n": n, "winner": winner_idx, "reason": reason, "applied": applied,
              "attempts": [{k: v for k, v in a.items() if k not in ("dir", "check_tail")}
                           for a in attempts],
              "total_cost_usd": round(sum(a.get("cost_usd", 0.0) for a in attempts), 4)}
    if winner_idx is not None:
        result["answer"] = attempts[winner_idx].get("answer", "")
    # clean the throwaway trees
    for d in dirs:
        shutil.rmtree(os.path.dirname(d), ignore_errors=True)
    return result
