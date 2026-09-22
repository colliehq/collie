"""pack — best-of-N with EXECUTION-BASED selection.

collie's thesis is "don't trust the model's claim, run the code." Pack mode applies that to candidate
selection: run the task N independent times in isolated copies of the working tree, then pick the
winner by what actually PASSES — an optional check command (exit 0 = pass), then the harness's own
verification verdict (edited + a repro ran green), then a cheap quality tiebreak. If a check is
given and NOTHING passes it, pack refuses to apply a losing attempt — a no-op beats a wrong edit.

The winner's EDITS outlive the throwaway trees: each attempt's baseline is measured inside its own
isolated directory before the model runs, so the winning diff can be saved as a reviewable bundle
(``result["artifact"]``) and applied later — once, conflict-checked — without paying for the run
again. ``--apply`` uses that same bundle, so it can no longer mirror a stale candidate over edits
made while the candidates were running. See ``pack_artifacts``.

CLI:  collie pack "task" -n 3 --check "python -m pytest -q" [--apply]
"""
import concurrent.futures
import math
import os
import shutil
import subprocess
import tempfile
import threading

from . import pack_artifacts

# One definition, shared with the apply path: a tree Pack does not isolate is a tree Pack does not
# own, and neither the diff nor the apply may touch it.
_SKIP = set(pack_artifacts.SKIP_DIRS)


def _error_text(exc, prefix=""):
    """Bound and redact exception text before it reaches Pack UI/receipts."""
    from .runner_specs import redact_text
    detail = "%s: %s" % (type(exc).__name__, exc)
    return redact_text((prefix + detail) if prefix else detail)


def _safe_emit(emit, lock, index, record):
    """A disconnected UI/observer cannot turn a completed attempt into a leak."""
    if emit is None:
        return
    try:
        with lock:
            emit(index, record)
    except Exception:
        return


class _PackBudget:
    """Thread-safe token/$ ledger shared by every candidate in one Pack invocation.

    A per-Harness budget makes ``n=3`` silently authorize three times the limit.  This observer is
    deliberately small: Harness remains responsible for accounting each provider call, while Pack
    owns the aggregate ceiling and prevents candidates that have not started from spending it again.
    """

    def __init__(self, max_cost=0.0, max_tokens=0):
        if isinstance(max_cost, bool):
            raise ValueError("Pack cost limit must be a number")
        raw_cost = float(max_cost or 0)
        if not math.isfinite(raw_cost):
            raise ValueError("Pack cost limit must be finite")
        if isinstance(max_tokens, bool):
            raise ValueError("Pack token limit must be an integer")
        raw_tokens = int(max_tokens or 0)
        if isinstance(max_tokens, float) and not max_tokens.is_integer():
            raise ValueError("Pack token limit must be an integer")
        self.max_cost = max(0.0, raw_cost)
        self.max_tokens = max(0, raw_tokens)
        self.tokens = 0
        self.cost_usd = 0.0
        self.unknown_fields = set()
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls):
        try:
            max_cost = float(os.environ.get("COLLIE_MAX_COST", "0") or 0)
        except (TypeError, ValueError):
            raise ValueError("COLLIE_MAX_COST must be a finite number")
        if not math.isfinite(max_cost):
            raise ValueError("COLLIE_MAX_COST must be a finite number")
        try:
            max_tokens = int(os.environ.get("COLLIE_MAX_TOTAL_TOKENS", "0") or 0)
        except (TypeError, ValueError):
            raise ValueError("COLLIE_MAX_TOTAL_TOKENS must be an integer")
        return cls(max_cost, max_tokens) if max_cost > 0 or max_tokens > 0 else None

    def account(self, model, usage):
        from .costs import cost_usd
        tokens = (usage.input_tokens + usage.output_tokens +
                  usage.cache_read + usage.cache_creation)
        cost = cost_usd(model, usage.input_tokens, usage.output_tokens,
                        usage.cache_read, usage.cache_creation)
        self.account_values(tokens=tokens, cost_usd=cost)

    def account_values(self, tokens=None, cost_usd=None):
        """Account external usage, failing closed when a configured meter is absent."""
        with self._lock:
            if self.max_tokens > 0 and tokens is None:
                self.unknown_fields.add("tokens")
            elif tokens is not None:
                valid_tokens = (isinstance(tokens, int) and not isinstance(tokens, bool)
                                and tokens >= 0)
                if not valid_tokens:
                    self.unknown_fields.add("tokens")
                else:
                    self.tokens += tokens
            if self.max_cost > 0 and cost_usd is None:
                self.unknown_fields.add("cost_usd")
            elif cost_usd is not None:
                valid_cost = (isinstance(cost_usd, (int, float))
                              and not isinstance(cost_usd, bool)
                              and math.isfinite(float(cost_usd)) and cost_usd >= 0)
                if not valid_cost:
                    self.unknown_fields.add("cost_usd")
                else:
                    self.cost_usd += float(cost_usd)
            return tuple(sorted(self.unknown_fields))

    def exceeded(self):
        with self._lock:
            return (bool(self.unknown_fields) or
                    (self.max_tokens > 0 and self.tokens >= self.max_tokens) or
                    (self.max_cost > 0 and self.cost_usd >= self.max_cost))

    def snapshot(self):
        with self._lock:
            return {"tokens": self.tokens, "cost_usd": self.cost_usd,
                    "usage_unknown": bool(self.unknown_fields),
                    "unknown_fields": sorted(self.unknown_fields),
                    "exhausted": (bool(self.unknown_fields) or
                                  (self.max_tokens > 0 and self.tokens >= self.max_tokens) or
                                  (self.max_cost > 0 and self.cost_usd >= self.max_cost))}


