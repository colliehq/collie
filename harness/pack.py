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


class _PackBudget:
    """Thread-safe token/$ ledger shared by every candidate in one Pack invocation.

    A per-Harness budget makes ``n=3`` silently authorize three times the limit.  This observer is
    deliberately small: Harness remains responsible for accounting each provider call, while Pack
    owns the aggregate ceiling and prevents candidates that have not started from spending it again.
    """

    def __init__(self, max_cost=0.0, max_tokens=0):
        self.max_cost = max(0.0, float(max_cost or 0))
        self.max_tokens = max(0, int(max_tokens or 0))
        self.tokens = 0
        self.cost_usd = 0.0
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        try:
            max_cost = float(os.environ.get("COLLIE_MAX_COST", "0") or 0)
        except (TypeError, ValueError):
            max_cost = 0.0
        try:
            max_tokens = int(os.environ.get("COLLIE_MAX_TOTAL_TOKENS", "0") or 0)
        except (TypeError, ValueError):
            max_tokens = 0
        return cls(max_cost, max_tokens) if max_cost > 0 or max_tokens > 0 else None

    def account(self, model, usage):
        from .costs import cost_usd
        tokens = (usage.input_tokens + usage.output_tokens +
                  usage.cache_read + usage.cache_creation)
        cost = cost_usd(model, usage.input_tokens, usage.output_tokens,
                        usage.cache_read, usage.cache_creation)
        with self._lock:
            self.tokens += tokens
            self.cost_usd += cost

    def exceeded(self):
        with self._lock:
            return ((self.max_tokens > 0 and self.tokens >= self.max_tokens) or
                    (self.max_cost > 0 and self.cost_usd >= self.max_cost))

    def snapshot(self):
        with self._lock:
            return {"tokens": self.tokens, "cost_usd": self.cost_usd,
                    "exhausted": ((self.max_tokens > 0 and self.tokens >= self.max_tokens) or
                                  (self.max_cost > 0 and self.cost_usd >= self.max_cost))}


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
    evidence = _run_check_evidence(cmd, cwd, timeout)
    return evidence["passed"], evidence["output"][-2000:]


def _run_check_evidence(cmd, cwd, timeout=300):
    from .verification import run_verification_command
    return run_verification_command(
        cmd, cwd, timeout=timeout, source="pack objective check", after_last_edit=True)


