"""pack — best-of-N with EXECUTION-BASED selection.

collie's thesis is "don't trust the model's claim, run the code." Pack mode applies that to candidate
selection: run the task N independent times in isolated copies of the working tree, then pick the
winner by what actually PASSES — an optional check command (exit 0 = pass), then the harness's own
verification verdict (edited + a repro ran green), then a cheap quality tiebreak. Only the winning
tree is (optionally) copied back. If a check is given and NOTHING passes it, pack refuses to apply a
losing attempt — a no-op beats shipping a wrong edit.

CLI:  collie pack "task" -n 3 --check "python -m pytest -q" [--apply]
"""
import concurrent.futures
import os
import shutil
import subprocess
import tempfile
import threading

_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
         ".pytest_cache", ".collie", "dist", "build", ".tox"}


def _ignore(_dir, names):
    return [n for n in names if n in _SKIP]


def _isolate(cwd):
    """A throwaway copy of the working tree (heavy/vcs dirs excluded) for one attempt."""
    dst = tempfile.mkdtemp(prefix="collie_pack_")
    try:
        # Return the directory we own. Cleanup can then delete this exact path;
        # deriving a parent from a test double once caused all of %TEMP% to be targeted.
        shutil.copytree(cwd, dst, ignore=_ignore, symlinks=True, dirs_exist_ok=True)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)
        raise
    return dst


def _run_check(cmd, cwd, timeout=300):
    from . import plat
    _cmdargs, _use_shell = plat.shell_argv(cmd)              # POSIX predicate on every OS
    try:
        p = subprocess.run(_cmdargs, shell=_use_shell, cwd=cwd, timeout=timeout,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           **plat.no_window_kwargs())
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


def normalize_roster(roster, provider, model):
    """[(provider, model), …] from a roster of "provider", "provider:model", or pairs.

    maxsplit=1 on purpose — an ollama tag is itself colon-separated ("ollama:qwen2.5-coder:7b").
    An entry that names no model leaves it None so make_provider picks that backend's own default;
    carrying the caller's model across backends would send `deepseek-chat` to Anthropic.
    """
    if not roster:
        return [(provider, model)]
    members = []
    for entry in roster:
        if isinstance(entry, (tuple, list)):
            name, want = (list(entry) + [None])[:2]
        elif ":" in str(entry):
            name, want = str(entry).split(":", 1)
        else:
            name, want = entry, None
        name = str(name or provider or "").strip()
        want = str(want).strip() if want else ""
        members.append((name, want or None))
    return members