def _ignore(_dir, names):
    return [n for n in names if n.lower() in _SKIP]


def _isolate(cwd):
    """A throwaway copy of the working tree (heavy/vcs dirs excluded) for one attempt."""
    dst = tempfile.mkdtemp(prefix="collie_pack_")
    try:
        # Return the directory we own. Cleanup can then delete this exact path;
        # deriving a parent from a test double once caused all of %TEMP% to be targeted.
        shutil.copytree(cwd, dst, ignore=_ignore, symlinks=True, dirs_exist_ok=True)
    except BaseException as copy_error:
        try:
            shutil.rmtree(dst)
        except BaseException as cleanup_error:
            # The partial tree can contain source files.  If it cannot be
            # removed, make that retention visible instead of reporting only
            # the original copy failure and silently orphaning the directory.
            raise RuntimeError(
                "Pack isolation failed and partial workspace %s could not be removed: %s"
                % (dst, _error_text(cleanup_error))) from copy_error
        raise
    return dst


def _init_external_git(cwd):
    """Give a copied Pack attempt a private baseline for worker mutation evidence."""
    from . import plat
    commands = (["git", "init", "--quiet"], ["git", "add", "-A"],
                ["git", "-c", "user.name=Collie Pack", "-c",
                 "user.email=pack@localhost", "commit", "--quiet", "--allow-empty",
                 "-m", "collie pack baseline"])
    for argv in commands:
        done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=120, **plat.no_window_kwargs())
        if done.returncode:
            raise RuntimeError("could not create external-worker Pack baseline: %s" %
                               ((done.stderr or done.stdout or "git failed").strip()[:300]))


def _run_check(cmd, cwd, timeout=300):
    evidence = _run_check_evidence(cmd, cwd, timeout)
    return evidence["passed"], evidence["output"][-2000:]


def _run_check_evidence(cmd, cwd, timeout=300, *, cancelled=None):
    from .verification import run_verification_command
    return run_verification_command(
        cmd, cwd, timeout=timeout, source="pack objective check", after_last_edit=True,
        cancelled=cancelled)


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