def select(attempts, have_check):
    """Pure selection over attempts (list of dicts with keys: check_pass bool|None, verified bool,
    answer str, turns int, error str, idx int). Returns (winner_idx or None, reason).

    Order of preference:
      1. if a check was given: only check-passing attempts are eligible; if none pass -> no winner.
      2. among eligible: prefer verified (repro ran green), then a real answer, then fewer turns.
    """
    pool = [a for a in attempts if not a.get("error")]
    if not pool:
        return None, "every attempt failed"
    if have_check:
        passing = [a for a in pool if a.get("check_pass")]
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
    """Make ``dst`` exactly match the winning tree, excluding deliberately unisolated heavy dirs.

    Copy-only semantics left deleted files behind, so a candidate could pass in isolation and then
    become a different, failing tree when applied. Filesystem errors are intentionally propagated:
    callers must never print APPLIED after a partial or refused operation.
    """
    src, dst = os.path.realpath(src), os.path.realpath(dst)
    if not os.path.isdir(src) or not os.path.isdir(dst) or src == dst:
        raise OSError("invalid pack apply roots")

    # Remove paths the winner removed. Work bottom-up, never crossing one of the excluded trees.
    # With ``topdown=False`` pruning ``dirs`` cannot prevent os.walk from having already visited a
    # child.  Check every component of the current relative path too, otherwise
    # ``packages/app/node_modules`` (and nested .venv/build trees) are emptied before their parent
    # gets a chance to filter the directory name.
    for root, dirs, files in os.walk(dst, topdown=False, followlinks=False):
        rel = os.path.relpath(root, dst)
        if rel != "." and any(part in _SKIP for part in rel.split(os.sep)):
            continue
        dirs[:] = [d for d in dirs if d not in _SKIP]
        source_root = src if rel == "." else os.path.join(src, rel)
        for name in files:
            if name in _SKIP:
                continue
            if not os.path.lexists(os.path.join(source_root, name)):
                os.remove(os.path.join(root, name))
        for name in dirs:
            target_path = os.path.join(root, name)
            source_path = os.path.join(source_root, name)
            if not os.path.lexists(source_path):
                if os.path.islink(target_path):
                    os.remove(target_path)
                else:
                    shutil.rmtree(target_path)

    # Then copy every winner path. Resolve file/directory type changes explicitly.
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if d not in _SKIP]
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else os.path.join(dst, rel)
        if os.path.lexists(target_root) and (os.path.islink(target_root)
                                                or not os.path.isdir(target_root)):
            os.remove(target_root)
        os.makedirs(target_root, exist_ok=True)
        # os.walk lists symlinks-to-directories in ``dirs`` but (correctly) does not traverse them;
        # copy them here or the applied tree silently loses that path.
        for d in list(dirs):
            source_dir = os.path.join(root, d)
            if not os.path.islink(source_dir):
                continue
            dirs.remove(d)
            target_dir = os.path.join(target_root, d)
            if os.path.isdir(target_dir) and not os.path.islink(target_dir):
                shutil.rmtree(target_dir)
            elif os.path.lexists(target_dir):
                os.remove(target_dir)
            os.symlink(os.readlink(source_dir), target_dir, target_is_directory=True)
        for f in files:
            source_file = os.path.join(root, f)
            target_file = os.path.join(target_root, f)
            if os.path.isdir(target_file) and not os.path.islink(target_file):
                shutil.rmtree(target_file)
            elif os.path.lexists(target_file) and os.path.islink(source_file) != os.path.islink(target_file):
                os.remove(target_file)
            if os.path.islink(source_file):
                if os.path.lexists(target_file):
                    os.remove(target_file)
                os.symlink(os.readlink(source_file), target_file,
                           target_is_directory=os.path.isdir(source_file))
            else:
                shutil.copy2(source_file, target_file)


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