def run_pack(task, cwd, n=3, check=None, provider=None, model=None,
             apply=False, emit=None, project="pack", roster=None, parallel=1):
    """Run N isolated attempts, select the winner by execution, optionally apply it back.

    ``roster`` runs the attempts on DIFFERENT backends, assigned round-robin. Selection stays what
    PASSES, never opinion, so a weak member costs tokens and nothing else — it cannot win unless it
    actually passed. That is what makes model diversity safe to add HERE rather than somewhere a
    model would be doing the judging.

    ``parallel`` is the maximum number of attempts in flight. It stays 1 by default: several
    attempts at once on ONE backend is a rate-limit magnet, and a subscription plan is the easiest
    thing to trip. A roster spread across different accounts is the case worth raising it for.
    """
    from .cli import make_harness
    from . import settings
    from .scratch import isolate_harness
    provider = provider or settings.get("PROVIDER", "anthropic")   # env > settings.json > API default
    members = normalize_roster(roster, provider, model)
    n = max(1, min(8, int(n)))
    if roster and len(members) > n:
        # Never silently drop a model someone named: a roster of 4 at n=3 would have looked like a
        # complete comparison while one backend never ran at all.
        n = min(8, len(members))
    parallel = max(1, min(int(parallel or 1), n))
    # Check the backends BEFORE spending attempts on them. An expired subscription token or an
    # unset API key otherwise shows up as N identical failures and a "no attempt passed the
    # check", which reads like the task was hard rather than like nobody was logged in.
    from .catalog import preflight
    blocked = preflight(members)
    if blocked:
        return {"n": n, "winner": None, "reason": "; ".join(blocked), "applied": False,
                "attempts": [], "total_cost_usd": 0.0}
    # Best-of-N is only best-of-N if the N are independent. Attempts used to share one project, so
    # each one's consolidated answer was auto-recalled into the NEXT one's prompt. A per-attempt
    # project separates the undo stacks (keyed by project, and cached in a process-global dict);
    # isolate_harness below then keeps reads on the shared project so they still start level.
    run_tag = "%s-%d" % (project, os.getpid())
    have_check = bool(check)
    # One slot per attempt, filled by the attempt itself. Copying all N trees up front would make
    # a sequential pack wait through N copytrees of the whole repo before the first model call,
    # and would sink every attempt if the last copy failed. Each index is written by exactly one
    # worker, so the list needs no lock.
    dirs = [None] * n
    emit_lock = threading.Lock()

    def _attempt(i):
        member_provider, member_model = members[i % len(members)]
        # Which backend produced which candidate. Without this the winner is anonymous and the one
        # question a mixed roster exists to answer — WHICH model wins, how often — is unanswerable.
        rec = {"idx": i, "provider": member_provider, "model": member_model}
        try:
            iso = dirs[i] = _isolate(cwd)
        except Exception as e:
            # One tree that could not be copied is one lost candidate, not a lost run.
            rec.update(answer="", verified=False, turns=0, cost_usd=0.0,
                       error="isolation failed: %s: %s" % (type(e).__name__, e))
            if emit:
                with emit_lock:
                    emit(i, rec)
            return rec
        rec["dir"] = iso
        try:
            h = make_harness(iso, provider=member_provider, model=member_model,
                             project="%s-%d" % (run_tag, i),
                             code_search=True, exec_code=True)
            isolate_harness(h, read_project=project)
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
            # Serialized: `emit` belongs to the caller (the web UI streams from it) and was written
            # against a sequential loop. Concurrency here is ours to contain, not theirs to absorb.
            with emit_lock:
                emit(i, rec)
        return rec

    if parallel == 1:
        attempts = [_attempt(i) for i in range(n)]
    else:
        done = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=parallel) as pool:
            futures = {pool.submit(_attempt, i): i for i in range(n)}
            for future in concurrent.futures.as_completed(futures):
                i = futures[future]
                try:
                    done[i] = future.result()
                except Exception as e:      # one worker must never take the whole pack down
                    member_provider, member_model = members[i % len(members)]
                    done[i] = {"idx": i, "dir": dirs[i], "answer": "", "verified": False,
                               "turns": 0, "cost_usd": 0.0, "provider": member_provider,
                               "model": member_model,
                               "error": "%s: %s" % (type(e).__name__, e)}
        attempts = [done[i] for i in range(n)]     # attempt order, not finish order

    winner_idx, reason = select(attempts, have_check)
    applied = False
    if apply and winner_idx is not None and dirs[winner_idx]:
        # `dirs[winner_idx]` can be empty only when every attempt failed to isolate and select()
        # still had to return one of them. There is no tree to copy back, and inventing one would
        # be worse than applying nothing.
        _copy_back(dirs[winner_idx], cwd)
        applied = True

    result = {"n": n, "winner": winner_idx, "reason": reason, "applied": applied,
              "attempts": [{k: v for k, v in a.items() if k not in ("dir", "check_tail")}
                           for a in attempts],
              "roster": ["%s:%s" % (p, m) if m else p for p, m in members],
              "parallel": parallel,
              "total_cost_usd": round(sum(a.get("cost_usd", 0.0) for a in attempts), 4)}
    if winner_idx is not None:
        best = attempts[winner_idx]
        result["answer"] = best.get("answer", "")
        # Name the backend that won. "pack picked attempt 2" does not answer "which model should I
        # be running", which is the only reason to pay for a mixed roster.
        result["winner_provider"] = best.get("provider")
        result["winner_model"] = best.get("model")
    # clean the throwaway trees (a slot stays empty when its copy never succeeded)
    for d in dirs:
        if d:
            shutil.rmtree(d, ignore_errors=True)
    return result
