"""Run one ad-hoc task on an external worker and bring Collie's evidence back.

This is the whole distance between a *decision* (``runner_select.decide`` said
which worker) and a *result* (``recorder.RunResult`` plus a ``RunnerReceipt``).
``collie run --runner claude-code "..."`` lands here instead of building a
``loop.Harness``; phase 2 adds ``run_mission_slice`` beside it for the Mission
code slice, which is why this module exists at all rather than the branch living
inside ``cli.cmd_run``.

Three decisions are worth stating out loud, because each is a place where the
obvious implementation would quietly break a promise Collie makes:

* **Falling back is only honest before the worker reads the prompt.**  A worker
  that never started cost nothing and changed nothing, so trying the next entry
  of ``decision.fallback_chain`` is free.  Once the prompt is in the child's
  stdin, *any* failure — a timeout, a cancel, a refused approval, a rate limit,
  a half-applied edit — may have left work behind, and starting a second worker
  on the same workspace would race the first one's leftovers and bill a second
  account for a job that is partly done.  After that point this function returns
  the failure, and the receipt says which worker owns the mess
  (``recovery_required``).  Resuming a thread never falls back either: the
  locator belongs to one worker's own session and means nothing to another.

* **Settled is not verified.**  ``snapshot_to_run_result`` always writes
  ``verified=False`` and so does the receipt path here.  Only the host verifier
  that runs *after* the worker exits may set it — that reversal is the product.

* **Cancellation is asked for, never taken.**  The watcher thread calls
  ``runner.cancel_current()``; it does not know a pid and must not learn one.
  Process-tree ownership (Job objects / process groups, and the extinction proof
  that goes with them) lives in ``agent_runners.SubprocessRunner``, and a second
  killer would be a second, unproven claim about the same tree.

Return shape: ``run_adhoc`` returns a ``RunResult`` — the type ``cli.cmd_run``
already stores, logs and prints — with the ``RunnerReceipt`` attached as
``result.runner_receipt`` (see :func:`receipt_of`).  The receipt is never None
on the returned object, including on every failure path: a run that produced no
evidence still has to say which worker, which account family and which billing
class it did not produce evidence for.

Credentials never enter this module: it passes no environment, reads no login
file, and every string that reaches a receipt goes through ``redact_text`` /
``redact_value`` first.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Mapping

from . import runner_registry as registry
from .agent_runners import RunnerSnapshot
from .recorder import RunResult
from .runner_specs import (
    HarnessDecision,
    HarnessSpec,
    NativeSessionRef,
    RunnerCapabilities,
    RunnerProbe,
    RunnerReceipt,
    RunnerSelectionError,
    RunnerUnavailableError,
    equivalent_cost_usd,
    redact_text,
    redact_value,
    snapshot_to_run_result,
    stable_digest,
    usage_to_collie,
)

# How often the watcher asks the caller "has the user cancelled?".  Half a second
# is the interval §F.1 specifies: fast enough that a cancel feels immediate, slow
# enough that a predicate doing real work (a DB read in the web surface) is not
# called thousands of times during a long turn.
CANCEL_POLL_S = 0.5

# Attribute the receipt is attached to on the returned RunResult.  Named here so
# a caller can `getattr(res, runner_slice.RECEIPT_ATTR)` without hardcoding it.
RECEIPT_ATTR = "runner_receipt"

# Events replayed through `emit` after a turn.  The snapshot is already bounded
# (`max_events`), but a phase-1 emit is a UI feed, not a transcript store.
_MAX_REPLAYED_EVENTS = 200


# --- public helpers ---------------------------------------------------------
def receipt_of(result: Any) -> RunnerReceipt | None:
    """The ``RunnerReceipt`` :func:`run_adhoc` attached to ``result``, if any.

    Total by design: a ``RunResult`` produced by the native harness has no
    runner receipt, and asking for one must not be an AttributeError at the point
    where a caller is trying to write a session receipt.
    """
    receipt = getattr(result, RECEIPT_ATTR, None)
    return receipt if isinstance(receipt, RunnerReceipt) else None


def run_adhoc(decision: HarnessDecision, task: str, workspace: str, *,
              timeout_s: float | None = None,
              emit: Callable[[str, dict], Any] | None = None,
              cancelled: Callable[[], bool] | None = None,
              history_note: str | None = None,
              resume_from: Any = None,
              model: str = "", provider: str = "",
              task_id: str = "") -> RunResult:
    """Carry ``task`` to the worker ``decision`` chose and report what happened.

    ``decision`` must be a decision to *run*: an empty ``runner`` or a non-empty
    ``error`` means the selector refused, and the caller is required to refuse
    too (exit 2 / ``done{error}``) instead of arriving here — so that is raised,
    not returned, because it is a bug in the caller rather than a bad run.

    ``resume_from`` continues the worker's own thread instead of opening a new
    one.  It accepts the ``native_session`` locator from a previous receipt (a
    string), that receipt's ``native_session`` dict, a serialized
    ``RunnerSnapshot``, or a live one.  Only a full snapshot carries the prior
    cursor and cumulative usage; from a bare locator the thread continues but the
    usage counted here is this turn's alone, which the receipt reflects.

    ``emit(kind, payload)`` receives ``"decision"`` before the worker starts,
    ``"runner"`` for each native event replayed after it stops (and for a
    fallback), and ``"receipt"`` at the end.  It is best-effort: an exception
    raised by the caller's emitter is swallowed, because a stream consumer that
    hung up must not be able to fail a run that is otherwise fine.  Phase 2's
    ``runner_events`` is what turns those native events into canonical ones.

    ``cancelled()`` is polled on a background thread; when it goes True the
    watcher asks ``runner.cancel_current()`` (repeatedly, until the runner
    confirms, so a cancel arriving mid-launch is not lost to the start-gate
    race).  A cancel that lands before the worker starts skips the launch
    entirely rather than paying for a turn nobody wants.

    ``model``/``provider`` are what the caller *asked* for — phase-1 protocols do
    not echo the model back, so they label the runs.db row (and price the
    equivalent cost) while ``receipt.model`` stays empty unless the worker
    actually reported one.  Both are keyword extras beyond the design's
    signature; without them an external worker would silently use its CLI
    default and the row would not say which Brain answered.

    Returns a ``RunResult`` with a ``RunnerReceipt`` attached (:func:`receipt_of`
    / :data:`RECEIPT_ATTR`).  ``verified`` is never set here.
    """
    if not isinstance(decision, HarnessDecision):
        raise TypeError("decision must be a HarnessDecision")
    if decision.error or not decision.runner:
        raise RunnerSelectionError(
            "refusing to run: the selector returned no usable worker (%s)"
            % (decision.error or "no runner"))
    if decision.runner == "collie":
        # The native harness is this process, not a worker to launch.  Surfacing
        # the wrong branch here keeps `cli.cmd_run` on the stack; a None runner
        # would surface it later as an AttributeError inside the slice.
        raise RunnerSelectionError(
            "collie is not an external worker; the caller took the wrong branch")

    root = _canonical_workspace(workspace)
    prompt = _prompt_text(task, history_note)
    prior = _resume_snapshot(resume_from, decision.runner, root)
    chain = _attempt_chain(decision, resuming=prior is not None)

    _safe_emit(emit, "decision", {
        "runner": decision.runner,
        "label": _label_of(decision.runner),
        "source": decision.source,
        "credential_family": decision.credential_family,
        "billing_class": decision.billing_class,
        "billing_mode": decision.billing_mode,
        "reasons": list(decision.reasons),
        "fallback_chain": list(chain[1:]),
        "resumed": prior is not None,
    })

    outcome: _Attempt | None = None
    for index, key in enumerate(chain):
        if index:
            _safe_emit(emit, "runner", {
                "event": "fallback", "from": decision.runner, "to": key,
                "reason": outcome.reason if outcome is not None else "",
            })
        outcome = _attempt(key, prompt, root, prior, timeout_s, model, cancelled)
        if not (outcome.pre_prompt_failure and index + 1 < len(chain)):
            break

    assert outcome is not None                      # chain is never empty
    fallback_from = decision.runner if outcome.key != decision.runner else ""
    return _finish(decision, outcome, prior, emit,
                   fallback_from=fallback_from, model=model, provider=provider,
                   task_id=task_id)


# --- one attempt ------------------------------------------------------------
class _Attempt:
    """What one worker did with the prompt — enough to decide about fallback.

    Not a dataclass: it is assembled in pieces across a try/except and only ever
    lives inside this module.
    """

    def __init__(self, key: str, spec: HarnessSpec | None, snapshot: RunnerSnapshot,
                 *, pre_prompt_failure: bool, reason: str = "",
                 env_receipt: dict | None = None):
        self.key = key
        self.spec = spec
        self.snapshot = snapshot
        self.pre_prompt_failure = pre_prompt_failure
        self.reason = reason
        self.env_receipt = dict(env_receipt or {"allowed": [], "stripped": []})


def _attempt(key: str, prompt: str, workspace: str, prior: RunnerSnapshot | None,
             timeout_s: float | None, model: str,
             cancelled: Callable[[], bool] | None) -> _Attempt:
    """Build the worker, run one turn under a cancel watcher, and describe it."""
    spec = registry.SPECS.get(key)
    if spec is None:
        return _failed_attempt(
            key, None, workspace, prior,
            "unknown runner %r; `collie runners` lists the keys that exist" % key,
            pre_prompt_failure=True)

    try:
        runner = registry.make_runner(key, model=model, timeout_s=timeout_s,
                                      env_policy=spec.env_policy)
    except (FileNotFoundError, RunnerUnavailableError) as exc:
        # The worker does not exist on this host: nothing was launched, nothing
        # was billed, so the next entry in the chain is still fair game.
        return _failed_attempt(key, spec, workspace, prior, _error_text(exc),
                               pre_prompt_failure=True)
    except Exception as exc:
        return _failed_attempt(key, spec, workspace, prior, _error_text(exc),
                               pre_prompt_failure=False)

    if prior is not None and prior.runner != key:
        # Only reachable if a caller hand-built a chain; a locator from another
        # worker's session is not a thread this one can continue.
        return _failed_attempt(
            key, spec, workspace, prior,
            "cannot resume a %s session on %s" % (prior.runner, key),
            pre_prompt_failure=False)

    # A cancel that is already true has to stop the launch, not the turn: the
    # watcher's first `cancel_current()` would find nothing active yet and the
    # process would start anyway.
    if _predicate(cancelled):
        return _failed_attempt(
            key, spec, workspace, prior,
            "cancelled before the worker started", pre_prompt_failure=False,
            cancelled=True, env_receipt=_env_receipt_of(runner))

    prior_cursor = prior.cursor if prior is not None else 0
    prior_events = len(prior.events) if prior is not None else 0
    with _CancelWatcher(runner, cancelled) as watcher:
        try:
            if prior is not None:
                snapshot = runner.resume(prior, prompt, timeout_s=timeout_s)
            else:
                snapshot = runner.start(prompt, workspace, timeout_s=timeout_s)
        except (FileNotFoundError, RunnerUnavailableError) as exc:
            # `_executable()` resolves the CLI while building argv, so a missing
            # binary raises here rather than being represented in a snapshot.
            return _failed_attempt(key, spec, workspace, prior, _error_text(exc),
                                   pre_prompt_failure=not watcher.fired,
                                   env_receipt=_env_receipt_of(runner))
        except Exception as exc:
            # Everything else that escapes start/resume happens before a child
            # exists too (a billing override, a bad argument, a busy runner), but
            # only the two cases above are "this worker is absent" — a billing
            # override, for instance, would apply to the next worker just the
            # same, so retrying it would only produce a second identical refusal.
            return _failed_attempt(key, spec, workspace, prior, _error_text(exc),
                                   pre_prompt_failure=False,
                                   env_receipt=_env_receipt_of(runner))

    return _Attempt(
        key, spec, snapshot,
        pre_prompt_failure=_never_reached_the_prompt(
            snapshot, prior_cursor, prior_events, watcher.fired),
        reason=snapshot.error,
        env_receipt=_env_receipt_of(runner))


def _failed_attempt(key: str, spec: HarnessSpec | None, workspace: str,
                    prior: RunnerSnapshot | None, error: str, *,
                    pre_prompt_failure: bool, cancelled: bool = False,
                    env_receipt: dict | None = None) -> _Attempt:
    """A failure with no snapshot of its own, given the same shape as one.

    ``mutation_check_complete`` stays False: no before/after digest pair was ever
    taken, so the receipt reports ``mutated=None`` — "nobody looked" — instead of
    the more comfortable False.
    """
    now = time.time()
    snapshot = RunnerSnapshot(
        runner=key, workspace=workspace,
        thread_id=prior.thread_id if prior is not None else "",
        cursor=prior.cursor if prior is not None else 0,
        events=tuple(prior.events) if prior is not None else (),
        usage=dict(prior.usage) if prior is not None else {},
        settled=False, exit_code=None, error=redact_text(error),
        recovery_required=False, mutated=False, mutation_check_complete=False,
        final_output=prior.final_output if prior is not None else "",
        cancelled=cancelled,
        invocation=(prior.invocation if prior is not None else 0) + 1,
        started_at=now, finished_at=now)
    return _Attempt(key, spec, snapshot, pre_prompt_failure=pre_prompt_failure,
                    reason=error, env_receipt=env_receipt)


def _never_reached_the_prompt(snapshot: RunnerSnapshot, prior_cursor: int,
                              prior_events: int, cancel_fired: bool) -> bool:
    """Did this turn fail *before* the worker could read the prompt? (§D.6)

    Every clause is a way of asking "is there any trace of the child having
    run?", and the answer has to be no for all of them.  Being conservative is
    the point: a false positive here starts a second worker on a workspace the
    first one may have touched, so an ambiguous failure is treated as post-start
    and simply returned.

    A start gate that refused (``cancelled`` with no events) counts only when the
    *caller* never asked to cancel — otherwise this is the user's own cancel, and
    quietly starting a different worker after it would be the opposite of what
    they pressed.
    """
    if cancel_fired or snapshot.settled:
        return False
    if snapshot.timed_out or snapshot.mutated or snapshot.recovery_required:
        return False
    if not snapshot.mutation_check_complete:
        return False        # the workspace was never compared: assume it changed
    if snapshot.cursor != prior_cursor or len(snapshot.events) != prior_events:
        return False        # the child spoke protocol, so it had the prompt
    if snapshot.cancelled:
        return True         # start gate refused the launch; no request was sent
    # No exit status at all: the transport raised before a child existed.  A
    # child that ran and failed always brings back a status.
    return snapshot.exit_code is None


# --- cancellation -----------------------------------------------------------
class _CancelWatcher:
    """Poll the caller's cancel predicate and relay it to the runner.

    Deliberately powerless: it can call ``cancel_current()`` and nothing else.
    The runner owns the process tree and is the only thing that can prove the
    tree is gone, so a watcher that reached for a pid would be making a claim it
    cannot back up.

    It keeps asking until the runner confirms, which is what closes the launch
    race: a cancel arriving between ``start()`` and process registration finds
    nothing active on the first call and would otherwise be dropped, letting a
    cancelled turn run to completion.
    """

    def __init__(self, runner: Any, predicate: Callable[[], bool] | None,
                 poll_s: float = CANCEL_POLL_S):
        self._runner = runner
        self._predicate = predicate
        self._poll_s = max(0.01, float(poll_s))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.fired = False          # the predicate went True at least once

    def __enter__(self) -> "_CancelWatcher":
        if self._predicate is None:
            return self             # no watcher asked for, no thread started
        self._thread = threading.Thread(target=self._loop, name="collie-runner-cancel",
                                        daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc_info: Any) -> bool:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            # cancel_current() may itself be waiting up to 5s for extinction.
            thread.join(timeout=15.0)
        return False

    def _loop(self) -> None:
        while not self._stop.is_set():
            if _predicate(self._predicate):
                self.fired = True
                try:
                    if bool(self._runner.cancel_current()):
                        return      # the runner confirmed; nothing left to ask
                except Exception:
                    # A cancel that failed is retried on the next tick; raising
                    # out of a daemon thread would only print a traceback nobody
                    # can act on and would leave the turn running anyway.
                    pass
            self._stop.wait(self._poll_s)


def _predicate(predicate: Callable[[], bool] | None) -> bool:
    """``predicate()``, with a broken watcher treated as "not cancelled".

    The caller's predicate reads shared state (a web request table, a Mission
    row).  If that read throws, the honest reading is "we do not know", and
    killing a healthy turn on "we do not know" would lose real work.
    """
    if predicate is None:
        return False
    try:
        return bool(predicate())
    except Exception:
        return False


# --- result and receipt -----------------------------------------------------
def _finish(decision: HarnessDecision, attempt: _Attempt,
            prior: RunnerSnapshot | None, emit: Callable[[str, dict], Any] | None,
            *, fallback_from: str, model: str, provider: str,
            task_id: str) -> RunResult:
    """Project the final snapshot onto a RunResult and a RunnerReceipt."""
    key = attempt.key
    spec = attempt.spec or registry.SPECS.get(key)
    snapshot = attempt.snapshot
    probe = _probe_for(key, decision)

    reported_model = _reported_model(snapshot)
    # What the row says answered: the worker's own word when it gave one, else
    # what we asked for.  The receipt is stricter (see below).
    effective_model = reported_model or str(model or "")

    if spec is None:
        # Only reachable for a key that is not in SPECS at all; the run already
        # failed, and a receipt still has to name what it failed as.
        spec = HarnessSpec(key=key, label=key, kind="external",
                           caps=RunnerCapabilities(protocol=""))

    result = _run_result(snapshot, spec, probe, decision,
                         model=effective_model, provider=provider, task_id=task_id)

    usage = usage_to_collie(key, dict(snapshot.usage or {}))
    prior_cursor = prior.cursor if prior is not None else 0
    replayed = [event for event in snapshot.events if event.cursor > prior_cursor]
    for event in replayed[-_MAX_REPLAYED_EVENTS:]:
        # Phase 1 replays the worker's own event names; phase 2's `runner_events`
        # is what maps them onto Collie's canonical vocabulary.  Emitting them
        # under an invented canonical name now would be a translation nobody
        # tested.
        _safe_emit(emit, "runner", {
            "event": "native", "runner": key, "cursor": event.cursor,
            "type": event.type, "payload": redact_value(event.payload),
        })

    receipt = RunnerReceipt(
        runner=key,
        runner_version=probe.version,
        runner_protocol=spec.caps.protocol,
        runner_protocol_version=spec.caps.protocol_version,
        billing_class=probe.billing_class,
        billing_mode=probe.billing_mode,
        credential_family=spec.credential_family,
        decision=decision.to_dict(),
        native_session=NativeSessionRef.from_snapshot(
            snapshot, protocol_version=spec.caps.protocol_version).to_dict(),
        usage=usage.to_dict(),
        usage_known=usage.known,
        cost_usd_reported=usage.cost_usd_reported,
        # Priced on `effective_model` so the receipt and the runs.db row agree;
        # `model` below stays empty when the worker never said, because a receipt
        # that names a model on our say-so is exactly the comfortable lie §C
        # forbids.
        cost_usd_equivalent=equivalent_cost_usd(effective_model, usage),
        model=reported_model,
        settled=bool(snapshot.settled),          # NOT verified — the host verifies
        recovery_required=bool(snapshot.recovery_required),
        mutated=bool(snapshot.mutated) if snapshot.mutation_check_complete else None,
        events_digest=_events_digest(snapshot.events),
        event_count=len(snapshot.events),
        approvals=(),                            # phase 3: no round trip exists yet
        env_receipt=attempt.env_receipt,
        fallback_from=fallback_from,
        error=snapshot.error,
    )
    setattr(result, RECEIPT_ATTR, receipt)
    _safe_emit(emit, "receipt", receipt.to_dict())
    return result


def _run_result(snapshot: RunnerSnapshot, spec: HarnessSpec, probe: RunnerProbe,
                decision: HarnessDecision, *, model: str, provider: str,
                task_id: str) -> RunResult:
    """``snapshot_to_run_result`` with the decision attached only when it fits.

    After a fallback the decision names a different worker than the one that ran,
    and ``snapshot_to_run_result`` rejects that pairing on purpose — mislabelling
    which account paid is a crash there, not a footnote.  The fallback is
    recorded in the receipt instead.
    """
    matching = decision if decision.runner == spec.key else None
    return snapshot_to_run_result(snapshot, spec, probe, decision=matching,
                                  model=model, provider=provider, task_id=task_id)


def _probe_for(key: str, decision: HarnessDecision) -> RunnerProbe:
    """The probe that describes ``key`` — reusing the decision's when it is that key.

    The selector already probed the chosen worker and froze the result into the
    decision; probing again would risk a *different* answer being recorded than
    the one the choice was made on.  A fallback worker was never probed for the
    receipt, so it is probed here (cached, metadata-only, never live).
    """
    if key == decision.runner and decision.probe:
        try:
            return RunnerProbe.from_dict(decision.probe)
        except Exception:
            pass
    try:
        return registry.probe(key)
    except Exception:
        return RunnerProbe(key=key, installed=False, detail="probe unavailable")


def _events_digest(events: Any) -> str:
    """sha256 over the bounded, redacted event stream — timestamps excluded.

    ``at`` is wall clock, so including it would make two replays of the identical
    stream digest differently and the field would answer no question at all.
    """
    rows = [{"cursor": event.cursor, "type": event.type,
             "payload": redact_value(event.payload)}
            for event in (events or ())]
    return stable_digest(rows)


def _reported_model(snapshot: RunnerSnapshot) -> str:
    """The model the worker itself named, or "" — never what we asked for.

    Phase-1 dialects mostly do not report one; the newest event that does wins,
    since a resumed thread can carry older events from an earlier model.
    """
    for event in reversed(tuple(snapshot.events or ())):
        payload = event.payload if isinstance(event.payload, dict) else {}
        name = payload.get("model")
        if isinstance(name, str) and name.strip():
            return name.strip()
        name = _dominant_model(payload.get("modelUsage"))
        if name:
            return name
    return ""


def _dominant_model(model_usage: Any) -> str:
    """The model that did the work, out of Claude's per-model cost breakdown.

    `claude -p --output-format json` has no top-level "model", but it does carry
    `modelUsage: {"<id>": {..., "costUSD": …, "canonicalModel": …}}`.  A single
    run routinely lists two — a small one for internal steps and the one that
    actually answered — so the most expensive entry is the honest answer for a
    receipt line that says which Brain ran.  Measured against claude 2.1.221 on
    2026-08-22, where a one-file edit reported claude-haiku-4-5 alongside the
    session model.  Without this the receipt shows an empty model for a run that
    knew perfectly well which one it used.
    """
    if not isinstance(model_usage, Mapping) or not model_usage:
        return ""
    best_name, best_cost = "", None
    for raw_name, entry in model_usage.items():
        if not isinstance(entry, Mapping):
            continue
        cost = entry.get("costUSD")
        cost = float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else -1.0
        canonical = entry.get("canonicalModel")
        name = canonical if isinstance(canonical, str) and canonical.strip() else str(raw_name)
        if best_cost is None or cost > best_cost:
            best_name, best_cost = name.strip(), cost
    return best_name


def _env_receipt_of(runner: Any) -> dict:
    """``{"allowed": [names], "stripped": [names]}`` from the runner — names only.

    Values never appear: that is the whole reason ``child_env`` returns a receipt
    separate from the environment it built.
    """
    receipt = getattr(runner, "last_env_receipt", None)
    if not isinstance(receipt, Mapping):
        return {"allowed": [], "stripped": []}
    return {str(name): [str(item) for item in (values or ())]
            for name, values in receipt.items()}


# --- inputs -----------------------------------------------------------------
def _attempt_chain(decision: HarnessDecision, *, resuming: bool) -> tuple[str, ...]:
    """The chosen worker, then the fallbacks that are still worth trying.

    ``decision.fallback_chain`` is already restricted to the same credential
    family and billing class (and is empty for a pinned or configured worker, so
    an explicit ``--runner`` never silently becomes another one).  Two entries
    are dropped here anyway: ``collie``, which is not something this module can
    launch, and any repeat of the chosen worker.

    Resuming has no chain at all — a thread locator is one worker's private
    session id.
    """
    chain = [decision.runner]
    if not resuming:
        for key in decision.fallback_chain:
            if key and key != "collie" and key not in chain:
                chain.append(key)
    return tuple(chain)


def _resume_snapshot(resume_from: Any, runner: str,
                     workspace: str) -> RunnerSnapshot | None:
    """Turn whatever the caller kept from last turn into a resumable snapshot.

    Phase 1 stores only a ``NativeSessionRef`` in the session receipt, so the
    common input is a bare locator string.  What is lost with it is the cursor
    and the cumulative usage, not the thread: the worker still continues its own
    conversation, and this turn's usage is simply counted from zero.
    """
    if resume_from is None:
        return None
    if isinstance(resume_from, RunnerSnapshot):
        return resume_from
    if isinstance(resume_from, Mapping):
        value = dict(resume_from)
        if "locator" in value and "thread_id" not in value:
            # A NativeSessionRef.to_dict(): id and path, by design nothing else.
            value = {"runner": value.get("runner") or runner,
                     "workspace": value.get("workspace") or workspace,
                     "thread_id": value.get("locator")}
        value.setdefault("runner", runner)
        value.setdefault("workspace", workspace)
        snapshot = RunnerSnapshot.from_dict(value)
        return snapshot if snapshot.thread_id else None
    locator = str(resume_from or "").strip()
    if not locator:
        return None
    # from_dict canonicalizes the workspace exactly as the runners' own
    # `_workspace` does, which `ClaudeCodeRunner.resume` insists on.
    return RunnerSnapshot.from_dict(
        {"runner": runner, "workspace": workspace, "thread_id": locator})


def _prompt_text(task: str, history_note: str | None) -> str:
    """The prompt handed to the worker: the task, optionally after a recap.

    The recap is the caller's summary of the previous turn, which an external
    worker cannot see — it has no access to Collie's message history and, on a
    fresh thread, no memory of it either.
    """
    text = str(task or "").strip()
    if not text:
        raise ValueError("task must be a non-empty string")
    note = str(history_note or "").strip()
    if not note:
        return text
    return "Earlier in this session:\n%s\n\n---\n\nTask:\n%s" % (note, text)


def _canonical_workspace(workspace: str) -> str:
    """The workspace as the runners canonicalize it, checked before launching.

    Failing here means no worker was built and no environment was assembled; the
    same mistake found three frames deeper would already have touched both.
    """
    if not isinstance(workspace, str) or not workspace.strip() or "\x00" in workspace:
        raise ValueError("workspace must be a non-empty path")
    root = os.path.realpath(os.path.abspath(workspace))
    if not os.path.isdir(root):
        raise ValueError("workspace does not exist or is not a directory: %s" % root)
    return root


def _label_of(key: str) -> str:
    spec = registry.SPECS.get(key)
    return spec.label if spec is not None else key


def _error_text(exc: BaseException) -> str:
    """``TypeName: message``, redacted — CLI text can quote an environment."""
    return redact_text("%s: %s" % (type(exc).__name__, exc))


def _safe_emit(emit: Callable[[str, dict], Any] | None, kind: str,
               payload: dict) -> None:
    """Best-effort event delivery: a dead consumer never fails a live run."""
    if emit is None:
        return
    try:
        emit(kind, payload)
    except Exception:
        pass


__all__ = ["CANCEL_POLL_S", "RECEIPT_ATTR", "receipt_of", "run_adhoc"]