def run_pack(task, cwd, n=3, check=None, provider=None, model=None, effort=None,
             speed="standard",
             apply=False, emit=None, project="pack", roster=None, parallel=1,
             cancel=None, quality="balanced", verification="auto", gate_factory=None,
             history=None):
    """Run N isolated attempts, select the winner by execution, optionally apply it back.

    ``roster`` runs the attempts on DIFFERENT backends, assigned round-robin. Selection stays what
    PASSES, never opinion, so a weak member costs tokens and nothing else — it cannot win unless it
    actually passed. That is what makes model diversity safe to add HERE rather than somewhere a
    model would be doing the judging.

    ``parallel`` is the maximum number of attempts in flight. It stays 1 by default: several
    attempts at once on ONE backend is a rate-limit magnet, and a subscription plan is the easiest
    thing to trip. A roster spread across different accounts is the case worth raising it for.
    """
    from .cli import configure_run_options, make_harness
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
    requested_parallel = parallel
    shared_budget = _PackBudget.from_env()
    # Without a reservation protocol the spend of an in-flight model call is unknowable. Letting N
    # workers all observe an empty ledger would therefore permit N first calls past a supposedly
    # aggregate hard cap. Budgeted Packs serialize candidates; unbudgeted Packs keep the requested
    # parallelism and its existing performance characteristics.
    if shared_budget is not None:
        parallel = 1
    # Check the backends BEFORE spending attempts on them. An expired subscription token or an
    # unset API key otherwise shows up as N identical failures and a "no attempt passed the
    # check", which reads like the task was hard rather than like nobody was logged in.
    from .catalog import preflight
    blocked = preflight(members)
    if blocked:
        return {"n": n, "winner": None, "reason": "; ".join(blocked), "applied": False,
                "attempts": [], "total_cost_usd": 0.0, "apply_error": "", "canceled": False,
                "roster": ["%s:%s" % (p, m) if m else p for p, m in members],
                "parallel": parallel, "requested_parallel": requested_parallel,
                "budget_exhausted": False, "budget_tokens": 0, "budget_cost_usd": 0.0}
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

    def _cancelled():
        try:
            return bool(cancel and cancel())
        except Exception:
            return False

    def _attempt(i):
        member_provider, member_model = members[i % len(members)]
        # Which backend produced which candidate. Without this the winner is anonymous and the one
        # question a mixed roster exists to answer — WHICH model wins, how often — is unanswerable.
        rec = {"idx": i, "provider": member_provider, "model": member_model,
               "effort": effort, "speed": speed}
        if _cancelled():
            rec.update(answer="", verified=False, turns=0, cost_usd=0.0,
                       error="canceled by user")
            if emit:
                with emit_lock:
                    emit(i, rec)
            return rec
        if shared_budget is not None and shared_budget.exceeded():
            rec.update(answer="", verified=False, turns=0, cost_usd=0.0,
                       error="pack budget exhausted")
            if emit:
                with emit_lock:
                    emit(i, rec)
            return rec
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
        h = None
        try:
            gate = gate_factory(iso) if gate_factory is not None else None
            h = make_harness(iso, provider=member_provider, model=member_model,
                             effort=effort, speed=speed,
                             project="%s-%d" % (run_tag, i),
                             code_search=True, exec_code=True, gate=gate)
            configure_run_options(h, quality=quality, verification=verification)
            isolate_harness(h, read_project=project)
            h.cancelled = _cancelled
            h.shared_budget = shared_budget
            res = h.run("pack%d" % i, task, history=history)
            rec.update(answer=res.answer or "", verified=bool(getattr(res, "verified", False)),
                       turns=res.turns, error=res.error or "", cost_usd=res.cost_usd,
                       model=getattr(res, "model", None) or member_model,
                       speed=getattr(getattr(h, "provider", None), "actual_speed", speed))
        except Exception as e:
            rec.update(answer="", verified=False, turns=0, error="%s: %s" % (type(e).__name__, e),
                       cost_usd=0.0)
        finally:
            if h is not None:
                try:
                    h.memory.close(); h.recorder.close()
                except Exception:
                    pass
        if have_check and not rec.get("error") and not _cancelled():
            evidence = _run_check_evidence(check, iso)
            rec["check_pass"] = evidence["passed"]
            rec["check_tail"] = evidence["output"][-2000:]
            rec["verification_evidence"] = evidence
        if _cancelled() and not rec.get("error"):
            rec["error"] = "canceled by user"
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

    canceled = _cancelled() or any(a.get("error") == "canceled by user" for a in attempts)
    winner_idx, reason = (None, "canceled by user") if canceled else select(attempts, have_check)
    applied = False
    apply_error = ""
    if apply and winner_idx is not None and dirs[winner_idx] and not canceled:
        # `dirs[winner_idx]` can be empty only when every attempt failed to isolate and select()
        # still had to return one of them. There is no tree to copy back, and inventing one would
        # be worse than applying nothing.
        try:
            _copy_back(dirs[winner_idx], cwd)
            applied = True
        except Exception as e:
            apply_error = "%s: %s" % (type(e).__name__, e)
            reason = "%s; apply failed: %s" % (reason, apply_error)

    budget = shared_budget.snapshot() if shared_budget is not None else {
        "tokens": 0, "cost_usd": 0.0, "exhausted": False}
    result = {"n": n, "winner": winner_idx, "reason": reason, "applied": applied,
              "apply_error": apply_error, "canceled": canceled,
              "attempts": [{k: v for k, v in a.items() if k not in ("dir", "check_tail")}
                           for a in attempts],
              "roster": ["%s:%s" % (p, m) if m else p for p, m in members],
              "parallel": parallel,
              "requested_parallel": requested_parallel,
              "budget_exhausted": budget["exhausted"],
              "budget_tokens": budget["tokens"],
              "budget_cost_usd": round(budget["cost_usd"], 6),
              "total_cost_usd": round((budget["cost_usd"] if shared_budget is not None else
                                       sum(a.get("cost_usd", 0.0) for a in attempts)), 4)}
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