def _task_text(task):
    """A short, redacted label for the bundle. Never the workspace's file contents."""
    from .runner_specs import redact_text
    if isinstance(task, (list, tuple)):
        parts = [str(item.get("text", "")) for item in task if isinstance(item, dict)]
        task = " ".join(p for p in parts if p)
    return redact_text(str(task or ""))[:400]


def _save_winner(attempt_dir, baseline, baseline_error, cwd, metadata):
    """Persist the winner's diff BEFORE the throwaway tree is deleted.

    Returns ``(record or None, error)``. ``(None, "")`` means the winner changed nothing — the
    cheap, common case for a question-only pack, which must not create an empty bundle. A non-empty
    error means the winner is NOT saved, and the caller keeps its attempt directory.
    """
    if baseline is None:
        return None, (baseline_error or "no baseline manifest was captured for the winner")
    try:
        bundle = pack_artifacts.create_artifact(
            attempt_dir, baseline, workspace=cwd, metadata=metadata)
        if bundle.empty:
            return None, ""
        return pack_artifacts.save_artifact(bundle), ""
    except Exception as exc:
        return None, _error_text(exc, "winner changes could not be saved: ")


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
             history=None, runner_decision=None, runner_model="", limits=None,
             capabilities=None):
    """Run N isolated attempts, select the winner by execution, optionally apply it back.

    ``roster`` runs the attempts on DIFFERENT backends, assigned round-robin. Selection stays what
    PASSES, never opinion, so a weak member costs tokens and nothing else — it cannot win unless it
    actually passed. That is what makes model diversity safe to add HERE rather than somewhere a
    model would be doing the judging.

    ``parallel`` is the maximum number of attempts in flight. It stays 1 by default: several
    attempts at once on ONE backend is a rate-limit magnet, and a subscription plan is the easiest
    thing to trip. A roster spread across different accounts is the case worth raising it for.
    """
    from .cli import (configure_run_options, make_harness, _paths,
                      _worker_history_note, _worker_provider)
    from . import settings, capability_policy
    run_limits = settings.enforce_pinned(limits) if limits is not None else settings.current_limits()
    run_capabilities = dict(capabilities) if capabilities is not None else capability_policy.snapshot()
    from .scratch import isolate_harness
    external_worker = bool(runner_decision and getattr(runner_decision, "runner", "") != "collie")
    if external_worker and roster:
        raise ValueError("Pack accepts either a provider roster or one external worker, not both")
    provider = provider or settings.get("PROVIDER", "anthropic")   # env > settings.json > API default
    members = ([] if external_worker else normalize_roster(roster, provider, model))
    n = max(1, min(8, int(n)))
    if roster and len(members) > n:
        # Never silently drop a model someone named: a roster of 4 at n=3 would have looked like a
        # complete comparison while one backend never ran at all.
        n = min(8, len(members))
    parallel = max(1, min(int(parallel or 1), n))
    requested_parallel = parallel
    shared_budget = (_PackBudget(run_limits.max_cost, run_limits.max_total_tokens)
                     if limits is not None and (run_limits.max_cost or run_limits.max_total_tokens)
                     else None if limits is not None else _PackBudget.from_env())
    if shared_budget is not None:
        shared_budget.limits = run_limits
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
    blocked = [] if external_worker else preflight(members)
    if blocked:
        return {"n": n, "winner": None, "reason": "; ".join(blocked), "applied": False,
                "attempts": [], "total_cost_usd": 0.0, "apply_error": "", "canceled": False,
                "roster": ([runner_decision.runner] if external_worker else
                           ["%s:%s" % (p, m) if m else p for p, m in members]),
                "parallel": parallel, "requested_parallel": requested_parallel,
                "budget_exhausted": False, "budget_usage_unknown": False,
                "budget_unknown_fields": [],
                "budget_tokens": 0, "budget_cost_usd": 0.0,
                # Same shape on every return path: a UI that reads result["artifact"] must not
                # have to special-case the run that never started.
                "artifact": None, "artifact_error": "", "apply_conflicts": [],
                "retained_attempt_dir": ""}
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
    # The winner's diff is measured against the tree ITS attempt started from, so the baseline has
    # to be taken from that isolated directory before any model work — never from the live
    # workspace afterwards, where the user's own concurrent edits are indistinguishable from the
    # candidate's. Saving is on unless disabled, and always on when the caller asked to apply.
    want_artifact = bool(apply) or pack_artifacts.enabled()
    baselines = [None] * n
    baseline_errors = [""] * n
    emit_lock = threading.Lock()

    def _cancelled():
        try:
            return bool(cancel and cancel())
        except Exception:
            return False

    def _attempt(i):
        member_provider, member_model = (("", None) if external_worker else
                                         members[i % len(members)])
        # Which backend produced which candidate. Without this the winner is anonymous and the one
        # question a mixed roster exists to answer — WHICH model wins, how often — is unanswerable.
        rec = {"idx": i, "provider": member_provider, "model": member_model,
               "runner": runner_decision.runner if external_worker else "collie",
               "effort": effort, "speed": speed}
        if _cancelled():
            rec.update(answer="", verified=False, turns=0, cost_usd=0.0,
                       error="canceled by user")
            _safe_emit(emit, emit_lock, i, rec)
            return rec
        if shared_budget is not None and shared_budget.exceeded():
            rec.update(answer="", verified=False, turns=0, cost_usd=0.0,
                       error="pack budget exhausted")
            _safe_emit(emit, emit_lock, i, rec)
            return rec
        try:
            iso = dirs[i] = _isolate(cwd)
        except Exception as e:
            # One tree that could not be copied is one lost candidate, not a lost run.
            rec.update(answer="", verified=False, turns=0, cost_usd=0.0,
                       error=_error_text(e, "isolation failed: "))
            _safe_emit(emit, emit_lock, i, rec)
            return rec
        rec["dir"] = iso
        if want_artifact:
            try:
                baselines[i] = pack_artifacts.capture_baseline(iso)
            except Exception as e:
                # A candidate whose baseline could not be measured can still run and still win;
                # it just cannot be turned into a reviewable bundle. Say so if it does win.
                baseline_errors[i] = _error_text(e, "baseline capture failed: ")
        h = None
        external_recorder = None
        res = None
        try:
            if external_worker:
                from . import runner_slice
                from .recorder import Recorder
                _init_external_git(iso)
                external_recorder = Recorder(_paths()[1])
                res = runner_slice.run_adhoc(
                    runner_decision, task, iso, cancelled=_cancelled,
                    history_note=_worker_history_note(history), model=runner_model,
                    speed=speed, effort=effort,
                    provider=_worker_provider(runner_decision), task_id="pack%d" % i,
                    recorder=external_recorder)
                worker_receipt = runner_slice.receipt_of(res)
                actual_runner = (worker_receipt.runner if worker_receipt is not None
                                 else getattr(res, "harness", "") or
                                 runner_decision.runner)
                rec.update(
                    answer=res.answer or "", verified=False,
                    turns=res.turns, error=res.error or "",
                    cost_usd=res.cost_usd,
                    model=getattr(res, "model", None) or runner_model or None,
                    runner=actual_runner,
                    runner_receipt=(worker_receipt.to_dict() if worker_receipt else None))
                if shared_budget is not None:
                    unknown = shared_budget.account_values(res.total_tokens, res.cost_usd)
                    if unknown:
                        rec["error"] = ((rec.get("error") + "; ") if rec.get("error") else "") + \
                            "pack budget cannot be enforced: worker did not report %s" % \
                            ", ".join(unknown)
            else:
                gate = gate_factory(iso) if gate_factory is not None else None
                h = make_harness(iso, provider=member_provider, model=member_model,
                                 effort=effort, speed=speed,
                                 project="%s-%d" % (run_tag, i),
                                 code_search=True, exec_code=True, gate=gate,
                                 limits=run_limits, capabilities=run_capabilities)
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
            rec.update(answer="", verified=False, turns=0, error=_error_text(e),
                       # The exception may have happened after a physical model
                       # request. Unknown is not a free attempt.
                       cost_usd=None)
            if external_worker and shared_budget is not None:
                shared_budget.account_values(None, None)
        finally:
            if h is not None:
                try:
                    h.memory.close(); h.recorder.close()
                except Exception:
                    pass
        if have_check and not rec.get("error") and not _cancelled():
            try:
                evidence = _run_check_evidence(check, iso, cancelled=_cancelled)
                rec["check_pass"] = evidence["passed"]
                rec["check_tail"] = evidence["output"][-2000:]
                rec["verification_evidence"] = evidence
                if external_worker:
                    rec["verified"] = bool(evidence["passed"])
            except Exception as exc:
                # Starting a check is host bookkeeping, not another worker
                # attempt. Keep this candidate as a failed check and continue
                # to the other isolated trees; most importantly, still reach
                # recorder close, UI emission, and exact-root cleanup below.
                rec["check_pass"] = False
                rec["verified"] = False
                rec["error"] = _error_text(exc, "verification failed: ")
        if _cancelled() and not rec.get("error"):
            rec["error"] = "canceled by user"
        if external_recorder is not None:
            try:
                if res is not None:
                    res.verified = bool(rec.get("verified"))
                    try:
                        external_recorder.finish_run(res)
                    except Exception:
                        # The attempt and host check are the facts; telemetry is
                        # best-effort and must not strand the isolated tree.
                        pass
            finally:
                try:
                    external_recorder.close()
                except Exception:
                    pass
        # Serialized: `emit` belongs to the caller (the web UI streams from it)
        # and was written against a sequential loop. A dead observer is not an
        # attempt failure and is never allowed to bypass final cleanup.
        _safe_emit(emit, emit_lock, i, rec)
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
                    member_provider, member_model = (("", None) if external_worker else
                                                     members[i % len(members)])
                    done[i] = {"idx": i, "dir": dirs[i], "answer": "", "verified": False,
                               "turns": 0, "cost_usd": None, "provider": member_provider,
                               "model": member_model,
                               "runner": (runner_decision.runner if external_worker else "collie"),
                               "error": _error_text(e)}
        attempts = [done[i] for i in range(n)]     # attempt order, not finish order

    canceled = _cancelled() or any(a.get("error") == "canceled by user" for a in attempts)
    winner_idx, reason = (None, "canceled by user") if canceled else select(attempts, have_check)
    applied = False
    apply_error = ""
    apply_conflicts = []
    artifact_record = None
    artifact_error = ""
    # `dirs[winner_idx]` can be empty only when every attempt failed to isolate and select() still
    # had to return one of them. There is nothing to save and nothing to apply, and inventing a
    # tree would be worse than applying nothing.
    winner_dir = dirs[winner_idx] if winner_idx is not None else None
    if want_artifact and winner_dir and not canceled:
        best = attempts[winner_idx]
        artifact_record, artifact_error = _save_winner(
            winner_dir, baselines[winner_idx], baseline_errors[winner_idx], cwd,
            metadata={"task": _task_text(task), "check": str(check or ""),
                      "reason": reason, "attempt": winner_idx, "project": project,
                      "provider": best.get("provider") or "", "model": best.get("model") or "",
                      "runner": best.get("runner") or "collie",
                      "turns": best.get("turns", 0),
                      "check_pass": best.get("check_pass"),
                      "verified": bool(best.get("verified"))})
    if apply and winner_idx is not None and not canceled:
        # Immediate apply is the SAME bundle apply as a later review→apply: baseline-compared,
        # conflict-refusing, backed up. It is never a mirror of the whole stale candidate tree.
        if artifact_error:
            apply_error = artifact_error
        elif artifact_record is None:
            applied = bool(winner_dir)          # the winner changed no files: nothing to apply
        else:
            try:
                outcome = pack_artifacts.apply_artifact(artifact_record["id"], cwd)
            except Exception as e:              # defensive: apply reports, it does not raise
                outcome = {"applied": False, "error": _error_text(e), "conflicts": []}
            applied = bool(outcome.get("applied"))
            if not applied:
                apply_error = outcome.get("error") or "apply was refused"
                apply_conflicts = list(outcome.get("conflicts") or ())[:20]
        if not applied and apply_error:
            reason = "%s; apply failed: %s" % (reason, apply_error)

    budget = shared_budget.snapshot() if shared_budget is not None else {
        "tokens": 0, "cost_usd": 0.0, "usage_unknown": False,
        "unknown_fields": [], "exhausted": False}
    attempt_costs = [attempt.get("cost_usd") for attempt in attempts]
    total_cost = (
        None if (shared_budget is not None and budget["usage_unknown"]) else
        (round(budget["cost_usd"], 4) if shared_budget is not None else
         (None if any(value is None for value in attempt_costs) else
          round(sum(float(value or 0.0) for value in attempt_costs), 4))))
    result = {"n": n, "winner": winner_idx, "reason": reason, "applied": applied,
              "apply_error": apply_error, "canceled": canceled,
              "attempts": [{k: v for k, v in a.items() if k not in ("dir", "check_tail")}
                           for a in attempts],
              "roster": ([runner_decision.runner] if external_worker else
                         ["%s:%s" % (p, m) if m else p for p, m in members]),
              "parallel": parallel,
              "requested_parallel": requested_parallel,
              "budget_exhausted": budget["exhausted"],
              "budget_usage_unknown": budget["usage_unknown"],
              "budget_unknown_fields": budget["unknown_fields"],
              "budget_tokens": budget["tokens"],
              "budget_cost_usd": round(budget["cost_usd"], 6),
              "total_cost_usd": total_cost,
              # A compact view only: ids, counts and a bounded path preview. The full change list
              # (and never the file contents) is read back with pack_artifacts.inspect_artifact,
              # so a Pack `done` event does not carry a serialized workspace.
              "artifact": (pack_artifacts.summarize_artifact(artifact_record)
                           if artifact_record else None),
              "artifact_error": artifact_error,
              "apply_conflicts": apply_conflicts}
    if winner_idx is not None:
        best = attempts[winner_idx]
        result["answer"] = best.get("answer", "")
        # Name the backend that won. "pack picked attempt 2" does not answer "which model should I
        # be running", which is the only reason to pay for a mixed roster.
        result["winner_provider"] = best.get("provider")
        result["winner_model"] = best.get("model")
        result["winner_runner"] = best.get("runner") or "collie"
    # Clean the exact throwaway roots we created. A failed deletion is part of
    # the outcome: it may retain source, worker transcripts, or half-applied
    # candidate edits and must not disappear behind ignore_errors=True.
    cleanup_errors = []
    # A winner we could not persist is NOT deleted as though it had been saved: its exact owned
    # directory is kept and named, so the work someone already paid for is still recoverable.
    retained = winner_dir if (artifact_error and winner_dir) else None
    for idx, d in enumerate(dirs):
        if not d or d == retained:
            continue
        try:
            shutil.rmtree(d)
        except Exception as exc:
            cleanup_errors.append({"idx": idx, "error": _error_text(
                exc, "attempt cleanup failed: ")})
    result["cleanup_errors"] = cleanup_errors
    result["retained_attempt_dir"] = retained or ""
    notes = []
    if cleanup_errors:
        notes.append("%d attempt workspace(s) could not be removed" % len(cleanup_errors))
    if retained:
        notes.append("winner kept at %s: %s" % (retained, artifact_error))
    if notes:
        result["reason"] = ((result.get("reason") + "; ")
                            if result.get("reason") else "") + "; ".join(notes)
    return result
