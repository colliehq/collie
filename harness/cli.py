"""collie CLI — the entrypoint you (or Claude) invoke to run and test the harness.

    python -m harness.cli selftest          # $0 deterministic end-to-end (mock)
    python -m harness.cli run "<task>" [--provider mock|anthropic] [--model M]
    python -m harness.cli compare [--cc off|baseline|real] [--provider ...]
    python -m harness.cli dashboard         # rebuild data/dashboard.html
    python -m harness.cli mem search "<q>"  |  mem add "<text>"
"""
from __future__ import annotations
import argparse
import json
import os
import re
import subprocess     # module-level: cmd_uninstall (tccutil) and _collie_procs (ps) call it bare,
                      # inside except-blocks that were silently swallowing the NameError
import sys
import tempfile

from . import __version__
from .providers import make_provider
from .embeddings import make_embedding
from .memory import SqliteMemory
from .tools import default_registry
from .context import ContextComposer, TokenBudgeter
from .recorder import Recorder
from .loop import Harness
from . import compare as cmp
from . import dashboard as dash

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _data_dir() -> str:
    """Where collie keeps what the user made: sessions, memory.db, runs.db, the sandbox.

    This used to be `<wherever harness is installed>/data`, which is fine for a checkout and wrong
    everywhere else. Installed from the .app it resolved INSIDE the bundle — read-only, so nothing
    could be saved at all (the app showed "no chats yet" forever), and had it been writable every
    update would have replaced the bundle and taken the history with it. From pip it landed in
    site-packages, which the next upgrade deletes.

    A checkout keeps using its own `data/`, so a dev box and the test suite see what they always saw
    and nothing existing is orphaned. Anything else writes beside the rest of the user's collie
    state.
    """
    override = os.environ.get("COLLIE_DATA_DIR")
    if override:
        return override
    # Explicit state is an isolation boundary even from a source checkout.  Ignoring it here made
    # benchmarks, tests, and parallel supervisors silently share the repository's memory/runs DB.
    state_env = os.environ.get("COLLIE_STATE_DIR")
    if state_env:
        return os.path.join(state_env, "data")
    if os.path.exists(os.path.join(ROOT, "pyproject.toml")):     # a source checkout, not an install
        return os.path.join(ROOT, "data")
    state = os.path.expanduser("~/.collie")
    new = os.path.join(state, "data")
    # Only rescue into the DEFAULT store. The rescue is a move, not a copy, so running it against an
    # explicitly requested directory empties ROOT/data into it — asking for an isolated run would
    # relocate the repository's own memory, runs and benchmark instance lists, and deleting that
    # scratch directory afterwards would take them with it. Observed exactly once, on the first
    # isolated run after COLLIE_STATE_DIR started being honoured here; the data was recovered.
    _migrate_legacy_data(os.path.join(ROOT, "data"), new)        # rescue pre-0.20.13 install data (once)
    return new


def _migrate_legacy_data(old: str, new: str) -> None:
    """One-time rescue of pre-0.20.13 data. Those installs kept sessions/memory.db/runs.db under
    <install>/data — writable on the Windows installer, so real history accumulated there. The move to
    ~/.collie/data (v0.20.13) would otherwise ORPHAN it — and a later uninstall deletes {app} outright,
    destroying it. If the new store has no real content and the legacy one does, move it across once.
    Best-effort and idempotent: a migration failure must never stop collie from starting."""
    try:
        if os.path.abspath(old) == os.path.abspath(new):
            return

        def _has(d):
            try:
                with os.scandir(d) as it:
                    return any(True for _ in it)
            except OSError:
                return False

        if not _has(old) or _has(new):        # nothing to rescue, or the new store is already in use
            return
        import shutil
        os.makedirs(os.path.dirname(new), exist_ok=True)
        if os.path.exists(new):               # new dir exists but empty — move each child in
            for name in os.listdir(old):
                dst = os.path.join(new, name)
                if not os.path.exists(dst):
                    shutil.move(os.path.join(old, name), dst)
        else:
            shutil.move(old, new)             # rename the whole legacy data dir into place
    except Exception:
        pass


DATA = _data_dir()


def _paths():
    os.makedirs(DATA, exist_ok=True)
    return (os.path.join(DATA, "memory.db"),
            os.path.join(DATA, "runs.db"),
            os.path.join(DATA, "dashboard.html"),
            os.path.join(DATA, "sandbox"))


def _embedder(embed="auto"):
    """Resolve the memory embedder. auto -> granite (in-process, no daemon) if its ONNX deps are
    present, else None = BM25-only (NEVER HashEmbedding — measured worse than BM25). A named backend
    (COLLIE_EMBED=granite|bge-m3|e5|jina|hash|onnx:<repo>|...) is honored as-is.

    Returns an EmbeddingProvider or None (None => the memory pipeline runs sparse-only)."""
    embed = os.environ.get("COLLIE_EMBED", embed)
    if embed in ("bm25", "none", "off", "sparse"):
        return None
    if embed in ("auto", "granite", "local", "default", "daemon"):   # "daemon" = legacy alias
        # First run must NOT block on a multi-hundred-MB model download. If granite isn't on disk
        # yet, download it in the BACKGROUND and run this session BM25-only; the next run finds it
        # cached and gets full semantic memory instantly. Once cached, build in-line (fast).
        try:
            from .embeddings import granite_cached, warm_async
            if not granite_cached():
                if warm_async("granite"):
                    print("  [embed] downloading the semantic-memory model in the background — "
                          "this run uses BM25 (keyword) retrieval; the next run will be fully "
                          "semantic. No need to wait.", file=sys.stderr)
                return None
        except Exception:
            pass                                          # fall through to the normal (blocking) build
        try:
            return make_embedding("granite")
        except Exception as e:
            # stderr, NOT stdout — `run --json`/`--stream-json` promise machine-readable stdout.
            # This fires when onnxruntime/tokenizers aren't installed or the model can't download.
            if isinstance(e, ImportError):
                why, fix = ("onnxruntime/tokenizers not installed",
                            "pip install collie-harness[local]   (or run: collie setup)")
            else:
                why = "%s: %s" % (type(e).__name__, str(e)[:100])
                fix = ("model download failed (huggingface.co + hf-mirror.com) — check the network, "
                       "or set an intranet mirror: COLLIE_HF_ENDPOINT=<url>")
            print("  [embed] semantic memory unavailable (%s) -> BM25-only (keyword retrieval)\n"
                  "  [embed] enable it: %s" % (why, fix), file=sys.stderr)
            return None
    return make_embedding(embed)


def apply_persona(h, gate, name, cwd):
    """Put a persona on a built harness: its identity text, its tool allowlist, and the
    stricter of its mode and the user's. Returns the Persona, or None if there is no such
    file — a missing persona is reported, never silently ignored, because running the
    wrong role is worse than not running."""
    from .personas import load
    p = load(name, cwd)
    if p is None:
        return None
    if gate is not None:
        gate.mode = p.effective_mode(gate.mode)   # narrowing only — see personas.py
    dropped = p.apply_tools(h.registry)
    if p.prompt:
        try:
            base = getattr(h.composer, "identity", "") or ""
            h.composer.identity = (base + "\n\n" + p.prompt).strip()
        except AttributeError:
            pass
    print("  [persona] %s · mode %s%s" % (
        p.name, gate.mode.value if gate is not None else "-",
        " · %d tools withheld" % dropped if dropped else ""))
    return p


def default_gate(cwd, mode=None, commands=None):
    """The gate a user-facing surface runs behind. Mode from COLLIE_MODE, else `project`
    (writes and commands inside cwd are covered by the fact that you launched collie
    here; anything reaching off this machine is asked).

    Deliberately NOT the default inside make_harness: benchmarks, `pack` and the delegate
    child build harnesses through the same function, and a gate there would change what
    those measure. Surfaces with a human attached opt in; measurement paths stay as they
    were."""
    from .gate import Gate, Mode, mode_from_env
    from .settings import get as _sget
    from .trust import repo_allowed_commands
    allowed = []
    raw = (os.environ.get("COLLIE_ALLOW_COMMANDS") or _sget("ALLOW_COMMANDS", "") or "").strip()
    if raw:
        allowed = [c.strip() for c in raw.split(",") if c.strip()]
    allowed += [str(c).strip() for c in (commands or []) if str(c).strip()]
    # …plus whatever this repo asks for — but only once the user has trusted this exact
    # directory (`collie trust`). Cloning a repository is not the same act as believing it.
    allowed += repo_allowed_commands(cwd)
    chosen = Mode(mode) if mode else mode_from_env()   # an explicit flag beats the environment
    g = Gate(cwd=cwd, mode=chosen, allowed_commands=allowed)
    try:                       # user-local risk overrides (mainly to relax MCP's default)
        from .overrides import RiskOverrideStore
        g.risk_overrides = RiskOverrideStore().resolver()
    except ImportError:
        pass
    try:                       # live page origin, for (tool, origin) rules — never cached
        from .browserbridge import current_origin
        g.origin_lookup = current_origin
    except ImportError:
        pass
    return g


def make_harness(cwd, provider="mock", model=None, project="demo",
                 embed="auto", prefix_ceiling=6000, code_search=False,
                 rerank=None, distill=None, web_search=None, exec_code=False, delegate=False,
                 gate=None, effort=None, speed="standard"):
    from .embeddings import make_reranker
    from .distill import make_distiller
    mem_db, runs_db, _, _ = _paths()
    rr = make_reranker(rerank or os.environ.get("COLLIE_RERANK"))   # opt-in cross-encoder
    ds = make_distiller(distill or os.environ.get("COLLIE_DISTILL"))  # opt-in extraction
    memory = SqliteMemory(mem_db, embedder=_embedder(embed), reranker=rr, distiller=ds)
    if web_search is None:
        web_search = os.environ.get("COLLIE_WEBSEARCH", "") in ("1", "on", "true")
    registry = default_registry(code_search=code_search, web_search=web_search,
                                exec_code=exec_code, delegate=delegate)
    # COLLIE_IDENTITY lets a PARENT process say who this run is, which a subprocess otherwise has
    # no way to be told: `collie slack` names each dog and then had to shell out to `collie run`,
    # so the name lived in the Slack message tag and nowhere the model could see it — asked "Hi
    # Cornetto" the dog introduced itself as collie and corrected the person. Empty keeps the
    # default identity exactly as it was (ContextComposer falls back on ""), so nothing changes
    # for anyone who does not set it.
    composer = ContextComposer(memory, registry, TokenBudgeter(prefix_ceiling),
                               identity=os.environ.get("COLLIE_IDENTITY", ""))
    recorder = Recorder(runs_db)
    prov = make_provider(provider, model, effort=effort, speed=speed)
    h = Harness(prov, memory, registry, composer, recorder, cwd=cwd, project=project)
    # Run presets may choose a lower everyday/deep-work target, but this value is a user-owned
    # HARD ceiling.  Keep it separately from ``max_turns`` so selecting Thorough can never turn a
    # five-turn safety limit into a forty-eight-turn run.
    h._max_turns_hard_cap = None
    h.gate = gate                             # None = ungated (benchmarks, delegate child, embedded)
    if gate is not None:                      # record decisions only where there is a gate making them
        try:
            from .audit import AuditLog
            h.audit = AuditLog()
        except Exception:
            pass                              # a read-only home must not stop a run
    try:                                      # Settings-panel turn limit (env/JSON), else keep default
        mt = os.environ.get("COLLIE_MAX_TURNS")
        if mt:
            h.max_turns = max(1, min(120, int(mt)))
            h._max_turns_hard_cap = h.max_turns
    except (TypeError, ValueError):
        pass
    return h


def normalize_run_options(intent="build", quality="balanced", verification="auto"):
    """Validate and canonicalize the three harness-local run axes."""
    intent = str(intent or "build").strip().lower()
    quality = str(quality or "balanced").strip().lower()
    verification = str(verification or "auto").strip().lower()
    if intent not in ("build", "plan", "test", "review"):
        raise ValueError("intent must be build, plan, test, or review")
    if quality not in ("quick", "balanced", "thorough"):
        raise ValueError("quality must be quick, balanced, or thorough")
    if verification not in ("auto", "required"):
        raise ValueError("verification must be auto or required")
    return {"intent": intent, "quality": quality, "verification": verification}


def configure_run_options(h, intent="build", quality="balanced", verification="auto"):
    """Apply the web/product run axes to a harness without conflating their meanings.

    ``intent`` controls tool authority and prompt role (the caller still owns the Gate),
    ``quality`` controls how much room the loop gets, and ``verification`` controls whether an
    executed post-edit assertion is a hard finish condition.  Workspace isolation and Pack are
    deliberately absent: they decide *where/how many* harnesses run, not how this one reasons.

    The strict validation is intentional.  A misspelled URL option must not silently weaken a
    requested verification gate or turn a read-only plan into a writable build.
    """
    options = normalize_run_options(intent, quality, verification)
    intent = options["intent"]
    quality = options["quality"]
    verification = options["verification"]

    # ContextComposer tells the model the same boundary that Gate enforces.  The prompt is useful
    # guidance; Gate is the authority, so a Plan remains read-only even if the model ignores it.
    h.mode = "act" if intent == "build" else intent
    if intent in ("plan", "test", "review"):
        h.force_edit = False
        # These intents never repair by writing. Test evidence is executed by its
        # restricted gate/post-check; Plan and Review are inspection artifacts.
        h.self_verify = False

    # These are preset TARGETS, never permission to widen the Settings-panel hard cap. Applying the
    # Balanced target here too is important: Pack builds harnesses directly and used to leave both
    # Balanced and Thorough at Harness's 50-turn default, making its quality selector a no-op.
    target_turns = {"quick": 24, "balanced": 40, "thorough": 50}[quality]
    hard_cap = getattr(h, "_max_turns_hard_cap", None)
    h.max_turns = min(target_turns, int(hard_cap)) if hard_cap is not None else target_turns

    # Thorough is the honest successor to the old "Extreme Herding" depth preset.  It also buys
    # additional repair room. It does not itself claim that a check passed; that is the independent
    # verification axis below.
    if quality == "thorough":
        h.verify_max = max(int(getattr(h, "verify_max", 2) or 2), 4)

    if verification == "required":
        # Required is a product contract, not a hint. A reused/custom Harness may have disabled the
        # ordinary advisory self-check; turn it back on so the hard gate below cannot be bypassed.
        h.self_verify = True
        h.verify_gate = True
        h.require_assert = True
        h.verify_max = max(int(getattr(h, "verify_max", 2) or 2), 4)

    return options


_TURN_OPTION_FIELDS = (
    "mode", "force_edit", "self_verify", "max_turns", "verify_max",
    "verify_gate", "require_assert",
)


def configured_model_for(provider, requested_model=None, provider_was_explicit=False):
    """Return the model pin that applies to an interactive surface.

    A saved model belongs to its saved provider.  Passing ``--provider`` for a
    different account must not carry an unrelated model across that credential
    boundary.  ``None`` deliberately means Auto; the per-turn router will then
    choose a model *inside* ``provider``.
    """
    if requested_model is not None:
        return str(requested_model).strip() or None
    from . import settings
    saved_provider = settings.get("PROVIDER", provider) or provider
    if provider_was_explicit and provider != saved_provider:
        return None
    return settings.get("MODEL", "") or None


def resolve_turn_decision(text, provider, configured_model=None, history=None, receipts=None,
                          route_kind=None):
    """Resolve the shared per-turn policy used by terminal/editor conversations.

    ``receipts`` supplies structured failure truth.  Model error text is not
    guaranteed to appear in the assistant transcript, so relying on messages
    alone made the advertised failure escalation mostly synthetic.
    """
    from . import settings
    from .router import resolve_run_decision

    routing_history = list(history or [])
    for row in list(receipts or [])[-4:]:
        if not isinstance(row, dict):
            continue
        evidence = row.get("verification_evidence")
        failed_check = isinstance(evidence, dict) and evidence.get("passed") is False
        if row.get("error") or failed_check:
            # A fixed token is enough for router._FAILURE and avoids copying
            # tool output, prompts, or other private receipt details.
            routing_history.append({"role": "assistant", "content": "error: verification failed"})

    return resolve_run_decision(
        text, provider=provider, model=configured_model,
        effort=settings.get("REASONING_EFFORT", "auto") or "auto",
        speed="standard", route_kind=route_kind,
        intent="build", quality="balanced", verification="auto",
        explicit_axes=(), history=routing_history,
    )


def apply_turn_decision(h, decision, gate=None):
    """Apply a RunDecision to a reused Harness without recreating its stores.

    Provider/model/effort may change between turns, but memory, recorder,
    composer, registry, checkpoint scope, and conversation history remain on the
    same Harness.  Run-option fields are reset to their original values first so
    a read-only Plan or Required-verification turn cannot leak into the next one.
    """
    if not hasattr(h, "_turn_option_baseline"):
        defaults = {
            "mode": "act", "force_edit": False, "self_verify": True,
            "max_turns": 50, "verify_max": 2, "verify_gate": False,
            "require_assert": False,
        }
        h._turn_option_baseline = {
            key: getattr(h, key, defaults[key]) for key in _TURN_OPTION_FIELDS
        }
    for key, value in h._turn_option_baseline.items():
        setattr(h, key, value)

    configure_run_options(
        h, intent=decision.intent, quality=decision.quality,
        verification=decision.verification,
    )

    gate = gate if gate is not None else getattr(h, "gate", None)
    if gate is not None:
        from .gate import Mode
        if not hasattr(h, "_turn_gate_baseline"):
            h._turn_gate_baseline = gate.mode
        narrowed = {
            "plan": Mode.PLAN, "review": Mode.REVIEW, "test": Mode.TEST,
        }.get(decision.intent)
        gate.mode = narrowed or h._turn_gate_baseline

    signature = (decision.provider, decision.model, decision.effort, decision.speed)
    current = getattr(h, "provider", None)
    current_signature = (
        getattr(current, "name", ""), getattr(current, "model", ""),
        getattr(current, "effort", "default"), getattr(current, "speed", "standard"),
    )
    if getattr(h, "_turn_provider_signature", None) != signature:
        if current_signature != signature:
            h.provider = make_provider(
                decision.provider, decision.model,
                effort=decision.effort, speed=decision.speed,
            )
        h._turn_provider_signature = signature
    h.run_decision = decision.to_dict()
    return decision


def turn_decision_receipt(decision, res, provider=None):
    """Compact structured outcome used both for UI receipts and next-turn routing."""
    active = provider
    return {
        "decision": decision.to_dict(),
        "model": getattr(res, "model", "") or decision.model,
        "actual_speed": getattr(active, "actual_speed", decision.speed),
        "verified": bool(getattr(res, "verified", False)),
        "verification_evidence": getattr(res, "verification_evidence", None),
        "error": getattr(res, "error", "") or "",
    }


# --------------------------------------------------------------------------- #
def cmd_selftest(args):
    mem_db, runs_db, out_html, sandbox = _paths()
    os.makedirs(sandbox, exist_ok=True)
    facts = cmp.build_sandbox(sandbox)

    h = make_harness(sandbox, provider="mock", project="demo")
    # seed a durable design decision so the recall task has something to find
    h.memory.remember(
        "We decided to internalize embeddings: local bge-m3 via fastembed feeding "
        "sqlite-vec + FTS5 hybrid retrieval, so memory recall is $0 and fast.",
        keys="embedding memory design bge-m3 hybrid", project="demo")
    h.memory.set_block("project:demo", "goal",
                       "Build an evolvable coding harness; beat Claude Code on prefix tokens.",
                       char_limit=200)

    print("== collie selftest (mock provider, $0) ==")
    results = []
    for task in cmp.task_suite(facts, full=False):
        res = cmp.run_collie(h, task)
        results.append(res)
        print("  [%s] %-14s prefix=%-5d turns=%d tools=%d recall=%d %dms  ->  %s" % (
            "PASS" if res.success else "FAIL", task["id"], res.prefix_tokens,
            res.turns, res.tool_calls, res.mem_recalls, res.wall_ms,
            (res.answer or res.error)[:60].replace("\n", " ")))

    cmp.cc_baseline(h.recorder)   # reference prefix row for the dashboard
    dash.build(runs_db, out_html)
    npass = sum(r.success for r in results)
    print("\n  %d/%d tasks passed · memory facts=%d" % (
        npass, len(results), h.memory.count("demo")))
    print("  dashboard -> %s" % out_html)
    h.memory.close(); h.recorder.close()
    return 0 if npass == len(results) else 1


def cmd_loop(args):
    """Autonomous goal-directed loop: pin a goal, iterate the agent (memory carried across
    iterations), stop when an executed check passes (--until) or after --max iterations.
    On brand with collie's executed-verification identity — the loop ends on real green, not
    the model's say-so."""
    import subprocess as _sp
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    h = make_harness(cwd, provider=provider, model=args.model, project=args.project,
                     code_search=True, web_search=True, exec_code=True, delegate=True)
    goal = args.goal or args.task
    if goal:
        h.memory.set_block("project:" + args.project, "goal", goal[:390], char_limit=400)
    task = args.task or ("Make progress toward the goal above. Do one concrete step this turn.")
    stopped = False
    run_failed = False
    history = None
    h.defer_memory_promotion = bool(args.until)
    try:
        for i in range(args.max):
            print("\n── collie loop · iteration %d/%d ──" % (i + 1, args.max), flush=True)
            res = h.run("loop", task, consolidate=True, history=history)
            # Pending/rejected durable claims stay outside global recall, but the current loop must
            # still remember its concrete progress. Carry the bounded/elided transcript directly.
            history = getattr(res, "messages", None) or history
            print(res.answer or res.error or "(no output)", flush=True)
            # A later successful iteration must not erase an earlier model/provider failure.
            run_failed = run_failed or bool(res.error)
            if args.until:
                from . import plat
                _uargs, _ush = plat.shell_argv(args.until)   # POSIX --until predicate on every OS
                rc = _sp.run(_uargs, shell=_ush, cwd=cwd).returncode
                print("  [until] `%s` → exit %d" % (args.until, rc), flush=True)
                until_evidence = {"kind": "loop_until", "command": args.until,
                                  "exit_code": rc, "passed": rc == 0}
                res.verified = bool(rc == 0 and not res.error)
                settle = getattr(h, "settle_run_memory", None)
                if callable(settle):
                    settle(res, bool(res.verified), until_evidence, source="loop_until")
                # run() persisted the in-loop verdict before this out-of-process predicate.
                finish = getattr(h.recorder, "finish_run", None)
                if callable(finish):
                    finish(res)
                if rc == 0:
                    print("✓ goal condition met — stopping."); stopped = True; break
        if not stopped and args.until:
            print("✗ reached --max %d without the goal condition passing." % args.max)
    finally:
        h.memory.close(); h.recorder.close()
    # An executed predicate is a contract, not an advisory progress meter. Reaching --max without
    # it (and JSON/automation invoking this command) must be able to fail a build reliably.
    return 0 if (stopped or (not args.until and not run_failed)) else 1


def cmd_repl(args):
    """Interactive REPL — a lightweight readline chat that keeps the FULL conversation thread
    across turns (and persists it as a session, so you can --resume later). collie's answer to
    'no interactive mode' without a heavy TUI: one input() loop over the same harness."""
    from . import sessions as sess
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    configured_model = configured_model_for(
        provider, args.model, provider_was_explicit=bool(args.provider))
    _gate = default_gate(cwd, getattr(args, "mode", None))
    h = make_harness(cwd, provider=provider, model=configured_model, project=args.project,
                     code_search=True, web_search=True, exec_code=True, delegate=True,
                     gate=_gate)
    from .approve import tty_approver
    h.approve = tty_approver(gate=_gate)
    sid = args.resume or (sess.latest() if getattr(args, "cont", False) else None) or sess.new_id()
    h.checkpoint_scope = "session:" + sid
    loaded = sess.load(sid) if (args.resume or getattr(args, "cont", False)) else None
    history = (loaded or {}).get("messages") or []
    receipts = list((loaded or {}).get("run_receipts") or [])
    if getattr(args, "goal", None):
        h.memory.set_block("project:" + args.project, "goal", args.goal[:390], char_limit=400)
    print("collie repl · session %s · %s · %d prior turns · /exit to quit, /new for a fresh thread"
          % (sid, provider, sum(1 for m in history if m.get("role") == "user")))
    try:
        while True:
            try:
                line = input("\n› ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/new":
                history, receipts, sid = [], [], sess.new_id()
                h.checkpoint_scope = "session:" + sid
                print("  [new session %s]" % sid)
                continue
            try:
                decision = resolve_turn_decision(
                    line, provider, configured_model=configured_model,
                    history=history, receipts=receipts)
                apply_turn_decision(h, decision, _gate)
            except Exception as e:
                print("\ncollie could not route this turn: %s: %s" % (type(e).__name__, e))
                continue
            print("  [decision] %s · %s · %s/%s/%s" % (
                decision.model, decision.effort, decision.intent,
                decision.quality, decision.verification))
            res = h.run("repl", line, consolidate=True, history=history)
            print("\n" + (res.answer or res.error or "(no output)"))
            history = res.messages
            receipt = turn_decision_receipt(decision, res, getattr(h, "provider", None))
            saved_sid = sess.save(
                sid, history, project=args.project, cwd=cwd, answer=res.answer or "")
            if saved_sid:
                try:
                    sess.append_run_receipt(sid, receipt)
                except Exception:
                    pass
            receipts.append(receipt)
    finally:
        h.memory.close(); h.recorder.close()
        print("\nsession saved: %s  ·  resume: collie repl --resume %s" % (sid, sid))
    return 0


def cmd_tui(args):
    """Rich terminal TUI — friendly interactive chat with a live tool/gate/diff timeline."""
    from .tui import run_tui
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    configured_model = configured_model_for(
        provider, args.model, provider_was_explicit=bool(args.provider))
    return run_tui(args.cwd or os.getcwd(), provider, configured_model, project=args.project,
                   resume=args.resume, cont=getattr(args, "cont", False), goal=args.goal)


def cmd_web(args):
    """Serve the local web GUI (streams the verification gate live over SSE)."""
    if getattr(args, "remote", False):
        return _cmd_web_remote(args)
    from .webapp import main as web_main
    argv = ["--port", str(args.port)]
    if not args.open:
        argv.append("--no-open")
    if getattr(args, "lan", False):
        argv.append("--lan")
    if getattr(args, "qr", False):
        argv.append("--qr")
    if getattr(args, "name", ""):
        argv += ["--name", str(args.name)]
    return web_main(argv)


def _print_qr(data: str):
    """Print a scannable ASCII QR of the pairing link to the terminal, so a phone can just scan the
    screen.

    Uses collie's own stdlib encoder (harness/qr.py) rather than `segno`: the core ships no
    dependencies, and an optional one meant this printed "pip install …" instead of a code on a
    plain install — precisely when someone is trying to pair a phone for the first time."""
    from . import qr
    try:
        print(qr.ansi(data), flush=True)
    except ValueError:
        # the encoder tops out at 106 bytes (v6-M); a longer link is still printed above as text
        print("  (link too long for a terminal QR — open it on the phone directly)", flush=True)


def _cmd_web_remote(args):
    """collie web --remote — run the local GUI server AND dial the public relay so a phone can
    drive this desktop from anywhere. The local server still binds 127.0.0.1 only; the relay client
    replays the phone's requests to it with the CSRF token injected (see harness/remote.py)."""
    import threading, time
    from . import webapp
    from .remote import RemoteState

    relay = os.environ.get("COLLIE_RELAY", "wss://collie.run").rstrip("/")

    # The remote path binds its own server rather than going through webapp.main(), so the flag has
    # to be carried here too — a phone reaching this desktop over the relay is exactly the case that
    # needs to know which dog answered.
    if getattr(args, "name", ""):
        webapp.DOG_NAME = str(args.name)
    try:
        httpd, port = webapp.bind_server(args.port)
    except OSError as e:
        print("collie web --remote: %s (pass --port <free port>)" % e)
        return 1
    webapp.start_mission_ticker()
    threading.Thread(target=httpd.serve_forever, name="collie-web", daemon=True).start()

    state = RemoteState(relay, port, webapp.TOKEN, logf=lambda *a: print(*a, flush=True))
    webapp.REMOTE = state                           # expose to the web server's /api/remote/* + panel
    state.start()
    n_dev = len(state.identity.devices())

    print("collie web · local http://127.0.0.1:%d/ · provider=%s" % (port, webapp._provider()), flush=True)
    print("collie remote · relay=%s · %d paired device(s)" % (relay, n_dev), flush=True)
    print("  Control panel (on this computer):  http://127.0.0.1:%d/remote" % port, flush=True)
    print("─" * 60, flush=True)

    # Do not advertise a pairing link until the agent socket is actually up. A relay hostname that
    # serves something else (a marketing site, say) answers the phone's POST /pair with 405, and the
    # failure surfaces on the phone rather than here — which is exactly backwards.
    if not state.wait_connected(10.0):
        why = state.last_error()
        print("  NOT connected to the relay%s" % (" — %s" % why if why else ""), flush=True)
        print("  So no pairing link: it would point at whatever else answers on that host.", flush=True)
        print("  Check that %s routes /relay/agent and /r/* to the relay Worker," % relay, flush=True)
        print("  or set COLLIE_RELAY to the Worker's own hostname "
              "(e.g. wss://<name>.<subdomain>.workers.dev).", flush=True)
    else:
        print("  Open on your phone:  %s" % state.link(), flush=True)
        _print_qr(state.link())
        print("  Pairing code: %s   (only needed to add a NEW device)" % state.paircode, flush=True)
        if n_dev:
            print("  Already-paired devices reconnect automatically — no code needed.", flush=True)
    print("─" * 60, flush=True)
    print("  Ctrl-C to stop (this instantly cuts off all remote access).", flush=True)

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
        httpd.shutdown()
    return 0


# ── the desktop window ───────────────────────────────────────────────────────────────────────────
# One contract on every OS: a Chromium-family browser in --app mode under collie's own profile dir,
# which is what makes it a real borderless window instead of a tab (and stops Chrome from handing the
# request to an already-running instance that would ignore --app).
#
# Only two things genuinely vary, and neither is "which OS":
#   where the binary lives  — a .app bundle on macOS, PATH everywhere else
#   whose desktop it opens on — on WSL the user is looking at the WINDOWS desktop, so the browser has
#                               to be launched over there; everywhere else "local" is the right screen
# So supporting one more OS should be an entry in a tuple, not another branch.
_BROWSERS_BUNDLE = ("Google Chrome", "Microsoft Edge", "Brave Browser", "Chromium", "Vivaldi")
_BROWSERS_PATH = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                  "microsoft-edge", "microsoft-edge-stable", "brave-browser", "vivaldi-stable")


def _app_window_flags(url, kiosk):
    """The window contract — byte-identical on every OS."""
    profile = os.path.join(os.path.expanduser("~"), ".collie", "desktop")
    return (["--app=%s" % url, "--user-data-dir=%s" % profile]
            + (["--kiosk", "--start-fullscreen"] if kiosk else ["--start-maximized"]))


def _find_browser():
    """(path, label) of a local Chromium-family browser, or (None, why-not)."""
    from . import plat
    if plat.is_macos():
        for name in _BROWSERS_BUNDLE:
            for root in ("/Applications", os.path.expanduser("~/Applications")):
                exe = os.path.join(root, name + ".app", "Contents", "MacOS", name)
                if os.path.exists(exe):
                    return exe, name
        return None, ("no Chromium-family browser in /Applications (looked for %s), and Safari has "
                      "no --app mode" % ", ".join(_BROWSERS_BUNDLE))
    import shutil
    for name in _BROWSERS_PATH:
        exe = shutil.which(name)
        if exe:
            return exe, name
    return None, "no Chromium-family browser on PATH (looked for %s)" % ", ".join(_BROWSERS_PATH)


def _open_window_local(url, kiosk):
    """The window on THIS machine's desktop — macOS and Linux."""
    import subprocess
    exe, label = _find_browser()
    if not exe:
        return False, "%s — open %s in a browser instead" % (label, url)
    try:
        subprocess.Popen([exe] + _app_window_flags(url, kiosk),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, "launch error (%s): %s" % (label, e)
    return True, "opened in %s" % label


def _open_window_wsl(url, kiosk):
    """WSL only, and the one case that can't be done locally: the desktop the user is looking at
    belongs to Windows, so the browser is launched over there through powershell.exe, in their own
    logged-in Edge profile. A Linux-side window would open in WSLg, not on that desktop."""
    import shutil, subprocess
    ps = shutil.which("powershell.exe")
    if not ps:
        return False, "no powershell.exe"
    flags = ["'--kiosk'", "'--edge-kiosk-type=fullscreen'"] if kiosk else ["'--start-maximized'"]
    argl = ",".join(["'--app=%s'" % url] + flags) + \
        ",('--user-data-dir=' + $env:LOCALAPPDATA + '\\collie-desktop')"
    script = (
        "$e=@('C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',"
        "'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe')|?{Test-Path $_}|Select-Object -First 1;"
        "if(-not $e){Write-Error 'edge-not-found';exit 3};"
        "Start-Process $e -ArgumentList " + argl
    )
    try:
        from . import plat as _plat
        r = subprocess.run([ps, "-NoProfile", "-NonInteractive", "-Command", script],
                           capture_output=True, text=True, timeout=25,
                           **_plat.no_window_kwargs())
    except Exception as e:
        return False, "launch error: %s" % e
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or ("powershell exit %d" % r.returncode)).strip()
    return True, "opened"


def _desktop_window(url, kiosk=False):
    """Pop a borderless browser window showing `url` — a *real* window, so clicks and typing are 100%
    reliable (unlike a behind-icons wallpaper, where the shell eats clicks). Returns (ok, detail).

    Only ever reached off native Windows: there `collie app` / `collie wallpaper` drive the WebView2
    engine instead, so the cases here are WSL, macOS and Linux."""
    from . import plat
    if plat.is_wsl():
        ok, detail = _open_window_wsl(url, kiosk)
        if ok:
            return ok, detail
        # fall through rather than give up: WSLg puts a Linux window on the Windows desktop too, so
        # a WSL box with no powershell.exe still has a way to show this.
    return _open_window_local(url, kiosk)


# Config worth not destroying by accident: an API key, a paired phone, MCP logins.
_KEEP = ("settings.json", "mcp.json", "remote.json", "desktop.json")


def cmd_menubar(args):
    """collie as a menu-bar item: click, ask, click away. macOS only — Windows has the tray, which
    is a different enough thing to deserve its own implementation rather than a shared pretence."""
    from . import plat
    if not plat.is_macos():
        print("collie menubar is macOS-only for now.", file=sys.stderr)
        return 2
    from . import menubar_mac
    ok, why = menubar_mac.available()
    if not ok:
        print("collie menubar: %s" % why, file=sys.stderr)
        return 2
    import threading, time, urllib.request
    from .webapp import main as web_main
    threading.Thread(target=web_main, args=(["--port", str(args.port), "--no-open"],),
                     daemon=True).start()
    for _ in range(60):
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % args.port, timeout=0.8).read()
            break
        except Exception:
            time.sleep(0.2)
    print("collie menubar · click the collie in your menu bar · Ctrl-C to stop", flush=True)
    return menubar_mac.run("http://127.0.0.1:%d/" % args.port)


def cmd_update(args):
    """Report a newer release, and install it with --yes.

    Which install to touch is detected, not asked: the .app is replaced from the signed dmg, a pip
    install upgrades from the release wheel, brew runs brew upgrade. On macOS the download must
    satisfy Gatekeeper AND carry our Developer ID before anything is mounted — an updater that runs
    whatever it fetched is a way to lose the machine.
    """
    from . import update as up
    try:
        info = up.check()
    except Exception as e:
        print("could not reach the release feed: %s" % e, file=sys.stderr)
        return 1

    print("collie %s   latest %s   (installed via %s)"
          % (info["current"], info["latest"] or "?", info["kind"]))
    if not info["newer"]:
        print("already up to date." if info["latest"] else "no published release found.")
        return 0

    print("\n  a newer version is available: %s" % info["url"])
    for line in (info["notes"] or "").splitlines()[:6]:
        if line.strip():
            print("    " + line.strip()[:100])
    if not args.yes:
        print("\n  install it with:  collie update --yes")
        return 0

    kind, assets = info["kind"], info["assets"]
    digests = info.get("digests") or {}

    def _fetch(name):
        dest = os.path.join(tempfile.gettempdir(), name)
        print("\n  downloading %s …" % name)
        last = [0]

        def prog(got, total):
            pct = int(got * 100 / total)
            if pct >= last[0] + 10:
                last[0] = pct
                print("    %d%%" % pct, flush=True)

        up._download(assets[name], dest, prog)
        return dest

    if kind == "brew":
        ok, why = up.apply_brew()
    elif kind == "setup":
        name = next((n for n in assets if n.endswith(".exe")), "")
        if not name:
            print("this release has no Windows installer", file=sys.stderr)
            return 1
        try:
            exe = _fetch(name)
        except Exception as e:
            print("download failed: %s" % e, file=sys.stderr)
            return 1
        ok, why = up.apply_windows(exe, digests.get(name, ""), on_note=print)
    elif kind == "app":
        name = next((n for n in assets if n.endswith(".dmg")), "")
        if not name:
            print("this release has no macOS disk image", file=sys.stderr)
            return 1
        try:
            dest = _fetch(name)
        except Exception as e:
            print("download failed: %s" % e, file=sys.stderr)
            return 1
        ok, why = up.apply_macos(dest, on_note=print)
    else:
        whl = next((n for n in assets if n.endswith(".whl")), "")
        if not whl:
            print("this release has no wheel", file=sys.stderr)
            return 1
        ok, why = up.apply_pip(assets[whl])

    print(("\nupdated to %s — %s" % (info["latest"], why)) if ok
          else ("\nupdate failed: %s" % why), file=sys.stdout if ok else sys.stderr)
    if ok and kind in ("app", "setup"):
        print("relaunch Collie to pick it up.")
    return 0 if ok else 1


def cmd_uninstall(args):
    """Remove collie. Lists first, deletes only with --yes.

    macOS has no uninstaller, so "drag it to the Trash" leaves ~/.collie behind — which is mostly a
    browser profile and can run to hundreds of megabytes — and leaves the Screen Recording, Camera
    and Microphone grants sitting in System Settings under an app that no longer exists. Both are
    invisible until you go looking, so this names every path and every grant before touching any.
    """
    import shutil as _sh
    from . import plat

    home = os.path.expanduser("~")
    cdir = os.path.join(home, ".collie")
    targets, kept = [], []

    app = "/Applications/Collie.app"
    if plat.is_macos() and os.path.isdir(app):
        targets.append((app, _dirsize(app)))

    if os.path.isdir(cdir):
        for name in sorted(os.listdir(cdir)):
            path = os.path.join(cdir, name)
            if args.keep_config and name in _KEEP:
                kept.append(name); continue
            targets.append((path, _dirsize(path)))

    procs = _collie_procs()
    total = sum(sz for _, sz in targets)

    print("collie uninstall%s" % ("" if args.yes else "  (dry run — nothing will be deleted)"))
    if procs:
        print("\n  running processes to stop:")
        for pid, what in procs:
            print("    pid %-7s %s" % (pid, what[:76]))
    if targets:
        print("\n  to remove (%s):" % _human(total))
        for path, sz in sorted(targets, key=lambda t: -t[1]):
            print("    %8s  %s" % (_human(sz), path.replace(home, "~")))
    if kept:
        print("\n  kept (--keep-config): %s" % ", ".join(kept))
    if plat.is_macos():
        print("\n  macOS permission grants to reset (they outlive the app):")
        print("    ScreenCapture, Camera, Microphone, AppleEvents  for run.collie.desktop")
    if not targets and not procs:
        print("\n  nothing to remove — collie is not installed here.")
        return 0
    if not args.yes:
        print("\n  re-run with --yes to do it:  collie uninstall --yes")
        return 0

    failures = []
    for pid, _what in procs:
        ok, why = _stop_collie_proc(pid)
        if not ok:
            failures.append("could not stop pid %s: %s" % (pid, why))
    for path, _sz in targets:
        try:
            _sh.rmtree(path) if os.path.isdir(path) else os.remove(path)
        except Exception as e:
            failures.append("could not remove %s: %s" % (path, e))
    if plat.is_macos():
        for svc in ("ScreenCapture", "Camera", "Microphone", "AppleEvents"):
            try:
                subprocess.run(["tccutil", "reset", svc, "run.collie.desktop"],
                               capture_output=True, timeout=15)
            except Exception:
                pass
    if not args.keep_config and os.path.isdir(cdir) and not os.listdir(cdir):
        try:
            os.rmdir(cdir)
        except OSError:
            pass
    # Never claim removal when an OS denial or still-running process left material behind.
    for path, _sz in targets:
        if os.path.lexists(path) and not any(path in f for f in failures):
            failures.append("still exists after removal: %s" % path)
    if failures:
        print("\ncollie uninstall incomplete:", file=sys.stderr)
        for failure in failures:
            print("  - " + failure, file=sys.stderr)
        return 1
    print("\ncollie removed. `pip uninstall collie-harness` if you installed it that way.")
    return 0


def _dirsize(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    n = 0
    for root, _d, files in os.walk(path):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f%s" % (n, unit)
        n /= 1024.0


def _collie_procs():
    """collie processes started from anywhere — the wallpaper, a web server, the browser bridge."""
    out = []
    from . import plat

    def _ours(cmd):
        # Match argv/module boundaries, not arbitrary substrings: the old predicate could kill an
        # unrelated `python -c "print('harness.webapp docs')"` process during uninstall.
        import shlex
        try:
            words = shlex.split(cmd or "", posix=not plat.is_windows())
        except ValueError:
            return False
        words = [w.strip('"\'') for w in words]
        if not words:
            return False
        base = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base in ("collie", "collie.exe", "collie-harness", "collie-harness.exe",
                    "collie-wallpaper.exe"):
            return True
        python_base = base.removesuffix(".exe")
        if not re.fullmatch(r"(?:pythonw?|py)(?:\d+(?:\.\d+)*)?", python_base):
            return False
        modules = {"harness.cli", "harness.webapp", "harness.browserbridge",
                   "harness.wallpaper"}
        i = 1
        while i < len(words):
            word = words[i]
            if word == "-m":
                return i + 1 < len(words) and words[i + 1].lower() in modules
            if word == "-c":
                if i + 1 >= len(words):
                    return False
                code = words[i + 1]
                return any(("from %s import " % module) in code for module in modules)
            # Python options before a script are not the program identity. -W/-X consume the next
            # argv too; after the first non-option everything else is only an argument to that
            # script and must not be searched for Collie-looking text.
            if word in ("-W", "-X", "--check-hash-based-pycs"):
                i += 2
                continue
            if word == "--":
                i += 1
                break
            if word.startswith("-"):
                i += 1
                continue
            break
        if i >= len(words):
            return False
        script = words[i].replace("\\", "/").rsplit("/", 1)[-1].lower()
        return script in ("collie", "collie.py", "collie-harness", "bridge-boot.pyw")

    try:
        if plat.is_windows():
            # ps is absent in ordinary Windows installs (and a WSL ps cannot see native pythonw
            # processes). CIM is the native source of command lines, including windowless apps.
            script = ("Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | "
                      "ConvertTo-Json -Compress")
            r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                               capture_output=True, text=True, timeout=15,
                               **plat.no_window_kwargs())
            if r.returncode != 0:
                return []
            rows = json.loads(r.stdout or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                pid, cmd = str(row.get("ProcessId") or ""), str(row.get("CommandLine") or "")
                if pid.isdigit() and int(pid) != os.getpid() and "uninstall" not in cmd.lower() \
                        and _ours(cmd):
                    out.append((pid, cmd.strip()))
            return out
        r = subprocess.run(["ps", "-eo", "pid,command"], capture_output=True, text=True, timeout=10)
        for line in (r.stdout or "").splitlines()[1:]:
            pid, _, cmd = line.strip().partition(" ")
            if _ours(cmd) and "uninstall" not in cmd.lower() \
               and pid.isdigit() and int(pid) != os.getpid():
                out.append((pid, cmd.strip()))
    except Exception:
        pass
    return out


def _stop_collie_proc(pid):
    from . import plat
    try:
        pid = int(pid)
        if plat.is_windows():
            r = subprocess.run(["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                               capture_output=True, text=True, timeout=20,
                               **plat.no_window_kwargs())
            if r.returncode != 0:
                detail = (r.stderr or r.stdout or "taskkill failed").strip()
                return False, detail
            return True, ""
        os.kill(pid, 15)
        return True, ""
    except ProcessLookupError:
        return True, ""                         # it exited after the dry-run inventory
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def cmd_app(args):
    """collie app — open collie in a native desktop window, with the server behind it.

    Double-clicking Collie.app runs exactly this. It used to print "native window is Windows-only"
    and fall through to cmd_web, which starts a server and opens a browser — except the bundle
    launches with no controlling terminal and nothing to attach a browser to, so it created ZERO
    windows. The Dock icon bounced, and nothing ever appeared.

    macOS has had a real window all along; it was only ever wired to `collie wallpaper`. Same
    NSWindow + WKWebView, at the ordinary window level: an app you can Cmd-Tab to and close.
    """
    from . import plat
    if plat.is_windows():
        from . import wallpaper as wp
        return wp.run_app(port_pref=args.port)

    if plat.is_macos():
        from . import desktop_mac
        ok, why = desktop_mac.available()
        if ok:
            import threading, time, urllib.request
            from .webapp import main as web_main
            # Wait to be TOLD the port. web_main scans forward when the asked-for one is busy, and
            # used to keep the port it settled on to itself: this asked for 8787, the server bound
            # 8791, and the window was pointed at 8787 — a dead port. The app opened, bounced and
            # showed nothing, and relaunching made it worse, because the abandoned server kept its
            # port and the next one moved further along.
            bound = {}
            # AppKit owns the main thread, so the server goes to a daemon thread — the same split
            # the Windows engine gets by launching the server as its own pythonw process.
            threading.Thread(target=web_main,
                             args=(["--port", str(args.port), "--no-open"],),
                             kwargs={"on_bound": lambda p: bound.setdefault("port", p)},
                             daemon=True).start()
            for _ in range(60):
                if "port" in bound:
                    break
                time.sleep(0.2)
            port = bound.get("port", args.port)
            for _ in range(60):
                try:
                    urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % port, timeout=0.8).read()
                    break
                except Exception:
                    time.sleep(0.2)
            # An app window, not the desktop: titled, closable, in the Dock and in Cmd-Tab. The
            # desktop is `collie wallpaper`, and it stays opt-in — opening the app should not take
            # over your screen.
            return desktop_mac.run_app_window("http://127.0.0.1:%d/" % port)
        print("collie app: %s" % why, file=sys.stderr)
        print("  falling back to the browser GUI.", file=sys.stderr)
    else:
        print("collie app: native window is Windows/macOS only — falling back to the browser GUI.")
    return cmd_web(args)


def cmd_wallpaper(args):
    """collie's live desktop as the wallpaper (behind the icons), owned by collie. On Windows this
    drives the WebView2 engine (built on demand from source, autostart-able, port picked at runtime);
    elsewhere it degrades to a borderless full-screen browser window. See harness/wallpaper.py."""
    from . import plat
    # sub-actions (Windows engine): install/uninstall autostart, boot entry, clean stop
    if plat.is_windows():
        from . import wallpaper as wp
        if getattr(args, "install", False):
            return wp.install()
        if getattr(args, "uninstall", False):
            return wp.uninstall()
        if getattr(args, "stop", False):
            return wp.stop()
        return wp.run(port_pref=args.port, boot=getattr(args, "boot", False))

    # non-Windows: no Progman/WebView2 — fall back to a borderless browser window. Same page the
    # Windows engine loads (wallpaper.py sets COLLIE_WALLPAPER_URL to /ambient): the calm
    # theme-adaptive desktop, not the older /wallpaper galaxy, which only stayed the default here
    # because this branch hardcoded its own URL and the switch never reached it.
    import time, threading, urllib.request
    port = args.port
    url = "http://127.0.0.1:%d/ambient" % port

    # macOS has a real native path (window levels, via PyObjC) — the counterpart to the Windows
    # Progman engine — and it serves BOTH modes:
    #   default   desktop level, ignores mouse: a wallpaper you look at, clicks reach Finder
    #   --front   normal level, interactive, sized to visibleFrame so the Dock and menu bar stay
    #             clear — the composer lives at the bottom of the page and the Dock sat on it
    # --front used to skip this branch entirely and fall through to a borderless *browser* window,
    # which made desktop_mac's behind=False half dead code and meant asking for an interactive
    # desktop got you a browser instead of an app. A browser window is now only the fallback for
    # when PyObjC is not installed.
    if plat.is_macos():
        from . import desktop_mac
        if getattr(args, "stop", False):
            print(desktop_mac.stop())
            return 0
        ok, why = desktop_mac.available()
        if ok:
            if desktop_mac.running_pid():
                print("collie wallpaper: already running (pid %s) · stop it with:  collie wallpaper --stop"
                      % desktop_mac.running_pid())
                return 0
            # AppKit must own the main thread, so the server goes to a daemon thread — the same
            # split the Windows engine gets by launching the server as its own pythonw process.
            from .webapp import main as web_main
            threading.Thread(target=web_main, args=(["--port", str(port), "--no-open"],),
                             daemon=True).start()
            for _ in range(60):
                try:
                    urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % port, timeout=0.8).read()
                    break
                except Exception:
                    time.sleep(0.2)
            return desktop_mac.run(url, behind=not getattr(args, "front", False))
        print("collie wallpaper: %s" % why, file=sys.stderr)
        print("  falling back to a borderless browser window over the desktop.\n", file=sys.stderr)

    def _up():
        try:
            urllib.request.urlopen("http://127.0.0.1:%d/api/ver" % port, timeout=0.8).read()
            return True
        except Exception:
            return False

    if _up():
        ok, detail = _desktop_window(url, kiosk=args.kiosk)
        print("collie wallpaper · %s · %s" % (url, "window opened" if ok else "no window: " + detail))
        return 0 if ok else 1

    def _delayed():
        for _ in range(60):
            if _up():
                break
            time.sleep(0.2)
        ok, detail = _desktop_window(url, kiosk=args.kiosk)
        print(("collie wallpaper · window opened" if ok else
               "collie wallpaper · could not open window (%s) — open %s yourself" % (detail, url)),
              flush=True)
    threading.Thread(target=_delayed, daemon=True).start()
    from .webapp import main as web_main
    return web_main(["--port", str(port), "--no-open"])


def cmd_browser_bridge(args):
    """Run the browser-bridge server (the Chrome extension polls it; browser_* tools drive it).
    --install registers it to start hidden at logon, so collie keeps its real-browser powers."""
    from . import browserbridge as bb
    if getattr(args, "install", False):
        return bb.install_autostart()
    if getattr(args, "uninstall", False):
        return bb.uninstall_autostart()
    argv = ["--port", str(args.port)] if args.port else []   # [] not None: None re-reads argv
    if getattr(args, "browser", False):
        argv.append("--browser")
    if getattr(args, "headed", False):
        argv.append("--headed")
    return bb.main(argv)


def cmd_slack(args):
    """Answer to an @mention in Slack: one named collie per machine, a queue, and
    an autonomy setting it says out loud. Socket Mode, so the laptop needs no
    public address — see harness/slackbot.py for why that decides the design."""
    from . import slackbot
    argv = []
    if getattr(args, "slack_action", "run") == "setup":
        # Provisioning takes a different set of flags, and `--cwd`/`--provider` default to
        # something on every run — passing them through would look like they meant something here.
        argv = ["setup"]
        for flag in ("name", "config_token", "bot_token", "app_token"):
            v = getattr(args, flag, "")
            if v:
                argv += ["--" + flag.replace("_", "-"), str(v)]
        if getattr(args, "list_dogs", False):
            argv += ["--list"]
        return slackbot.main(argv)
    for flag in ("name", "autonomy", "cwd", "provider", "announce", "channels", "allow"):
        v = getattr(args, flag, "")
        if v:
            argv += ["--" + flag, str(v)]
    for flag in ("install_autostart", "uninstall_autostart"):
        if getattr(args, flag, False):
            argv += ["--" + flag.replace("_", "-")]
    return slackbot.main(argv)


def cmd_mail(args):
    """collie mail — an address of this dog's own, and the ability to wait for a letter.

    The reason it exists is `wait`: a signup that ends in "check your email" stops being a handover
    to a human. See harness/dogmail.py for the identity and encryption design.
    """
    from . import dogmail as dm
    act = args.mail_action
    if act == "claim":
        if not args.name or not args.value:
            print("usage: collie mail claim <handle> <your-real-email>")
            return 1
        d = dm.claim_handle(args.name, args.value)
        if not d.get("ok"):
            print("could not claim %r: %s" % (args.name, d.get("error") or d))
            return 1
        print("a code is on its way to %s — then: collie mail verify <code>" % args.value)
        return 0
    if act == "verify":
        d = dm.verify_handle(args.name)
        print("handle verified" if d.get("ok") else "not verified: %s" % (d.get("error") or d))
        return 0 if d.get("ok") else 1
    if act == "add":
        if not args.name:
            print("usage: collie mail add <dog name>")
            return 1
        d = dm.claim_dog(args.name)
        if not d.get("ok"):
            print("could not give %s an address: %s" % (args.name, d.get("error") or d))
            return 1
        print("%s is now %s" % (args.name, d["address"]))
        return 0
    if act == "list":
        st = dm.load()
        dogs = st.get("dogs") or {}
        if not dogs:
            print("(no addresses yet — `collie mail claim <handle> <email>` then `collie mail add <dog>`)")
            return 0
        for n, d in sorted(dogs.items()):
            print("  %-10s %s" % (n, d.get("address", "?")))
        for m in dm.fetch(args.name):
            print("  · %s — %s" % (m.get("from", "?"), (m.get("subject") or "")[:70]))
        return 0
    if act == "wait":
        m = dm.wait_for(args.name, subject=args.subject, sender=args.sender, timeout=args.timeout)
        if not m:
            print("nothing matching arrived in %ds" % args.timeout)
            return 1
        print("from: %s\nsubject: %s\n\n%s" % (m.get("from", ""), m.get("subject", ""),
                                               (m.get("text") or m.get("raw") or "")[:4000]))
        return 0
    print("unknown action %r" % act)
    return 1


def cmd_record(args):
    """Screen recording with a circular webcam bubble + mic (Loom / Reframe style), via ffmpeg.
    Sub-actions: start (default) / stop / status / devices. See harness/record.py."""
    from . import record as rec
    action = getattr(args, "record_action", None) or "start"
    try:
        if action == "stop":
            print(rec.stop())
        elif action == "status":
            print(rec.status())
        elif action == "devices":
            cams, mics = rec.list_capture_devices()
            print("cameras:\n  " + ("\n  ".join(cams) or "(none found)"))
            print("microphones:\n  " + ("\n  ".join(mics) or "(none found)"))
            screens = rec.list_screens()
            if screens:
                print("monitors:\n  " + "\n  ".join(screens))
        elif action == "windows":
            print("windows:\n  " + ("\n  ".join(rec.list_windows()) or "(none)"))
        elif action == "list":
            recs = rec.list_recordings()
            print("recordings in %s:\n  " % rec._default_outdir() + ("\n  ".join(
                "%s  (%.1f MB)" % (r["name"], r["mb"]) for r in recs) or "(none)"))
        else:  # start
            print(rec.start(webcam=args.webcam, mic=args.mic, sysaudio=args.sys_audio,
                            fps=args.fps, cam_size=args.cam_size, margin=args.margin,
                            position=args.position, mirror=not args.no_mirror,
                            monitor=args.monitor, region=args.region, window=args.window,
                            out=args.out, no_cam=args.no_cam, no_mic=args.no_mic, countdown=args.countdown))
        return 0
    except Exception as e:
        print("record: %s" % e)
        return 1


def cmd_acp(args):
    """Serve collie as an ACP agent over stdio (the editor spawns this)."""
    # A human running `collie acp` in a terminal has a tty on stdio, not the pipes the ACP
    # transport needs — that used to crash with a raw asyncio traceback. Explain instead.
    if sys.stdin.isatty():
        print("collie acp speaks the Agent Client Protocol over stdio — it has no interactive UI "
              "and is meant to be SPAWNED BY AN EDITOR (Zed / VS Code ACP client).\n"
              "Configure it (Zed example, ~/.config/zed/settings.json):\n"
              '  {"agent_servers": {"collie": {"command": "%s", "args": ["acp"]}}}\n'
              "For a terminal chat use `collie tui`; for a browser UI use `collie web`."
              % (sys.argv[0] or "collie"))
        return 0
    from .acp_agent import main as acp_main
    acp_main()
    return 0


def cmd_run(args):
    import json as _json
    _, runs_db, out_html, _ = _paths()
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")

    # Resolve continuity before routing: a recent failed turn is a legitimate
    # escalation signal, and therefore belongs in the same decision on CLI and Web.
    from . import sessions as sess
    history, sid = None, None
    def _recovery_refusal(candidate):
        state = sess.recovery_state(candidate) if candidate else None
        if not (state and state.get("recovery_required")):
            return False
        payload = {"answer": "", "error": state.get("reason") or "recovery required",
                   "session": candidate, "recovery_required": True, "recovery": state}
        if getattr(args, "json", False) or getattr(args, "stream_json", False):
            print(_json.dumps(payload, ensure_ascii=False))
        else:
            print("recovery required: %s" % payload["error"], file=sys.stderr)
        return True
    if getattr(args, "resume", None):
        if _recovery_refusal(args.resume):
            return 2
        s = sess.load(args.resume)
        if s:
            history, sid = (s.get("messages") or []), args.resume
        else:
            print("  [session] no such session %r — starting fresh" % args.resume,
                  file=sys.stderr if getattr(args, "json", False) else sys.stdout)
    elif getattr(args, "cont", False):
        sid = sess.latest()
        if sid:
            if _recovery_refusal(sid):
                return 2
            history = (sess.load(sid) or {}).get("messages")
    sid = sid or sess.new_id()

    from . import settings
    from .router import resolve_run_decision
    explicit = []
    for axis in ("intent", "quality", "verification", "effort", "speed"):
        if getattr(args, axis, None) is not None:
            explicit.append(axis)
    requested_intent = getattr(args, "intent", None) or "build"
    if getattr(args, "mode", None) == "plan":
        if getattr(args, "intent", None) not in (None, "plan"):
            print("--mode plan conflicts with --intent %s" % args.intent, file=sys.stderr)
            return 2
        requested_intent = "plan"
        explicit.append("intent")
    configured_model = args.model
    if configured_model is None and (not args.provider or
                                      args.provider == settings.get("PROVIDER", provider)):
        configured_model = settings.get("MODEL", "") or None
    decision = resolve_run_decision(
        args.task, provider=provider,
        model=configured_model,
        effort=(getattr(args, "effort", None) or
                settings.get("REASONING_EFFORT", "auto") or "auto"),
        speed=getattr(args, "speed", None) or "standard",
        intent=requested_intent,
        quality=getattr(args, "quality", None) or "balanced",
        verification=getattr(args, "verification", None) or "auto",
        explicit_axes=explicit, history=history)
    decision_payload = decision.to_dict()

    verify_command = (getattr(args, "verify_command", None) or "").strip()
    verify_source = "user" if verify_command else ""
    if not verify_command:
        from .verification import detect_verification_commands
        proposals = detect_verification_commands(cwd)
        if proposals:
            verify_command = proposals[0]["command"]
            verify_source = proposals[0]["source"]
    if verify_command:
        decision_payload["verification_proposal"] = {
            "command": verify_command, "source": verify_source,
        }
    if decision.intent == "test" and not verify_command:
        print("Test needs a detected or explicit --verify-command", file=sys.stderr)
        return 2

    gate_mode = (decision.intent if decision.intent in ("plan", "review", "test")
                 else getattr(args, "mode", None))
    _gate = default_gate(cwd, gate_mode,
                         commands=[verify_command] if decision.intent == "test" else None)
    h = make_harness(cwd, provider=provider, model=decision.model,
                     effort=decision.effort, speed=decision.speed, project=args.project,
                     web_search=True if getattr(args, "web_search", False) else None,
                     exec_code=True, delegate=True, gate=_gate)
    configure_run_options(h, intent=decision.intent, quality=decision.quality,
                          verification=decision.verification)
    # An approver only when there is genuinely someone there. Piped or in CI, stdin is not a
    # person: leaving it unset means off-machine calls are refused with a reason the model can
    # work around, rather than run because nobody objected. `--mode auto` is the explicit
    # opt-out for a sandbox that wants the old behaviour.
    import sys as _sys
    if _sys.stdin is not None and _sys.stdin.isatty():
        from .approve import tty_approver
        h.approve = tty_approver(gate=_gate)
    if getattr(args, "persona", None):
        if apply_persona(h, _gate, args.persona, cwd) is None:
            print("no persona named %r (looked in .collie/personas and ~/.collie/personas)"
                  % args.persona)
            return 2
    if getattr(args, "goal", None):              # pin a standing goal into CORE memory (every turn)
        h.memory.set_block("project:" + args.project, "goal", args.goal[:390], char_limit=400)
    h.checkpoint_scope = "session:" + sid
    # --stream-json: emit one NDJSON event per action (tool/edit/repro/receipt) as it happens,
    # so a terminal, an editor extension, or the ACP adapter can render the run LIVE (the
    # verification gate flipping fail->pass) instead of waiting for one final blob. Progress to
    # stderr keeps stdout clean for --json consumers piping the final object.
    if getattr(args, "stream_json", False):
        import sys as _sys
        h.emit = lambda kind, d: print(_json.dumps({"type": kind, **d}, ensure_ascii=False),
                                       file=_sys.stderr, flush=True)
        h.emit("decision", decision_payload)
    will_verify = bool(
        (decision.intent == "test" or decision.verification == "required") and
        verify_command)
    h.defer_memory_promotion = will_verify
    res = h.run("adhoc", args.task, history=history)
    verification_evidence = None
    if will_verify:
        from .verification import run_verification_command
        verification_evidence = run_verification_command(
            verify_command, cwd, source=verify_source or "detected", after_last_edit=True)
        if callable(getattr(h, "emit", None)):
            h.emit("verification_evidence", {"evidence": verification_evidence})
        res.verified = bool(verification_evidence["passed"] and not res.error)
        if not verification_evidence["passed"]:
            check_error = "required check failed: %s (exit %s)" % (
                verify_command, verification_evidence.get("exit_code"))
            res.error = ((res.error + "; ") if res.error else "") + check_error
        h.settle_run_memory(
            res, bool(res.verified), verification_evidence,
            source="cli_verification")
        # Persist the final host-side verdict; run() could only record its in-loop evidence.
        h.recorder.finish_run(res)
    sess.save(sid, res.messages, project=args.project, cwd=cwd, answer=res.answer or "")
    actual_speed = getattr(getattr(h, "provider", None), "actual_speed", decision.speed)
    try:
        sess.append_run_receipt(sid, {
            "decision": decision_payload, "model": res.model,
            "actual_speed": actual_speed, "verified": bool(getattr(res, "verified", False)),
            "verification_evidence": verification_evidence, "error": res.error or "",
        })
    except Exception:
        pass
    if getattr(args, "json", False) or getattr(args, "stream_json", False):
        print(_json.dumps({
            "answer": res.answer, "error": res.error, "model": res.model, "session": sid,
            "decision": decision_payload, "actual_speed": actual_speed,
            "verification_evidence": verification_evidence,
            "prefix_tokens": res.prefix_tokens, "prefix_measured": res.prefix_measured,
            "input_tokens": res.input_tokens,
            "output_tokens": res.output_tokens, "cache_read": res.cache_read,
            "cache_creation": res.cache_creation, "total_tokens": res.total_tokens,
            "cache_miss_tokens": res.cache_miss_tokens, "cache_waste_usd": res.cache_waste_usd,
            "turns": res.turns, "tool_calls": res.tool_calls, "mem_recalls": res.mem_recalls,
            "wall_ms": res.wall_ms, "cost_usd": res.cost_usd}, ensure_ascii=False))
    elif getattr(args, "print", False):
        print(res.answer or res.error)          # headless: answer only (like claude -p)
    else:
        print("decision=%s · effort=%s · speed=%s · %s/%s/%s" % (
            res.model or decision.model, decision.effort, actual_speed,
            decision.intent, decision.quality, decision.verification))
        print("prefix=%d in=%d out=%d turns=%d tools=%d recall=%d %dms" % (
            res.prefix_tokens, res.input_tokens, res.output_tokens, res.turns,
            res.tool_calls, res.mem_recalls, res.wall_ms))
        print("\n%s" % (res.answer or res.error))
        print("\n  session %s · continue: collie run \"…\" --continue  (or --resume %s)" % (sid, sid))
        dash.build(runs_db, out_html)
    h.memory.close(); h.recorder.close()
    return 1 if res.error else 0


def cmd_prefix(args):
    """Measure the real prefix cost on a provider via a two-request usage differential — the honest
    counterpart to the est_tokens (~len/4) headline number. Appends the result to
    ~/.collie/prefix_probe.json as the raw evidence the CHANGELOG/README leanness claim cites."""
    import json as _json
    from .providers import measure_prefix
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    h = make_harness(cwd, provider=provider, model=args.model, project=args.project or "demo")
    system, _msgs, meta = h.composer.build({"messages": []}, ".", cwd, h.project, h.mode)
    schemas = h.registry.active_schemas()
    measured = measure_prefix(h.provider, system, schemas)
    est = meta.prefix_tokens
    drift = (100.0 * (est - measured) / measured) if measured else 0.0
    print("provider=%s model=%s  est=%d  measured=%d  drift=%+.0f%% (est vs measured)" % (
        h.provider.name, h.provider.model, est, measured, drift)
        + ("" if measured else "   [measured=0: provider gave no usable usage — probe unsupported]"))
    if measured:
        rec = {"provider": h.provider.name, "model": h.provider.model,
               "est": est, "measured": measured, "note": "prefetch-off composition"}
        pj = os.path.expanduser("~/.collie/prefix_probe.json")
        try:
            os.makedirs(os.path.dirname(pj), exist_ok=True)
            hist = []
            if os.path.exists(pj):
                try: hist = _json.load(open(pj))
                except Exception: hist = []
            hist.append(rec)
            _json.dump(hist[-100:], open(pj, "w"), indent=1)
            print("  recorded -> %s" % pj)
        except OSError:
            pass
    h.memory.close(); h.recorder.close()
    return 0


def cmd_pack(args):
    import json as _json
    from . import pack as _pack
    cwd = args.cwd or os.getcwd()
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")

    roster = [x.strip() for x in (getattr(args, "roster", None) or "").split(",") if x.strip()]
    from . import settings
    from .router import resolve_run_decision
    explicit = ["strategy"]
    for axis in ("quality", "verification", "effort", "speed"):
        if getattr(args, axis, None) is not None:
            explicit.append(axis)
    configured_model = args.model
    if configured_model is None and (not args.provider or
                                      args.provider == settings.get("PROVIDER", provider)):
        configured_model = settings.get("MODEL", "") or None
    decision = resolve_run_decision(
        args.task, provider=provider,
        model=configured_model,
        effort=(getattr(args, "effort", None) or
                settings.get("REASONING_EFFORT", "auto") or "auto"),
        speed=getattr(args, "speed", None) or "standard",
        intent="build", quality=getattr(args, "quality", None) or "balanced",
        verification=getattr(args, "verification", None) or "auto",
        strategy="pack", explicit_axes=explicit)
    decision_payload = decision.to_dict()
    if roster:
        decision_payload["sources"]["roster"] = "user"
        decision_payload["reasons"].append(
            "roster: explicit provider list; automatic routing did not add a provider")

    def _emit(i, rec):
        tag = ("check=%s " % ("pass" if rec.get("check_pass") else "fail")) if args.check else ""
        who = (" [%s]" % rec["provider"]) if roster and rec.get("provider") else ""
        print("  attempt %d%s: %sverified=%s turns=%s%s" % (
            i, who, tag, rec.get("verified"), rec.get("turns"),
            (" ERROR " + rec["error"]) if rec.get("error") else ""), flush=True)

    res = _pack.run_pack(args.task, cwd, n=args.n, check=args.check, provider=provider,
                         model=decision.model, effort=decision.effort, speed=decision.speed,
                         apply=args.apply, emit=_emit, quality=decision.quality,
                         verification=decision.verification,
                         roster=roster or None, parallel=getattr(args, "parallel", 1))
    res["decision"] = decision_payload
    if getattr(args, "json", False):
        print(_json.dumps(res, ensure_ascii=False))
        return 0 if (res.get("winner") is not None
                     and (not args.apply or res.get("applied"))) else 1
    print("decision=%s · effort=%s · speed=%s · %s/%s" % (
        decision.model, decision.effort, decision.speed,
        decision.quality, decision.verification))
    if res["winner"] is None:
        print("\nno winner: %s (nothing applied)" % res["reason"])
        return 1
    if args.apply and not res.get("applied"):
        print("\nwinner selected, but apply failed: %s" %
              (res.get("apply_error") or res.get("reason")), file=sys.stderr)
        return 1
    won_on = ""
    if res.get("winner_provider") and len(set(res.get("roster") or [])) > 1:
        won_on = " on %s" % (res.get("winner_model") or res["winner_provider"])
    print("\nwinner: attempt %d%s (%s) · total $%.4f across %d attempts%s" % (
        res["winner"], won_on, res["reason"], res["total_cost_usd"], res["n"],
        " · APPLIED to cwd" if res["applied"] else " · not applied (use --apply)"))
    print("\n%s" % res.get("answer", ""))
    return 0


def cmd_compare(args):
    from . import adapters
    mem_db, runs_db, out_html, sandbox = _paths()
    os.makedirs(sandbox, exist_ok=True)
    facts = cmp.build_sandbox(sandbox)
    h = make_harness(sandbox, provider=args.provider, model=args.model, project="demo")
    h.memory.remember(
        "We decided to internalize embeddings: local bge-m3 via fastembed feeding "
        "sqlite-vec + FTS5 hybrid retrieval.", keys="embedding memory design",
        project="demo")

    targets = adapters.resolve([k.strip() for k in args.vs.split(",")])
    inst = ", ".join("%s%s" % (a.label, "" if a.available() else "(off)")
                     for a in targets) or "(none)"
    try:                                              # cheap LLM-judge for quality
        judge = make_provider(args.judge) if args.judge else None
    except Exception:
        judge = None
    print("== compare: collie(%s) vs %s  [%s]  judge=%s ==" % (
        args.provider, inst, "REAL" if args.real else "baseline", judge.name if judge else "heuristic"))

    for task in cmp.task_suite(facts, full=True):
        cmp.reset_sandbox(sandbox)                    # pristine copy per run (fair edits)
        m = cmp.run_collie(h, task)
        cmp.grade_and_cost(m, task["prompt"], judge); h.recorder.finish_run(m)
        line = "  %-14s | collie q=%2.0f $%.4f %s" % (
            task["id"], m.quality, m.cost_usd, "PASS" if m.success else "FAIL")
        for a in targets:
            if args.real and a.available():
                cmp.reset_sandbox(sandbox)
                c = a.run(task, cwd=sandbox, recorder=h.recorder, model=args.vs_model)
                cmp.grade_and_cost(c, task["prompt"], judge); h.recorder.finish_run(c)
                line += " | %s q=%2.0f %s" % (
                    a.key, c.quality, "PASS" if c.success else ("ERR" if c.error else "FAIL"))
            elif cmp.baseline(h.recorder, a.key, task["id"]):
                line += " | %s baseline" % a.key
            else:
                line += " | %s(%s)" % (a.key, "off" if not a.available() else "no-baseline")
        print(line)

    dash.build(runs_db, out_html)
    print("  dashboard -> %s" % out_html)
    h.memory.close(); h.recorder.close()
    return 0


def cmd_audit(args):
    """What the gate decided. `--unexplained` is the one to run if you ever wonder why
    something happened without being asked about: it lists calls that went ahead silently
    and cannot say under which rule. It should always be empty."""
    from .audit import AuditLog
    log = AuditLog()
    try:
        rows = log.unexplained() if args.unexplained else log.list(
            limit=args.limit, tool=args.tool, stage=args.stage)
        if args.unexplained and not rows:
            print("nothing ran unexplained — every silent call cites a rule.")
            return 0
        if not rows:
            print("no gate decisions recorded yet.")
            return 0
        import datetime as _dt
        for r in rows:
            when = _dt.datetime.fromtimestamp(r["at"]).strftime("%m-%d %H:%M")
            print("%s  %-9s %-9s %-22s %s" % (
                when, r["stage"], r["risk"], r["tool"], r["rule"] or r["reason"]))
            if r["target"]:
                print("%s on %s" % (" " * 14, r["target"]))
        return 0
    finally:
        log.close()


def cmd_activity(args):
    """One durable Activity view across foreground runs and every unattended lane."""
    from .controlplane import activity, health
    value = health(args.state_dir, probe_services=not args.no_probe) if args.health \
        else activity(args.state_dir, limit=args.limit)
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return 0 if (not args.health or value.get("ok")) else 1


def cmd_library(args):
    """Inspect and operate the digest-pinned local extension lifecycle."""
    from .extensions import ExtensionError, ExtensionStore, scaffold_package, validate_package
    store = ExtensionStore(args.state_dir or None)
    action, value = args.action, args.value
    try:
        if action == "list":
            result = {"extensions": store.list()}
        elif action == "scaffold":
            if not value:
                raise ExtensionError("scaffold requires a new local directory")
            report = scaffold_package(value, args.extension_id, args.name, args.publisher)
            result = {key: report[key] for key in ("root", "digest", "manifest")}
        elif action == "connections":
            result = {"connections": store.connections()}
        elif action == "audit":
            result = {"audit": store.audit(args.limit)}
        elif action == "validate":
            if not value:
                raise ExtensionError("validate requires a local package directory")
            report = validate_package(value)
            result = {key: report[key] for key in
                      ("digest", "scope_hash", "manifest", "file_hashes")}
        elif action == "plan":
            if not value:
                raise ExtensionError("plan requires a local package directory")
            result = store.plan(value)
        elif action == "install":
            if not value:
                raise ExtensionError("install requires a local package directory")
            result = store.install(value, expected_digest=args.digest,
                                   approve=args.approve)
            if args.enable:
                installed_version = result.get("installed_version") or ""
                if args.version and args.version != installed_version:
                    raise ExtensionError(
                        "--version must match the package being installed (%s)" % installed_version)
                result = store.enable(result["id"], installed_version,
                                      approve=args.approve)
        elif action == "show":
            if not value: raise ExtensionError("show requires an extension id")
            result = store.get(value)
        elif action == "enable":
            if not value: raise ExtensionError("enable requires an extension id")
            result = store.enable(value, args.version or "", approve=args.approve)
        elif action == "disable":
            if not value: raise ExtensionError("disable requires an extension id")
            result = store.disable(value)
        elif action == "rollback":
            if not value: raise ExtensionError("rollback requires an extension id")
            result = store.rollback(value, approve=args.approve)
        elif action == "uninstall":
            if not value: raise ExtensionError("uninstall requires an extension id")
            if not args.yes:
                current = store.get(value)
                print(json.dumps({"will_remove": value,
                                  "version": args.version or "all installed versions",
                                  "current": current}, ensure_ascii=False, indent=2))
                print("repeat with --yes after reviewing this removal", file=sys.stderr)
                return 2
            result = store.uninstall(value, args.version or "", force=args.force)
        elif action == "revoke":
            if not value: raise ExtensionError("revoke requires an extension id")
            if not args.yes:
                print("revocation disables matching active bytes; repeat with --yes", file=sys.stderr)
                return 2
            result = store.revoke(value, args.digest, args.reason)
        else:  # argparse owns the action enum; this is a defensive protocol boundary.
            raise ExtensionError("unsupported Library action: %s" % action)
    except ExtensionError as exc:
        print("library %s refused: %s" % (action, exc), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_recovery(args):
    """Inspect or explicitly reconcile crash-uncertain interactive tool boundaries."""
    from . import sessions
    sessions_dir = os.path.join(os.path.abspath(os.path.expanduser(args.state_dir)), "sessions") \
        if args.state_dir else None
    if args.action == "ls":
        value = {"runs": sessions.active_runs(limit=args.limit, directory=sessions_dir)}
    elif not args.session:
        print("recovery %s requires a session id" % args.action, file=sys.stderr)
        return 2
    elif args.action == "show":
        state = sessions.recovery_state(args.session, directory=sessions_dir)
        if state is None:
            print("no active recovery state for %s" % args.session, file=sys.stderr)
            return 1
        value = {"session": args.session, "recovery": state}
    else:
        if not args.yes:
            print("reconciliation changes the durable replay fence; inspect the outside system, "
                  "then repeat with --yes", file=sys.stderr)
            return 2
        try:
            state = sessions.reconcile_recovery(
                args.session, args.resolution, note=args.note, confirmed=True,
                directory=sessions_dir)
        except (KeyError, ValueError) as exc:
            print("reconcile refused: %s" % exc, file=sys.stderr)
            return 1
        value = {"session": args.session, "resolution": args.resolution,
                 "recovery": state}
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_hooks(args):
    """Review hook definitions by exact hash before Collie is allowed to execute them."""
    from . import hooks
    cwd = os.path.abspath(args.cwd or os.getcwd())
    manager = hooks.HookManager(cwd)
    paths = [os.path.abspath(os.path.join(cwd, args.path))] if args.path \
        and not os.path.isabs(args.path) else \
        ([os.path.abspath(args.path)] if args.path else hooks._config_paths(cwd))
    if args.action == "status":
        rows = []
        trust = hooks.HookTrustStore()
        for path in paths:
            if not os.path.isfile(path):
                continue
            digest = hooks._digest(path)
            rows.append({"path": path, "sha256": digest,
                         "trusted": trust.is_trusted(path, digest),
                         "errors": hooks.validate_config(path)})
        print(json.dumps({"cwd": cwd, "active_events": manager.events(),
                          "pending": manager.pending, "configs": rows},
                         ensure_ascii=False, indent=2))
        return 0
    if args.action == "check":
        found, bad = [], False
        for path in paths:
            if not os.path.isfile(path):
                continue
            errors = hooks.validate_config(path)
            found.append({"path": path, "sha256": hooks._digest(path), "errors": errors})
            bad = bad or bool(errors)
        if not found:
            print("no hook configuration found", file=sys.stderr)
            return 1
        print(json.dumps({"configs": found}, ensure_ascii=False, indent=2))
        return 1 if bad else 0
    if not args.path:
        if len(manager.pending) != 1:
            print("supply the exact hook JSON path to trust/untrust", file=sys.stderr)
            return 2
        path = manager.pending[0]["path"]
    else:
        path = os.path.abspath(os.path.join(cwd, args.path)) \
            if not os.path.isabs(args.path) else os.path.abspath(args.path)
    if args.action == "trust":
        errors = hooks.validate_config(path)
        if errors:
            print(json.dumps({"path": path, "trusted": False, "errors": errors},
                             ensure_ascii=False, indent=2))
            return 1
        digest = hooks.HookTrustStore().set(path, True)
        value = {"path": path, "trusted": True, "sha256": digest}
    else:
        digest = hooks.HookTrustStore().set(path, False)
        value = {"path": path, "trusted": False, "sha256": digest}
    print(json.dumps(value, ensure_ascii=False, indent=2))
    return 0


def cmd_supervisor(args):
    from . import supervisor
    argv = [args.action]
    if args.state_dir and args.action != "run":
        argv += ["--state-dir", args.state_dir]
    if args.action == "install":
        if args.no_boot:
            argv.append("--no-boot")
        for worker in args.disable_worker:
            argv += ["--disable-worker", worker]
    if args.action == "run" and args.config:
        argv += ["--config", args.config]
    return supervisor.main(argv)


def cmd_automations(args):
    from . import automations
    argv = [args.action]
    if args.action == "upsert":
        argv.append(args.value)
    elif args.action == "status" and args.value:
        argv.append(args.value)
    if args.state_dir:
        argv += ["--state-dir", args.state_dir]
    if args.db:
        argv += ["--db", args.db]
    if args.ops_db:
        argv += ["--ops-db", args.ops_db]
    if args.workspace_root:
        argv += ["--workspace-root", args.workspace_root]
    if args.action == "daemon":
        argv += ["--interval", str(args.interval)]
    if args.action == "tick" and args.execute:
        argv.append("--execute")
    return automations.main(argv)


def cmd_trust(args):
    """Decide whether a directory's own `.collie/allow.toml` counts. Nothing else writes
    this — a repo can ask, only you can agree."""
    from .trust import TrustStore, canonical, repo_allowed_commands
    store = TrustStore()
    if args.action == "ls":
        vals = store.list()
        print("\n".join("  " + v for v in vals) if vals else "  (no directory is trusted)")
        return 0
    target = args.path or os.getcwd()
    if args.action == "revoke":
        print("no longer trusted: %s" % store.set(target, False))
        return 0
    asked = repo_allowed_commands(target, _AlwaysTrusted())    # show BEFORE agreeing
    print("trusting a directory lets its .collie/allow.toml auto-run commands here.")
    if asked:
        print("\n%s asks to auto-run:" % canonical(target))
        for c in asked:
            print("    %s" % c)
        print("\neach still has to match a command's argv exactly and carry no shell "
              "operators,\nso none of them can chain into something else.")
    else:
        print("\n%s asks for nothing (no .collie/allow.toml)." % canonical(target))
    print("\ntrusted: %s" % store.set(target, True))
    return 0


class _AlwaysTrusted:
    """Preview helper: read what a directory ASKS for without granting it. Used only to
    show the user the list before they decide — never to make a gate decision."""
    def is_trusted(self, _workspace):
        return True


def cmd_risk(args):
    """Print what collie can reach, grouped by how far it reaches. The policy is data in
    one table (harness/risk.py); this makes it something a person can actually look at
    before trusting the thing, instead of a claim in a README."""
    from . import risk as R
    from .gate import Mode, mode_from_env
    from .overrides import RiskOverrideStore
    from .risk import RiskClass

    reg = default_registry(code_search=True, web_search=True, exec_code=True, delegate=True)
    for mod, fn in (("harness.browserbridge", "register_browser_bridge"),
                    ("harness.native", "register_native"),
                    ("harness.mcpclient", "register_mcp_management")):
        try:
            __import__(mod)
            getattr(sys.modules[mod], fn)(reg)
        except Exception:
            pass
    live = sorted(reg.names())

    mode = Mode(args.mode) if getattr(args, "mode", None) else mode_from_env()
    blurb = {
        RiskClass.READ: "no side effects — never asks",
        RiskClass.WRITE_LOCAL: "changes files on this machine",
        RiskClass.EXEC: "runs commands on this machine",
        RiskClass.EXTERNAL: "reaches OFF this machine — your logged-in browser, "
                            "your desktop, a remote server",
    }
    print("mode: %s   (cwd %s)\n" % (mode.value, os.getcwd()))
    for cls in (RiskClass.READ, RiskClass.WRITE_LOCAL, RiskClass.EXEC, RiskClass.EXTERNAL):
        names = [n for n in live if R.classify(n) is cls]
        if not names:
            continue
        print("%-13s %s" % (cls.value, blurb[cls]))
        for i in range(0, len(names), 4):
            print("    " + "  ".join("%-24s" % n for n in names[i:i + 4]).rstrip())
        print()
    unclassified = [n for n in live if not R.is_classified(n)]
    if unclassified:
        print("NOT IN THE TABLE (treated as external until classified):")
        print("    " + "  ".join(unclassified))
        print()
    print("tools that can never carry an 'always allow' rule:")
    print("    " + "  ".join(sorted(n for n in R.NO_STANDING_RULE if n in live)))
    ov = RiskOverrideStore().list()
    if ov:
        print("\nyour overrides (most specific first):")
        for r in ov:
            print("    %-34s -> %s" % (r.pattern, r.risk.value))
    return 0


def cmd_risk_set(args):
    """Change a tool's risk class. The ONLY writer of the override store — deliberately
    not a tool, because something collie loaded must never be able to reclassify itself
    as harmless."""
    from .overrides import RiskOverrideStore
    store = RiskOverrideStore()
    if args.unset:
        print("removed" if store.unset(args.pattern) else "no such rule: %s" % args.pattern)
        return 0
    if not args.risk:
        print("need --risk read|write_local|exec|external (or --unset)")
        return 2
    store.set(args.pattern, args.risk)
    print("%s -> %s" % (args.pattern, args.risk))
    if args.risk == "read":
        print("  (read is never asked about — only do this for tools you have looked at)")
    return 0


def cmd_harnesses(args):
    from . import adapters
    print("== mainstream harness adapters ==")
    for a in adapters.ADAPTERS.values():
        base = adapters.MEASURED_PREFIX.get(a.key)
        print("  %-9s %-18s cli=%-13s %s  usage=%s  baseline=%s" % (
            a.key, a.label, a.cli,
            "INSTALLED" if a.available() else "—",
            "yes" if a.usage_supported else "no",
            base if base else "—"))
    print("\n  run:  python -m harness.cli compare --vs all --real")
    return 0


def cmd_dashboard(args):
    _, runs_db, out_html, _ = _paths()
    if not os.path.exists(runs_db):
        print("no runs.db yet — run `selftest` or `compare` first"); return 1
    dash.build(runs_db, out_html)
    print("dashboard -> %s" % out_html)
    return 0


def cmd_mem(args):
    mem_db, runs_db, _, _ = _paths()
    if args.action == "eval":
        from . import reval
        out = os.path.join(DATA, "retrieval_eval.json")
        r = reval.run_and_save(out, embed_name="local")
        for k in ("real", "hash"):
            e = r[k] or {}
            print("  %-6s %-24s P@1=%.2f P@5=%.2f MRR=%.2f (n=%d)" % (
                k, e.get("embedder", "?"), e.get("p_at_1", 0), e.get("p_at_5", 0),
                e.get("mrr", 0), e.get("n", 0)))
        print("  -> %s" % out)
        return 0
    review_actions = {"pending", "list", "approve", "attest", "reject", "invalidate"}
    # Reviewing an exact local row needs no embedding model (and must not start
    # a download just to approve a proposal).
    embedder = None if args.action in review_actions else _embedder(args.embed)
    m = SqliteMemory(mem_db, embedder=embedder)
    if args.action not in review_actions:
        print("  [embed] %s%s" % (
            m.embedder.name if m.embedder else "bm25-only",
            " (dim=%d)" % m.embedder.dim if m.embedder else ""))
    project = args.project or "demo"

    if args.action in ("pending", "list"):
        status = "proposed" if args.action == "pending" else (args.status or None)
        rows = m.list_claims(status=status, project=args.project or None, limit=args.limit)
        for row in rows:
            review = (" review=%s" % row["review_source"]
                      if row.get("review_source") else "")
            print("#%d [%-11s] project=%s source=%s%s\n  %s" % (
                row["id"], row["status"], row["project"], row["source"], review,
                (row["text"] or "")[:240]))
        if not rows:
            print("(no pending memory proposals)" if args.action == "pending"
                  else "(no memory claims)")
        m.close()
        return 0

    if args.action in ("approve", "attest", "reject", "invalidate"):
        try:
            memory_id = int(args.text)
            if memory_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            print("memory id must be a positive integer")
            m.close()
            return 2
        claim = m.get_claim(memory_id)
        if claim is None:
            print("no memory claim #%d" % memory_id)
            m.close()
            return 1
        provenance = "collie mem %s" % args.action
        if args.action in ("approve", "attest"):
            ok = m.promote(
                memory_id, status="attested",
                evidence=args.note or "local user attestation",
                review_source="local_user", review_provenance=provenance)
            message = "attested memory #%d as the local user" % memory_id
        elif args.action == "reject":
            ok = m.reject(
                memory_id, evidence=args.note or "local user rejected proposal",
                review_source="local_user", review_provenance=provenance)
            message = "rejected memory proposal #%d as the local user" % memory_id
        else:
            ok = m.invalidate(
                memory_id, evidence=args.note or "local user invalidated memory",
                review_source="local_user", review_provenance=provenance)
            message = "invalidated memory #%d as the local user" % memory_id
        if not ok:
            expected = "proposed" if args.action != "invalidate" else "recallable"
            print("memory #%d is %s, not %s; nothing changed" %
                  (memory_id, claim["status"], expected))
            m.close()
            return 1
        print(message)
        m.close()
        return 0

    if args.action == "import":
        from .mem_import import run_import
        run_import(m, source=args.source, limit=args.limit, dry_run=args.dry_run,
                   no_llm=args.no_llm, force=args.force,
                   provider_name=args.provider, model=args.model,
                   max_chunks=args.max_chunks, workers=args.workers)
    elif args.action == "purge-imported":
        from .mem_import import purge
        print("purged %d imported facts" % purge(m))
    elif args.action == "add":
        print("remembered #%d" % m.remember(args.text, project=project))
    elif args.action == "reembed":
        print("re-embedded %d facts with %s" % (m.reembed_all(), m.embed_model))
    else:
        hits = m.recall(args.text, project=project, k=8)
        for h in (hits or []):
            print("[%.3f] %s" % (h["score"], h["text"][:120]))
        if not hits:
            print("(no memories)")
    m.close()
    return 0


def _state_dir():
    """Where the delegate's durable state lives (~/.collie, overridable for tests
    via COLLIE_STATE_DIR). Matches the actions.py/jobs.py defaults."""
    d = os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie")
    os.makedirs(d, exist_ok=True)
    return d


def cmd_inbox(args):
    """Everything waiting on a person, in one list.

    Two subsystems park questions: a delegated job's irreversible action waits for a
    confirm token (actions.py), and a run's off-machine tool call waits for an approval
    (inbox.py). They are different mechanisms for good reasons — a job's action stops the
    run at a step boundary, a tool call suspends in place — but that distinction is
    collie's, not the user's. Somebody who wants to know what is waiting for them should
    not have to know which half of the codebase parked it.
    """
    from .inbox import STATE_PENDING, InboxStore

    store = InboxStore()
    try:
        if args.action in ("allow", "always", "deny", "never"):
            ok = store.resolve(args.id, args.action)
            print("%s %s" % ("answered" if ok else "could not answer (already decided,"
                             " unknown, or its run has ended):", args.id))
            return 0 if ok else 1

        items = store.pending() if args.action == "ls" else store.list(limit=args.limit)
        if not items:
            print("nothing is waiting for you.")
        else:
            print("── waiting for you (%d) ──" % sum(1 for i in items if i.state == STATE_PENDING))
            for i in items:
                mark = "?" if i.state == STATE_PENDING else ("→ " + (i.resolution or "?"))
                print("  %s  %-16s %-9s %s" % (i.id, i.tool, mark, i.body[:70]))
                if i.target:
                    print("      on %s" % i.target)
                if i.rule_offer and i.state == STATE_PENDING:
                    print("      'always' would allow: %s" % i.rule_offer)
            print("\n  collie inbox allow <id> | always <id> | deny <id> | never <id>")

        # The delegate half. Its confirm tokens live in a different store and are answered
        # with `collie jobs confirm`; listing them here is what makes this one list.
        try:
            from .actions import ActionStore
            pend = ActionStore(os.path.join(_state_dir(), "actions.db")).pending()
            if pend:
                print("\n── delegated actions awaiting confirm (%d) ──" % len(pend))
                for a in pend:
                    print("  %s  %s" % (a["nonce"], a["capability"]))
                print("\n  collie jobs confirm <nonce>")
        except Exception:
            pass                       # no delegate store yet is not a problem to report
        return 0
    finally:
        store.close()


def cmd_jobs(args):
    """The human surface for delegated work: see jobs, confirm gated (irreversible)
    actions, read receipts. The confirm step approves a CONCRETE materialized
    payload; execution runs only in a process where the capability is registered
    (a runner/daemon), so a bare `collie jobs confirm` approves and reports —
    the model never executes here."""
    from .actions import ActionStore, RefusedError
    from .jobs import JobStore, Executor, NEEDS_YOU
    from . import capabilities as _caps
    _caps.register_builtins()          # make shipped capabilities executable here
    d = _state_dir()
    acts = ActionStore(os.path.join(d, "actions.db"))
    jobs = JobStore(os.path.join(d, "jobs.db"))
    rc = 0
    try:
        if args.action == "ls":
            js = jobs.list()
            if not js:
                print("(no jobs)")
            for j in js:
                print("  %-12s %-14s %s" % (j.job_id, j.state, (j.goal or "")[:60]))
        elif args.action == "inbox":
            pend = acts.pending()
            print("── pending confirmations (%d) ──" % len(pend))
            for p in pend:
                print("  %s  %s  %s" % (p["nonce"], p["capability"],
                                        (p["args_json"] or "")[:80]))
            ny = jobs.list(state=NEEDS_YOU)
            print("── jobs needing you (%d) ──" % len(ny))
            for j in ny:
                print("  %-12s %s" % (j.job_id, (j.goal or "")[:60]))
            if not pend and not ny:
                print("  (nothing waiting)")
        elif args.action == "confirm":
            nonce = args.text
            rec = acts.get(nonce)
            if not rec:
                print("unknown nonce"); return 1
            # A Mission action must go back through its campaign driver. Running it
            # with the one-shot Job Executor would fire the side effect but leave
            # the Mission's parked step unresolved (and could bypass pause/cancel).
            from .mission import MissionStore
            mstore = MissionStore(os.path.join(d, "jobs.db"))
            try:
                owner = mstore.get(rec.job_id) if rec.job_id else None
            finally:
                mstore.close()
            if owner:
                from . import settings as _mst
                from .missionweb import MissionService
                _mst.apply()
                svc = MissionService(state_dir=d)
                try:
                    out = svc.confirm(owner.mission_id, nonce)
                    print("mission %s → %s%s" % (
                        owner.mission_id, out.get("state", "unknown"),
                        (": " + out["error"]) if out.get("error") else ""))
                    return 1 if out.get("error") else 0
                finally:
                    svc.close()
            # confirm() raises on a non-pending nonce (already approved/executed);
            # don't let that abort the command — report state and, if it already
            # fired, reconcile the job from its receipt rather than crashing.
            try:
                acts.confirm(nonce)
                print("approved %s  cap=%s  digest=%s…" % (nonce, rec.capability, rec.digest[:12]))
            except RefusedError as e:
                print("not confirming %s: %s" % (nonce, e))
            try:
                v = Executor(acts, jobs).run_confirmed(nonce, job_id=rec.job_id)
                print("executed → %s: %s" % (v.status, v.reason))
            except RefusedError as e:
                print("not executed here (%s)." % e)
                print("a runner with the capability loaded will execute it.")
        elif args.action == "run":
            # collie jobs run <capability> '<json-args>' — create a job, propose the
            # action, and DRIVE it: a reversible in-scope capability runs live and
            # verifies; an irreversible one parks in needs_you awaiting confirm.
            import json as _json
            cap = args.text
            if not cap:
                print("usage: collie jobs run <capability> '<json-args>' [--goal ...]"); return 1
            try:
                cap_args = _json.loads(args.jargs) if args.jargs else {}
            except _json.JSONDecodeError as e:
                print("bad json args: %s" % e); return 1
            import secrets as _s
            jid = "job-" + _s.token_hex(4)
            jobs.create(jid, args.goal or cap, leash=_json.loads(args.leash) if args.leash else {})
            nonce = acts.propose(cap, cap_args, job_id=jid)
            print("job %s  proposed %s (%s)" % (jid, cap, nonce[:12]))
            try:
                v = Executor(acts, jobs).drive(nonce)
                print("→ %s: %s   [job %s]" % (v.status, v.reason, jobs.get(jid).state))
            except RefusedError as e:
                print("refused: %s" % e)
        elif args.action == "wake":
            # catch-up-on-wake: fire every overdue wait now (what the daemon does
            # on start / each tick). Durable waits survive restart; this drains them.
            from .scheduler import Scheduler
            import time as _t
            sched = Scheduler(acts, jobs, db_path=os.path.join(d, "jobs.db"))
            fired = sched.tick(int(_t.time()))
            print("catch-up: fired %d due wait(s); %d still pending"
                  % (fired, len(sched.pending_waits())))
            sched.close()
            try:
                from . import settings as _mst
                from .missionweb import MissionService
                _mst.apply()
                msvc = MissionService(state_dir=d)
                mout = msvc.tick(now=int(_t.time()))
                print("missions: advanced %d" % mout.get("advanced", 0))
                msvc.close()
            except Exception as e:
                print("missions not advanced: %s" % e)
        elif args.action == "ask":
            # natural language -> compile to a job -> drive it
            from . import mandate
            from .jobs import Executor
            import secrets as _s2
            text = (args.text + (" " + args.jargs if args.jargs else "")).strip()
            if not text:
                print('usage: collie jobs ask "记一下 今晚买菜"'); return 1
            prov = None
            try:
                from . import settings as _st
                from .providers import make_provider
                _st.apply()
                prov = make_provider(_st.get("PROVIDER"), _st.get("MODEL"))
            except Exception:
                pass
            plan = mandate.compile(text, prov)
            if not plan.get("capability"):
                print("🤔 " + (plan.get("clarify") or "not sure what to do")); return 0
            print("understood → %s %s" % (plan["capability"], plan.get("args")))
            jid = "job-" + _s2.token_hex(4)
            jobs.create(jid, plan.get("goal") or text, leash=plan.get("leash") or {})
            nonce = acts.propose(plan["capability"], plan.get("args") or {}, job_id=jid)
            try:
                v = Executor(acts, jobs).drive(nonce)
                print("→ %s: %s   [job %s]" % (v.status, v.reason, jobs.get(jid).state))
            except RefusedError as e:
                print("refused: %s" % e)
        elif args.action == "web":
            # the delegation-first dashboard (Today / Inbox / Receipts).
            from .jobsweb import serve
            serve(port=int(args.port) if args.port else 8794, state_dir=d)
        elif args.action == "daemon":
            # colliejobd: catch up on start, then tick jobs plus model-driven
            # Missions on an interval (the Mission lane cannot delay reminders).
            from .scheduler import Scheduler
            sched = Scheduler(acts, jobs, db_path=os.path.join(d, "jobs.db"))
            from . import settings as _mst
            from .missionweb import MissionService
            _mst.apply()
            msvc = MissionService(state_dir=d)
            last_mission_error = [""]

            def _mission_tick(now):
                try:
                    msvc.tick(now=now)
                    last_mission_error[0] = ""
                except Exception as e:
                    msg = "%s: %s" % (type(e).__name__, e)
                    if msg != last_mission_error[0]:
                        print("mission tick paused: %s" % msg)
                        last_mission_error[0] = msg

            print("colliejobd: jobs + missions, catch-up + tick every %ss (Ctrl-C to stop)"
                  % args.interval)
            try:
                sched.serve(interval=float(args.interval), extra_tick=_mission_tick)
            except KeyboardInterrupt:
                print("\ncolliejobd stopped")
            finally:
                msvc.close()
                sched.close()
        elif args.action == "receipts":
            rows = acts.receipts(args.text or None)
            if not rows:
                print("(no receipts)")
            for r in rows:
                print("  %s  %s  fired=%s  %s: %s" % (
                    r["capability"], r["nonce"][:12], r["fired"],
                    r["verdict"], (r["verdict_reason"] or "")[:60]))
                if r["evidence"]:
                    print("      evidence: %s" % r["evidence"][:100])
    finally:
        acts.close()
        jobs.close()
    return rc


def cmd_mission(args):
    """Manage durable campaigns without opening the Web UI."""
    import json as _json
    from . import settings as _mst
    from .missionweb import MissionService
    _mst.apply()
    svc = MissionService(state_dir=_state_dir())
    try:
        action = args.action
        if action == "ls":
            out = {"missions": svc.missions()}
        elif action == "start":
            goal = (args.text or "").strip()
            if not goal:
                print('usage: collie mission start "<goal>" [--review] '
                      '[--code --workspace PATH] [--overnight]'); return 1
            bounds = {}
            if args.domains:
                bounds["allowed_domains"] = [x.strip() for x in args.domains.split(",")
                                              if x.strip()]
            if args.actions_per_hour is not None:
                bounds["actions_per_hour"] = args.actions_per_hour
            if args.max_actions is not None:
                bounds["max_irreversible_actions"] = args.max_actions
            if args.max_steps is not None:
                bounds["max_total_steps"] = args.max_steps
            try:
                autonomy = True if args.auto else (False if args.review else None)
                billing_evidence = None
                if args.billing_evidence:
                    billing_evidence = _json.loads(args.billing_evidence)
                    if not isinstance(billing_evidence, dict):
                        raise ValueError("--billing-evidence must decode to a JSON object")
                out = svc.start(
                    goal, autonomous=autonomy, code=bool(args.code),
                    workspace=args.workspace or "", overnight=bool(args.overnight),
                    verify_command=args.verify_command or "",
                    no_paid_overage=bool(args.no_paid_overage),
                    billing_evidence=billing_evidence,
                    provider=args.mission_provider or "",
                    model=args.mission_model or "", **bounds)
            except (ValueError, RuntimeError, _json.JSONDecodeError) as e:
                print("invalid Mission: %s" % e); return 1
            if args.run and not out.get("error"):
                out = svc.run(out["mission_id"])
        else:
            mid = (args.text or "").strip()
            if not mid:
                print("usage: collie mission %s <mission-id>" % action); return 1
            if action == "status":
                out = svc.status(mid)
            elif action == "report":
                out = svc.report(mid)
            elif action == "run":
                out = svc.run(mid)
            elif action == "pause":
                out = svc.pause(mid)
            elif action == "resume":
                out = svc.resume(mid)
            elif action == "retry":
                out = svc.retry(mid, args.note or "")
            elif action == "cancel":
                out = svc.cancel(mid)
            elif action == "accept":
                out = svc.accept(mid)
            elif action == "continue":
                out = svc.continue_after_human(mid, args.note or "")
            elif action == "reconcile":
                out = svc.reconcile(
                    mid, args.note or "", args.code_resolution or "")
            elif action == "check":
                out = svc.check(mid)
            else:  # confirm
                nonce = (args.nonce or "").strip()
                if not nonce:
                    print("usage: collie mission confirm <mission-id> <nonce>"); return 1
                out = svc.confirm(mid, nonce)
        if args.json:
            print(_json.dumps(out, ensure_ascii=False))
        elif action == "report":
            print(out.get("error") or out.get("markdown") or "progress report unavailable")
        elif action == "ls":
            rows = out.get("missions", [])
            if not rows:
                print("(no missions)")
            for m in rows:
                print("  %-16s %-14s %s" % (
                    m["mission_id"], m["state"], (m.get("goal") or "")[:70]))
        else:
            print("%s  %s  %s" % (
                out.get("mission_id", ""), out.get("state", "unknown"),
                out.get("error") or out.get("result") or out.get("goal") or ""))
            if action == "start" and not args.run and not out.get("error"):
                print("queued; `collie jobs daemon` will run it, or use `collie mission run %s`"
                      % out["mission_id"])
        return 1 if out.get("error") else 0
    finally:
        svc.close()


def cmd_init(args):
    """collie init — one-time PROJECT prep for the current repo. code_search is ripgrep-backed now
    (no index to build), so this front-loads the memory embedder's first-use model download and
    validates the codemap. --rules additionally has the MODEL explore the repo and write an AGENTS.md
    (the opencode `/init` convention); collie reads AGENTS.md / CLAUDE.md as project rules every run.
    For machine-level setup (install deps, pick a provider), use `collie setup`."""
    import time as _t
    cwd = os.path.abspath(args.cwd or os.getcwd())
    print("collie init · %s" % cwd)
    if not args.no_config:
        _setup_wizard(force=True)     # provider/model first — init is also "set me up" (tty only)
    t0 = _t.time()
    # init's job IS to warm — download and wait here on purpose (unlike a normal run, which
    # backgrounds the download). Build directly so the auto-path's non-blocking fallback is bypassed.
    if args.embed in ("bm25", "none", "off", "sparse"):
        emb = None
    elif args.embed in ("auto", "granite", "local", "default", "daemon"):
        try:
            emb = make_embedding("granite")
        except Exception as e:
            print("  · semantic memory unavailable (%s) — BM25 keyword recall" % (str(e)[:100]))
            emb = None
    else:
        emb = _embedder(args.embed)
    if emb is not None:
        emb.embed("warm-up", kind="query")
        print("  ✓ semantic memory ready: %s (dim=%d)  [%.1fs]" % (emb.name, emb.dim, _t.time() - t0))
    else:
        print("  · semantic memory unavailable — BM25 keyword recall (run `collie setup` to enable)")
    from . import codemap                             # codemap (cheap; validates the map view)
    tree = codemap.build_tree(cwd)
    print("  ✓ codemap: %d files · %d defs" % (len(tree), sum(f.get("defs", 0) for f in tree)))
    if args.rules:                                    # 4) optional: model-written AGENTS.md
        existing = [f for f in ("AGENTS.md", "CLAUDE.md", ".collie.md") if os.path.exists(os.path.join(cwd, f))]
        if existing:
            print("  · rules file already present (%s) — skipping generation" % ", ".join(existing))
        else:
            print("  … generating AGENTS.md with the model (one short run)")
            provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
            h = make_harness(cwd, provider=provider, project="init")
            res = h.run("init", (
                "Explore this repository briefly (README, entry points, key modules, how tests run) "
                "and CREATE a concise AGENTS.md at the repo root with: what the project is (2-3 "
                "sentences), the layout (key dirs/files), how to build/run/test, and any conventions "
                "an agent must follow. Write the file with write_file. Keep it under 80 lines."))
            ok = os.path.exists(os.path.join(cwd, "AGENTS.md"))
            print(("  ✓ AGENTS.md written" if ok else "  ✗ AGENTS.md not written (%s)" %
                   (res.error or "model finished without writing")))
    print("done in %.1fs — collie is warm; first question won't pay the indexing cost." % (_t.time() - t0))
    return 0


def cmd_setup(args):
    """collie setup — machine-level onboarding ("collie doctor" + one-click install). Checks the
    environment (POSIX shell, ripgrep, the ONNX deps for semantic memory), installs the missing
    Python pieces with ONE confirmation, prints OS-specific hints for the non-pip tools, and picks
    a provider. `--check` diagnoses only (installs nothing); `--yes` installs without prompting."""
    import importlib.util as _il
    import shutil as _sh
    import subprocess as _sp
    from . import plat
    check_only = getattr(args, "check", False)
    assume_yes = getattr(args, "yes", False)
    print("collie setup · %s\n" % plat.os_label())

    def have(mod):
        return _il.find_spec(mod) is not None

    # 1) POSIX shell (the cross-platform shell contract) ----------------------------------------
    sh = plat.posix_shell()
    if sh:
        print("  ✓ POSIX shell: %s" % sh)
    else:
        hint = ("winget install Git.Git  (Git Bash)" if plat.is_windows()
                else "your package manager (bash ships with the OS)")
        print("  ✗ POSIX shell: none — the `bash` tool degrades to cmd.exe.\n    install: %s" % hint)

    # 2) ripgrep (code_search backend; grep is the fallback) ------------------------------------
    if _sh.which("rg"):
        print("  ✓ ripgrep: %s" % _sh.which("rg"))
    elif _sh.which("grep"):
        print("  · ripgrep not found — using grep (fine; rg is faster on big repos)")
    else:
        rg_hint = ("winget install BurntSushi.ripgrep.MSVC" if plat.is_windows()
                   else "brew install ripgrep" if plat.is_macos() else "apt install ripgrep")
        print("  ✗ no ripgrep or grep — code_search needs one.  install: %s" % rg_hint)

    # 3) semantic-memory deps (granite via onnxruntime) ----------------------------------------
    need = [m for m in ("onnxruntime", "tokenizers", "huggingface_hub", "numpy") if not have(m)]
    if not need:
        print("  ✓ semantic memory deps present (onnxruntime, tokenizers, huggingface_hub, numpy)")
    else:
        print("  ✗ semantic memory needs: %s" % ", ".join(need))
        if check_only:
            print("    install: pip install collie-harness[local]")
        else:
            ok = assume_yes or _confirm("  install semantic-memory deps now (pip install "
                                        "collie-harness[local])?")
            if ok:
                rc = _sp.run([sys.executable, "-m", "pip", "install",
                              "onnxruntime", "tokenizers", "huggingface_hub", "numpy"]).returncode
                print("  %s deps install" % ("✓" if rc == 0 else "✗"))
            else:
                print("  · skipped — memory runs on BM25 keyword recall until installed")

    # 4) pre-download the default model so the first run is instant -----------------------------
    if not check_only and not need and have("onnxruntime"):
        want = assume_yes or _confirm("  pre-download the granite semantic model (~55MB) now?")
        if want:
            try:
                from .embeddings import make_embedding
                e = make_embedding("granite")
                print("  ✓ model ready: %s (dim=%d)" % (e.name, e.dim))
            except Exception as e:
                print("  ✗ model download failed (%s) — will retry on first use; for a mirror set "
                      "COLLIE_HF_ENDPOINT=https://hf-mirror.com" % (type(e).__name__))

    # 5) real-browser bridge -------------------------------------------------------------------
    # Without this, collie's browser_* tools fall back to a logged-out scratch browser and every
    # "check my account" task fails confusingly. The classic failure: the Chrome extension IS
    # loaded, but nobody ever started the local server it polls.
    from . import browserbridge as _bb
    _ext = os.path.join(os.path.dirname(os.path.abspath(_bb.__file__)), "browser_ext")
    if _bb._bridge_live():
        # Compare the LOADED extension's version with the one we ship. A mismatch means Chrome is
        # running a copy from some other path (a second checkout, a \wsl$ share) — every fix you
        # make here is invisible to it, which is maddening to debug without this line.
        import json as _json
        import urllib.request as _urlreq
        want = ""
        try:
            with open(os.path.join(_ext, "manifest.json"), encoding="utf-8") as _f:
                want = (_json.load(_f) or {}).get("version", "")
        except Exception:
            pass
        got = ""
        try:
            with _urlreq.urlopen("http://127.0.0.1:%d/health" % _bb._port(), timeout=2) as _r:
                got = (_json.loads(_r.read() or b"{}") or {}).get("extension_version", "")
        except Exception:
            pass
        if want and got and want != got:
            print("  ! real browser: bridge live, but the loaded extension is v%s while this collie "
                  "ships v%s\n    it is loaded from ANOTHER copy — remove it and Load unpacked: %s"
                  % (got, want, _ext))
        else:
            print("  ✓ real browser: bridge live, extension connected%s" % (" (v%s)" % got if got else ""))
    elif _bb._server_up(_bb._port()):
        print("  · real browser: bridge running, but no extension connected.\n"
              "    load it: chrome://extensions → Developer mode → Load unpacked → %s" % _ext)
    else:
        print("  ✗ real browser: bridge not running — browser tools would use a LOGGED-OUT browser")
        if check_only:
            print("    fix: collie browser-bridge   (and load %s in chrome://extensions)" % _ext)
        elif assume_yes or _confirm("  start the browser bridge now and run it at every logon?"):
            ok = _bb.start_background()
            print("  %s bridge started" % ("✓" if ok else "✗"))
            _bb.install_autostart()
            if ok and not _bb._bridge_live():
                print("    now load the extension: chrome://extensions → Developer mode → "
                      "Load unpacked → %s" % _ext)

    # 6) provider (interactive) ------------------------------------------------------------------
    if not check_only:
        print("")
        _setup_wizard(force=True)
    print("\nsetup %s." % ("check complete" if check_only else "complete — try: collie -p \"explain this repo\""))
    return 0


def _confirm(prompt):
    """Yes/no on a tty; default NO off a tty (non-interactive/CI never auto-installs)."""
    try:
        if not sys.stdin.isatty():
            return False
        return input(prompt + " [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        return False


def cmd_config(args):
    """collie config — read/write ~/.collie/settings.json from the command line.

        collie config            list every knob and its effective value
        collie config LANG       print one
        collie config LANG zh    set one (merges; never clobbers the other keys)

    Scriptable counterpart to the GUI Settings panel — it's how the Windows installer hands the
    language you picked in the wizard to the app, so the first launch is already in your language.
    """
    from . import settings
    keys = [s["key"] for s in settings.SCHEMA]
    if not args.key:
        vals = settings.all_values()
        for k in keys:
            print("%-18s %s" % (k, vals.get(k, "")))
        return 0
    key = args.key.upper()
    if key not in keys:
        print("collie config: unknown key %r (try `collie config` to list them)" % args.key,
              file=sys.stderr)
        return 2
    spec = next(s for s in settings.SCHEMA if s["key"] == key)
    if args.value is None:
        print(settings.get(key, spec.get("default", "")) or "")
        return 0
    # validate against the schema's own options — a typo'd language should fail loudly, not
    # silently persist a value nothing reads
    opts = [o["value"] for o in spec.get("options", [])]
    if opts and args.value not in opts:
        print("collie config: %s must be one of: %s" % (key, ", ".join(opts)), file=sys.stderr)
        return 2
    settings.update({key: args.value})
    print("%s = %s" % (key, args.value))
    return 0


def cmd_mcp(args):
    from . import mcpclient as mc
    servers = mc._load_config()
    if args.action == "list":
        if not servers:
            # An empty list used to end at "add one with a url-or-command", which is a strange thing
            # to ask of the screen whose job is to say what exists. Name what can be connected with
            # one word and no URL at all.
            print("(no MCP servers configured)")
            print("  connect one in a single step — signs in through your browser, no token to find:")
            print("    " + "  ".join(sorted(k for k, v in mc.CATALOG.items()
                                            if not v.get("byo_client"))))
            print("  e.g. `collie mcp connect linear`")
            # Listed apart rather than mixed in: they are one press plus an OAuth app you have to
            # create, and finding that out by pressing is the thing this line exists to prevent.
            print("  these need an OAuth app of your own (no dynamic client registration):")
            print("    " + "  ".join(sorted(k for k, v in mc.CATALOG.items()
                                            if v.get("byo_client"))))
            print("  anything else: `collie mcp add <name> <https://url | shell command>`")
            return 0
        for s in mc.status():
            tools = "?" if s["tools"] is None else str(s["tools"])
            state = "" if s["enabled"] else "  OFF"
            auth = {"none": "", "header": "  [static-header]", "oauth": "  [oauth ✓]",
                    "login-needed": "  [oauth — run: collie mcp connect %s]" % s["name"]}[s["auth"]]
            print("  %-16s %-6s %-40s %s tools%s%s"
                  % (s["name"], s["kind"], s["target"][:40], tools, auth, state))
        return 0
    if args.action == "connect":
        # The whole thing in one command: fill in the address, then the browser handshake. Everything
        # it needs was already here — the address was the only missing piece, and asking the user for
        # it is asking them for the one thing they came here to be told.
        hit = mc.known(args.name)
        if not hit:
            print("%r is not a service collie knows the address of." % args.name)
            print("  known: " + "  ".join(sorted(mc.CATALOG)))
            print("  for anything else: collie mcp add <name> <https://url | shell command>")
            return 1
        name = hit["name"]
        cfg = servers.get(name)
        if hit.get("byo_client") and not (cfg or {}).get("client_id"):
            # Before adding anything: a server in the config that can never sign in is worse than
            # no server, because the list then says it is one Sign-in press away.
            print(mc.byo_client_help(name, hit["label"], hit["url"]))
            return 1
        if not cfg:
            err = mc.add_server(name, {"url": hit["url"]}, replace=False)
            if err:
                print(err)
                return 1
            print("added %s → %s" % (name, hit["url"]))
        servers = mc._load_config()
        args.action, args.name = "login", name        # fall through to the browser handshake
    if args.action == "add":
        # `collie mcp add linear https://mcp.linear.app/mcp` (remote),
        # `collie mcp add fs "npx -y @modelcontextprotocol/server-filesystem /tmp"` (stdio), or
        # `collie mcp add slack` — a name on its own, for a service whose address is in the catalog.
        if not args.name:
            print("usage: collie mcp add <name> <https://url | shell command>")
            return 1
        if not args.value:
            hit = mc.known(args.name)
            if not hit:
                print("usage: collie mcp add <name> <https://url | shell command>")
                print("  (or a name on its own, for: " + "  ".join(sorted(mc.CATALOG)) + ")")
                return 1
            err = mc.add_server(hit["name"], {"url": hit["url"]},
                                replace=bool(getattr(args, "force", False)))
            if err:
                print(err)
                return 1
            print("added %s → %s" % (hit["name"], hit["url"]))
            print("  sign in: collie mcp connect %s" % hit["name"])
            return 0
        if args.value.startswith(("http://", "https://")):
            cfg = {"url": args.value}
        else:
            parts = args.value.split()
            cfg = {"command": parts[0], "args": parts[1:]} if len(parts) > 1 else {"command": parts[0]}
        err = mc.add_server(args.name, cfg, replace=bool(getattr(args, "force", False)))
        if err:
            print(err)
            return 1
        print("added %s" % args.name)
        if cfg.get("url"):
            print("  if it needs OAuth: collie mcp login %s" % args.name)
        return 0
    if args.action in ("remove", "enable", "disable"):
        if not args.name:
            print("usage: collie mcp %s <name>" % args.action)
            return 1
        if args.action == "remove":
            if not mc.remove_server(args.name):
                print("no such server: %r" % args.name)
                return 1
            print("removed %s (config, cached tool list and stored token)" % args.name)
            return 0
        on = args.action == "enable"
        if not mc.set_enabled(args.name, on):
            print("no such server: %r" % args.name)
            return 1
        print("%s %s — takes effect on the next collie run" % ("enabled" if on else "disabled", args.name))
        return 0
    cfg = servers.get(args.name) if args.name else None
    if args.action in ("login", "tools") and not cfg:
        print("no such server: %r (see `collie mcp list`)" % args.name)
        return 1
    if args.action == "login":
        try:
            mc.login(args.name, cfg)
        except Exception as e:
            print("login failed: %s" % e)
            return 1
        print("✓ authorized %s — refreshing tool cache…" % args.name)
        try:                                    # re-list now that we're authorized, so tools cache warms
            cache = mc._read_cache()
            conn = mc._get_conn(args.name, cfg)
            tools = [{"name": t.get("name"), "description": t.get("description", ""),
                      "inputSchema": t.get("inputSchema") or t.get("input_schema")}
                     for t in conn.list_tools() if t.get("name")]
            cache[args.name] = {"hash": mc._cfg_hash(cfg), "tools": tools}
            mc._write_cache(cache)
            print("  %d tools available" % len(tools))
        except Exception as e:
            print("  (authorized, but tool list failed: %s)" % e)
        return 0
    if args.action == "logout":
        toks = mc._load_tokens()
        existed = toks.pop(args.name, None) is not None
        mc._save_tokens(toks)
        print("logged out %s" % args.name if existed else "no stored token for %s" % args.name)
        return 0
    if args.action == "tools":
        try:
            conn = mc._get_conn(args.name, cfg)
            tools = conn.list_tools()
        except Exception as e:
            print("list failed: %s" % e)
            return 1
        for t in tools:
            print("  mcp__%s__%s — %s" % (args.name, t.get("name"), (t.get("description") or "")[:70]))
        if not tools:
            print("  (no tools)")
        return 0
    return 0


CMDS = {"selftest", "run", "prefix", "pack", "compare", "harnesses", "dashboard", "mem", "acp",
        "loop", "repl", "tui", "web", "app", "wallpaper", "browser-bridge", "slack", "record", "mcp", "mail", "init",
        "setup", "jobs", "mission", "config", "uninstall", "update", "menubar", "risk", "inbox", "trust", "audit",
        "activity", "recovery", "hooks", "supervisor", "automations", "library"}


def _setup_wizard(force=False):
    """Interactive provider/model setup, saved to ~/.collie/settings.json. Two entries:

    bare `collie` (force=False) — one-time onboarding, only when NOTHING is configured (no
    COLLIE_PROVIDER env, no saved PROVIDER); a short curated list, the opencode/hermes convention.
    `collie init` (force=True) — ALWAYS offered (init is the canonical "set me up" command):
    the full provider menu straight from the settings SCHEMA (single source of truth — a provider
    added there appears here with zero wizard edits), current values prefilled, model asked too.

    Non-tty always skips (CI/scripts stay non-interactive); the full knob set lives in the web
    Settings panel. anthropic-oauth stays a deliberate pick, never a silent default."""
    from . import settings as st
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    if not force and (os.environ.get("COLLIE_PROVIDER") or st._load().get("PROVIDER")):
        return
    cur = st.get("PROVIDER") or ""
    if force:
        sch = {s["key"]: s for s in st.SCHEMA}
        opts = [(o["value"], o["label"]) for o in sch["PROVIDER"]["options"]]
        print("Where should completions come from? (Enter keeps the current choice)\n")
    else:
        opts = [
            ("anthropic-oauth", "Claude direct (experimental; reuses login, availability/billing unverified)"),
            ("anthropic",       "Anthropic API key (metered — needs ANTHROPIC_API_KEY exported)"),
            ("ollama",          "Ollama (local models — nothing leaves this machine)"),
            ("mock",            "Mock (offline demo — try the harness before connecting anything)"),
        ]
        print("Welcome to collie — one-time setup. Where should completions come from?\n")
    for i, (val, label) in enumerate(opts, 1):
        print("  %d) %s%s" % (i, label, "   ← current" if val == cur else ""))
    if not force:
        print("\n(more providers + models: `collie init`, or the Settings panel in `collie web`)")
    # Enter = keep the current provider; with nothing configured yet it means the schema default
    # ("anthropic") on init, or the recommended option 1 on first run — NEVER the runtime's mock.
    default = cur or (sch["PROVIDER"]["default"] if force else opts[0][0])
    try:
        c = input("Choice [1-%d, Enter = %s]: " % (len(opts), default)).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    pick = opts[int(c) - 1][0] if c.isdigit() and 1 <= int(c) <= len(opts) else default
    data = dict(st._load())            # save() replaces the whole file — merge, don't clobber
    data["PROVIDER"] = pick
    if force:                          # model id, prefilled; `-` clears back to the provider default
        curm = st.get("MODEL") or ""
        # suggest models of the picked provider's family; the generic head of the list otherwise
        fam = {"anthropic": "claude", "anthropic-oauth": "claude", "claude-cli": "claude"}.get(
            pick, pick.split("-")[0])
        sug = [m for m in sch["MODEL"]["list"] if fam in m] or sch["MODEL"]["list"][:4]
        print("\nModel id for %s (e.g. %s)" % (pick, ", ".join(sug[:4])))
        hint = "Enter = %s" % (curm or "provider default")
        if curm:
            hint += ", `-` = provider default"
        try:
            m = input("Model [%s]: " % hint).strip()
        except (EOFError, KeyboardInterrupt):
            m = ""
        if m == "-":
            data.pop("MODEL", None)
        elif m:
            data["MODEL"] = m
    st.save(data)
    st.apply()                         # same-process pickup (e.g. init --rules runs right after)
    print("→ provider = %s%s (saved to %s)"
          % (pick, ", model = " + data["MODEL"] if data.get("MODEL") else "", st._PATH))
    hard = os.environ.get("COLLIE_PROVIDER") if "COLLIE_PROVIDER" in st._HARD_ENV else None
    if hard and hard != pick:
        print("  note: COLLIE_PROVIDER=%s is exported in this shell and overrides the saved value." % hard)
    if pick == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
        print("  next: export ANTHROPIC_API_KEY=sk-ant-…   (add it to your shell profile)")
    elif pick == "anthropic-oauth":
        print("  note: reuses your Claude Code OAuth token — run `claude` once if you never logged in.")
    else:
        # schema labels carry the key env var, e.g. "DeepSeek (DEEPSEEK_API_KEY) ☁" — hint if unset
        mkey = re.search(r"\(([A-Z][A-Z0-9_]*_API_KEY)\)", dict(opts)[pick])
        if mkey and not os.environ.get(mkey.group(1)):
            print("  next: export %s=…   (add it to your shell profile)" % mkey.group(1))
    print()


def _first_run_wizard():
    _setup_wizard(force=False)


def main(argv=None):
    from . import plat as _plat
    # Before anything prints: on a console that cannot encode what collie writes, `print` raises and
    # the command dies. One ✓ was enough to make `collie init` exit 1 with half a line written.
    _plat.make_output_safe()
    from . import settings as _settings
    _settings.apply()   # inject saved Settings-panel values into os.environ (real env vars still win)
    argv = list(sys.argv[1:] if argv is None else argv)
    # headless one-liner:  collie "task"  |  collie -p "task"
    if argv and argv[0] in ("-p", "--print"):
        argv = ["run", "-p"] + argv[1:]
    elif argv and argv[0] not in CMDS and not argv[0].startswith("-"):
        argv = ["run"] + argv
    elif not argv and sys.stdin.isatty():
        # bare `collie` = the default interactive surface (the opencode/hermes convention).
        # (the first-run wizard fires at dispatch below — it covers every chat surface, not just this)
        try:
            import rich  # noqa: F401
            argv = ["tui"]
        except ImportError:
            argv = ["repl"]           # stdlib-only fallback — and SAY so, or nobody learns the TUI exists
            print("(rich not installed — plain repl. `pipx inject collie-harness rich` unlocks `collie tui`)")

    p = argparse.ArgumentParser(prog="collie", description="collie — evolvable coding-agent harness")
    # every packaging path wants to ask a built binary what it is — the Homebrew formula's `test do`,
    # a bug report, `spctl` triage on the .app. Subcommands make that awkward, so it lives up here.
    p.add_argument("--version", action="version", version="collie %s" % __version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest").set_defaults(fn=cmd_selftest)

    pr = sub.add_parser("run", help="run one task headlessly (collie -p \"task\")")
    pr.add_argument("task")
    pr.add_argument("--provider", default=None,
                    help="mock|ollama|anthropic|deepseek|qwen|... (env COLLIE_PROVIDER)")
    pr.add_argument("--model", default=None,
                    help="pin this exact model; omitted lets saved config/task routing choose")
    pr.add_argument("--intent", choices=["build", "plan", "test", "review"], default=None,
                    help="execution intent (default: Auto from the task)")
    pr.add_argument("--quality", choices=["quick", "balanced", "thorough"], default=None,
                    help="run depth (default: Auto from task complexity)")
    pr.add_argument("--verification", choices=["auto", "required"], default=None,
                    help="whether an executed check is a hard finish condition")
    pr.add_argument("--effort", choices=["auto", "low", "medium", "high", "xhigh", "max"],
                    default=None, help="model reasoning effort (default: Auto by task)")
    pr.add_argument("--speed", choices=["standard", "fast"], default=None,
                    help="provider service tier; Fast keeps the same model and may cost more")
    pr.add_argument("--verify-command", default=None,
                    help="editable objective check for Test/Required (otherwise detect from repo)")
    pr.add_argument("--cwd", default=None); pr.add_argument("--project", default="demo")
    pr.add_argument("-p", "--print", action="store_true", help="print only the answer")
    pr.add_argument("--json", action="store_true", help="print a JSON result")
    pr.add_argument("--stream-json", action="store_true", dest="stream_json",
                    help="stream NDJSON events (tool/edit/repro/receipt) to stderr as they "
                         "happen — for live UX / editor extension / ACP adapter")
    pr.add_argument("--mode", default=None, choices=["plan", "project", "interactive", "auto"],
                    help="how much collie may do without asking. project (default): reads, "
                         "writes and commands inside this directory go ahead — you consented to "
                         "that by running collie here — while anything reaching OFF this machine "
                         "(your logged-in browser, your desktop, an MCP server) asks first. "
                         "plan: read-only. interactive: ask before every write and command. "
                         "auto: ask nothing (sandboxes and CI). Also COLLIE_MODE.")
    pr.add_argument("--persona", default=None,
                    help="a role from .collie/personas or ~/.collie/personas: its identity, "
                         "its tool allowlist, and its mode. A persona can only NARROW what "
                         "you already allowed — it can never widen it.")
    pr.add_argument("--web-search", action="store_true", dest="web_search",
                    help="enable the web_search tool (keyless DuckDuckGo, or a browser-extension "
                         "bridge via COLLIE_WEBSEARCH_BRIDGE)")
    pr.add_argument("--goal", default=None,
                    help="pin a standing goal into CORE memory (loaded every turn)")
    pr.add_argument("--continue", dest="cont", action="store_true",
                    help="continue the most recent session's conversation thread")
    pr.add_argument("--resume", default=None, metavar="ID",
                    help="resume a specific session by id (see the id printed after each run)")
    pr.set_defaults(fn=cmd_run)

    # prefix: measure the real cached-prefix cost on a provider (honest counterpart to est ~len/4)
    pp = sub.add_parser("prefix", help="measure the real prefix token cost on a provider "
                                       "(two-request usage differential; --measure implied)")
    pp.add_argument("--measure", action="store_true", help="(default action; accepted for clarity)")
    pp.add_argument("--provider", default=None, help="mock|anthropic|deepseek|... (env COLLIE_PROVIDER)")
    pp.add_argument("--model", default=None)
    pp.add_argument("--cwd", default=None); pp.add_argument("--project", default="demo")
    pp.set_defaults(fn=cmd_prefix)

    # pack: best-of-N with execution-based selection (run N isolated attempts, pick what passes)
    pk = sub.add_parser("pack", help="best-of-N: run the task N times in isolation, keep what passes")
    pk.add_argument("task")
    pk.add_argument("-n", type=int, default=3, help="number of attempts (1-8, default 3)")
    pk.add_argument("--check", default=None,
                    help="shell command run in each attempt's copy; exit 0 = pass (selection gate)")
    pk.add_argument("--apply", action="store_true",
                    help="copy the winning attempt's files back over the working dir")
    pk.add_argument("--provider", default=None); pk.add_argument("--model", default=None)
    pk.add_argument("--quality", choices=["quick", "balanced", "thorough"], default=None,
                    help="candidate run depth (default: Auto)")
    pk.add_argument("--verification", choices=["auto", "required"], default=None,
                    help="candidate harness verification contract")
    pk.add_argument("--effort", choices=["auto", "low", "medium", "high", "xhigh", "max"],
                    default=None, help="model reasoning effort (default: Auto by task)")
    pk.add_argument("--speed", choices=["standard", "fast"], default=None,
                    help="same-model provider speed tier; may consume more credits")
    pk.add_argument("--roster", default=None,
                    help="comma-separated backends to spread the attempts over, e.g. "
                         "'anthropic-oauth,codex-oauth,deepseek:deepseek-reasoner'. Assigned "
                         "round-robin; n rises to cover every backend named. Selection is still "
                         "by what PASSES, so a weak member can only cost tokens.")
    pk.add_argument("--parallel", type=int, default=1,
                    help="max attempts in flight (default 1). Worth raising for a roster spread "
                         "over different accounts; several at once on ONE plan invites rate limits.")
    pk.add_argument("--cwd", default=None)
    pk.add_argument("--json", action="store_true", help="print a JSON result")
    pk.set_defaults(fn=cmd_pack)

    # repl: lightweight interactive chat that keeps the full thread (and persists it as a session)
    prp = sub.add_parser("repl", help="interactive REPL that keeps the conversation thread")
    prp.add_argument("--provider", default=None); prp.add_argument("--model", default=None)
    prp.add_argument("--cwd", default=None); prp.add_argument("--project", default="demo")
    prp.add_argument("--goal", default=None)
    prp.add_argument("--continue", dest="cont", action="store_true", help="continue the latest session")
    prp.add_argument("--resume", default=None, metavar="ID", help="resume session by id")
    prp.set_defaults(fn=cmd_repl)

    # tui: rich full-experience terminal chat (live gate/diff/receipt timeline)
    pt = sub.add_parser("tui", help="rich terminal chat with a live tool/gate/diff timeline")
    pt.add_argument("--provider", default=None); pt.add_argument("--model", default=None)
    pt.add_argument("--cwd", default=None); pt.add_argument("--project", default="demo")
    pt.add_argument("--goal", default=None)
    pt.add_argument("--continue", dest="cont", action="store_true", help="continue the latest session")
    pt.add_argument("--resume", default=None, metavar="ID", help="resume session by id")
    pt.set_defaults(fn=cmd_tui)

    # web: local browser GUI, streams the run over SSE
    pw = sub.add_parser("web", help="serve the local web GUI (streams the verification gate live)")
    pw.add_argument("--port", type=int, default=8787)
    pw.add_argument("--no-open", dest="open", action="store_false", help="don't auto-open a browser")
    pw.add_argument("--remote", action="store_true",
                    help="also dial the public relay so a phone can drive this desktop from anywhere "
                         "(relay via $COLLIE_RELAY, default wss://collie.run)")
    pw.add_argument("--lan", action="store_true",
                    help="also listen on this machine's network address, so a phone on the same Wi-Fi "
                         "can reach it directly, no relay (CollieIOS); pairing is still required")
    pw.add_argument("--qr", action="store_true",
                    help="with --lan, also print a QR fallback of the one-shot pairing secret "
                         "(for when a camera can't read the ring code)")
    pw.add_argument("--name", default="",
                    help="which dog this server speaks for (CollieIOS shows it, and its face); "
                         "defaults to the kennel's dog when there is exactly one")
    pw.set_defaults(open=True, fn=cmd_web)

    # wallpaper: collie owns its own live desktop window (no third-party wallpaper engine)
    pwp = sub.add_parser("wallpaper", help="live desktop behind your icons (Windows engine); "
                                           "--install autostarts it at logon")
    pwp.add_argument("--port", type=int, default=8787, help="preferred port (a free one is picked if busy)")
    pwp.add_argument("--kiosk", action="store_true", help="non-Windows: immersive full-screen window")
    pwp.add_argument("--front", action="store_true",
                     help="macOS: an ordinary interactive window instead of the behind-the-icons desktop")
    pwp.add_argument("--install", action="store_true", help="autostart the wallpaper at every logon")
    pwp.add_argument("--uninstall", action="store_true", help="remove the logon autostart")
    pwp.add_argument("--stop", action="store_true", help="cleanly stop the running wallpaper engine")
    pwp.add_argument("--boot", action="store_true", help=argparse.SUPPRESS)  # internal autostart entry
    pwp.set_defaults(fn=cmd_wallpaper)

    # browser-bridge: LLM-driven real browser via a Chrome extension (authenticated / full-page)
    pb = sub.add_parser("browser-bridge", help="run the bridge the browser extension polls (browser_* tools)")
    pb.add_argument("--port", type=int, default=0)
    pb.add_argument("--browser", action="store_true",
                    help="also auto-launch a managed Chromium with the extension (no manual install)")
    pb.add_argument("--headed", action="store_true",
                    help="show that browser's window — the only way to sign in to sites, since its "
                         "profile is persistent and the logins are kept")
    pb.add_argument("--install", action="store_true",
                    help="start the bridge hidden at every logon (keeps real-browser powers)")
    pb.add_argument("--uninstall", action="store_true", help="remove the logon autostart")
    pb.set_defaults(fn=cmd_browser_bridge)

    # slack: @ a named collie in a channel and it queues the ask and works on it
    psl = sub.add_parser("slack", help="answer @mentions in Slack (Socket Mode; `setup` adds a dog)")
    psl.add_argument("slack_action", nargs="?", default="run", choices=["run", "setup"],
                     help="`setup` gives one more dog its own Slack app; run it again for the next")
    psl.add_argument("--config-token", dest="config_token", default="",
                     help="setup: app-configuration token (xoxe.xoxp-…) from api.slack.com/apps")
    psl.add_argument("--bot-token", dest="bot_token", default="", help="setup: xoxb-… if you have it")
    psl.add_argument("--app-token", dest="app_token", default="", help="setup: xapp-… if you have it")
    psl.add_argument("--list", dest="list_dogs", action="store_true", help="setup: show the pack")
    psl.add_argument("--name", default="", help="the name this collie answers to (kept across restarts)")
    psl.add_argument("--autonomy", default="", choices=["propose", "branch", "main"],
                     help="what it may do unattended; announced in the channel")
    psl.add_argument("--cwd", default="", help="repository it works in (default: here)")
    psl.add_argument("--provider", default="")
    psl.add_argument("--announce", default="", help="channel id to report in to on start")
    # These existed only on slackbot's own parser, so `collie slack --channels …` — the form setup
    # prints, and the form the logon launcher generates — died with "invalid choice: C0BM…" before
    # anything connected. At logon that failure is invisible: no window, and the bot simply is not
    # there, which is indistinguishable from a bot with nothing to say.
    psl.add_argument("--channels", default="",
                     help="comma-separated channel ids it will work in (default: only --announce)")
    psl.add_argument("--allow", default="",
                     help="comma-separated slack user ids that may task it (default: anyone there)")
    psl.add_argument("--install-autostart", dest="install_autostart", action="store_true",
                     help="bring this dog back after a restart (opt-in)")
    psl.add_argument("--uninstall-autostart", dest="uninstall_autostart", action="store_true",
                     help="stop it coming back")
    psl.set_defaults(fn=cmd_slack)

    # mail: a dog's own address, so a verification link is not a handover to a human
    pml = sub.add_parser("mail", help="a dog's own address (claim | verify | add | list | wait)")
    pml.add_argument("mail_action", choices=["claim", "verify", "add", "list", "wait"])
    pml.add_argument("name", nargs="?", default="", help="handle / code / dog name, per action")
    pml.add_argument("value", nargs="?", default="", help="for `claim`: your real email address")
    pml.add_argument("--subject", default="", help="for `wait`: match the subject")
    pml.add_argument("--sender", default="", help="for `wait`: match the sender")
    pml.add_argument("--timeout", type=int, default=180, help="for `wait`: seconds (default 180)")
    pml.set_defaults(fn=cmd_mail)

    # record: Loom/Reframe-style screen capture with a circular webcam bubble + mic, via ffmpeg
    prc = sub.add_parser("record", help="screen recording with a circular webcam bubble + mic "
                                        "(start / stop / status / devices)")
    prc.add_argument("record_action", nargs="?", default="start",
                     choices=["start", "stop", "status", "devices", "windows", "list"],
                     help="start (default), stop, status, devices, windows, or list recordings")
    prc.add_argument("--window", default=None,
                     help="record just this window (by title; see `record windows`) — small + smooth 30fps")
    prc.add_argument("--webcam", default=None,
                     help="camera device name (default: first found; see `record devices`)")
    prc.add_argument("--mic", default=None, help="microphone device name (default: first found)")
    prc.add_argument("--no-cam", dest="no_cam", action="store_true", help="screen only, no webcam bubble")
    prc.add_argument("--no-mic", dest="no_mic", action="store_true", help="no microphone audio")
    prc.add_argument("--sys-audio", dest="sys_audio", default=None,
                     help="also record system audio from this loopback device, mixed with the mic "
                          "(see `record devices`; needs Stereo Mix or a virtual audio cable)")
    prc.add_argument("--monitor", type=int, default=None,
                     help="record only display N (1-based, left-to-right; see `record devices`)")
    prc.add_argument("--region", default=None, help="record only a region, 'X,Y,W,H'")
    prc.add_argument("--position", default="bl", choices=["bl", "br", "tl", "tr"],
                     help="webcam bubble corner: bl/br/tl/tr (default bl)")
    prc.add_argument("--no-mirror", dest="no_mirror", action="store_true",
                     help="don't mirror the webcam (default: mirrored, like a selfie)")
    prc.add_argument("--countdown", type=int, default=0, help="3-2-1 countdown seconds before start")
    prc.add_argument("--fps", type=int, default=30, help="frame rate (default 30)")
    prc.add_argument("--cam-size", dest="cam_size", type=int, default=240,
                     help="webcam bubble diameter in px (default 240)")
    prc.add_argument("--margin", type=int, default=40, help="bubble margin from the corner in px (default 40)")
    prc.add_argument("--out", default=None, help="output file (default: the Collie folder under your videos dir)")
    prc.set_defaults(fn=cmd_record)

    # loop: autonomous goal-directed iteration — run the agent repeatedly toward a goal, stopping
    # when an EXECUTED check passes (on brand: the loop ends on real green, not the model's word).
    pl = sub.add_parser("loop", help="run the agent repeatedly toward a --goal until an executed "
                                     "--until check passes or --max iterations")
    pl.add_argument("task", nargs="?", default=None,
                    help="per-iteration instruction (default: 'make progress toward the goal')")
    pl.add_argument("--goal", default=None, help="the standing goal (pinned into CORE memory)")
    pl.add_argument("--until", default=None,
                    help="shell command; the loop stops the first iteration it exits 0 "
                         "(e.g. --until \"pytest -q\")")
    pl.add_argument("--max", type=int, default=5, help="max iterations (default 5)")
    pl.add_argument("--provider", default=None); pl.add_argument("--model", default=None)
    pl.add_argument("--cwd", default=None); pl.add_argument("--project", default="demo")
    pl.set_defaults(fn=cmd_loop)

    pa = sub.add_parser("acp", help="run as an ACP agent over stdio (Zed/JetBrains/neovim/"
                                    "VS Code plug in and drive collie's loop)")
    pa.set_defaults(fn=cmd_acp)

    pc = sub.add_parser("compare")
    pc.add_argument("--provider", default="mock"); pc.add_argument("--model", default=None)
    pc.add_argument("--vs", default="claude",
                    help="harness keys (claude,codex,gemini,cursor,opencode,aider) "
                         "or 'all' / 'discovered'")
    pc.add_argument("--real", action="store_true",
                    help="actually execute installed harness CLIs (spends quota)")
    pc.add_argument("--vs-model", default="")
    pc.add_argument("--judge", default="", help="provider for LLM quality judge (e.g. deepseek); '' = heuristic")
    pc.set_defaults(fn=cmd_compare)

    pib = sub.add_parser("inbox", help="what is waiting for you: approvals a run is "
                                       "suspended on, and delegated actions awaiting confirm")
    pib.add_argument("action", nargs="?", default="ls",
                     choices=["ls", "all", "allow", "always", "deny", "never"])
    pib.add_argument("id", nargs="?", default="")
    pib.add_argument("--limit", type=int, default=50)
    pib.set_defaults(fn=cmd_inbox)

    pau = sub.add_parser("audit", help="what the gate decided, and under which rule")
    pau.add_argument("--limit", type=int, default=40)
    pau.add_argument("--tool", default=None)
    pau.add_argument("--stage", default=None, choices=["asked", "approved", "denied", "auto"])
    pau.add_argument("--unexplained", action="store_true",
                     help="calls that ran without a prompt and cannot cite a rule "
                          "(should always be empty)")
    pau.set_defaults(fn=cmd_audit)

    pact = sub.add_parser(
        "activity", help="durable work and service health across interactive and unattended lanes")
    pact.add_argument("--state-dir", default=None)
    pact.add_argument("--limit", type=int, default=100)
    pact.add_argument("--health", action="store_true",
                      help="show supervisor/worker health plus work needing recovery")
    pact.add_argument("--no-probe", action="store_true",
                      help="skip live HTTP probes when using --health")
    pact.set_defaults(fn=cmd_activity)

    plib = sub.add_parser(
        "library", help="trusted extensions: validate, install, review, enable, rollback, remove")
    plib.add_argument("action", nargs="?", default="list",
                      choices=["list", "show", "scaffold", "validate", "plan", "install", "enable",
                               "disable", "rollback", "uninstall", "revoke", "connections",
                               "audit"])
    plib.add_argument("value", nargs="?", default="",
                      help="local package directory (validate/plan/install) or extension id")
    plib.add_argument("--version", default="")
    plib.add_argument("--id", dest="extension_id", default="",
                      help="stable reverse-domain id for scaffold")
    plib.add_argument("--name", default="", help="human-readable extension name for scaffold")
    plib.add_argument("--publisher", default="", help="publisher name for scaffold")
    plib.add_argument("--digest", default="",
                      help="expected SHA-256 provenance pin (install) or exact digest (revoke)")
    plib.add_argument("--reason", default="", help="security reason for revoke")
    plib.add_argument("--approve", action="store_true",
                      help="approve this exact digest and declared authority after review")
    plib.add_argument("--enable", action="store_true",
                      help="enable immediately after install (requires prior or --approve review)")
    plib.add_argument("--force", action="store_true",
                      help="allow uninstall of the active version; disable is safer")
    plib.add_argument("--yes", action="store_true",
                      help="confirm uninstall or digest revocation")
    plib.add_argument("--limit", type=int, default=100)
    plib.add_argument("--state-dir", default="",
                      help="state root for this command; set COLLIE_STATE_DIR for runtime use")
    plib.set_defaults(fn=cmd_library)

    prec = sub.add_parser(
        "recovery", help="inspect or explicitly reconcile crash-uncertain tool boundaries")
    prec.add_argument("action", nargs="?", default="ls",
                      choices=["ls", "show", "reconcile"])
    prec.add_argument("session", nargs="?", default="")
    prec.add_argument("--resolution", default="cancel",
                      choices=["completed", "not_fired", "cancel"],
                      help="what inspection proved happened at the uncertain boundary")
    prec.add_argument("--note", default="")
    prec.add_argument("--yes", action="store_true",
                      help="confirm an explicit reconcile after checking the outside system")
    prec.add_argument("--limit", type=int, default=100)
    prec.add_argument("--state-dir", default=None)
    prec.set_defaults(fn=cmd_recovery)

    phk = sub.add_parser(
        "hooks", help="validate and trust project hooks by their exact configuration hash")
    phk.add_argument("action", nargs="?", default="status",
                     choices=["status", "check", "trust", "untrust"])
    phk.add_argument("path", nargs="?", default="")
    phk.add_argument("--cwd", default="")
    phk.set_defaults(fn=cmd_hooks)

    psup = sub.add_parser(
        "supervisor", help="install and inspect Collie's per-user 24x7 worker supervisor")
    psup.add_argument("action", nargs="?", default="status",
                      choices=["install", "uninstall", "status", "run"])
    psup.add_argument("--state-dir", default="")
    psup.add_argument("--config", default="")
    psup.add_argument("--no-boot", action="store_true",
                      help="install only the logon trigger, without the optional boot trigger")
    psup.add_argument("--disable-worker", action="append", default=[],
                      choices=["web", "jobd", "automations", "bridge"])
    psup.set_defaults(fn=cmd_supervisor)

    paut = sub.add_parser(
        "automations", help="durable timer/file/page/webhook triggers and unattended execution")
    paut.add_argument("action", nargs="?", default="list",
                      choices=["daemon", "tick", "list", "status", "upsert"])
    paut.add_argument("value", nargs="?", default="",
                      help="automation id for status, or JSON file/- for upsert")
    paut.add_argument("--state-dir", default="")
    paut.add_argument("--db", default="")
    paut.add_argument("--ops-db", default="")
    paut.add_argument("--workspace-root", default="")
    paut.add_argument("--interval", type=float, default=5)
    paut.add_argument("--execute", action="store_true",
                      help="after polling triggers, execute one durable request")
    paut.set_defaults(fn=cmd_automations)

    ptr = sub.add_parser("trust", help="trust this directory's .collie/allow.toml "
                                       "(ls | revoke to undo)")
    ptr.add_argument("action", nargs="?", default="add", choices=["add", "ls", "revoke"])
    ptr.add_argument("path", nargs="?", default=None)
    ptr.set_defaults(fn=cmd_trust)

    prk = sub.add_parser("risk", help="what collie can reach, grouped by how far it reaches")
    prk.add_argument("--mode", default=None,
                     choices=["plan", "project", "interactive", "auto"])
    prk.add_argument("--set", default=None, dest="pattern", metavar="GLOB",
                     help="reclassify tools matching GLOB (e.g. 'mcp__fs__read_*')")
    prk.add_argument("--risk", default=None,
                     choices=["read", "write_local", "exec", "external"])
    prk.add_argument("--unset", action="store_true", help="remove the rule for --set's GLOB")
    prk.set_defaults(fn=lambda a: cmd_risk_set(a) if a.pattern else cmd_risk(a))
    ph = sub.add_parser("harnesses"); ph.set_defaults(fn=cmd_harnesses)

    sub.add_parser("dashboard").set_defaults(fn=cmd_dashboard)

    pm = sub.add_parser("mem")
    pm.add_argument("action", choices=[
        "search", "add", "reembed", "eval", "import", "purge-imported",
        "pending", "list", "approve", "attest", "reject", "invalidate"])
    pm.add_argument("text", nargs="?", default="")
    pm.add_argument("--project", default="",
                    help="project filter (search/add default to demo; review defaults to all)")
    pm.add_argument("--embed", default="auto")
    pm.add_argument("--status", default=None, choices=[
        "proposed", "active", "attested", "verified", "rejected", "invalidated"],
        help="filter `mem list` by lifecycle status")
    pm.add_argument("--note", default="",
                    help="review evidence recorded with approve/attest/reject/invalidate")
    # mem import: distill past Claude Code / Codex sessions into memory (see mem_import.py)
    pm.add_argument("--source", choices=["cc", "codex", "all"], default="all",
                    help="which local agent history to import")
    pm.add_argument("--limit", type=int, default=100,
                    help="max sessions/claims this run (newest first)")
    pm.add_argument("--dry-run", action="store_true", help="show extracted facts, store nothing")
    pm.add_argument("--no-llm", action="store_true", help="heuristic extraction only (no distiller calls)")
    pm.add_argument("--force", action="store_true", help="re-import sessions already in the state file")
    pm.add_argument("--provider", default=None, help="distiller provider override (default: Settings PROVIDER)")
    pm.add_argument("--model", default=None, help="distiller model override (default: sonnet on Claude providers)")
    pm.add_argument("--max-chunks", type=int, default=16,
                    help="rolling-distill call budget per session; giants get evenly sampled; 0 = no cap")
    pm.add_argument("--workers", type=int, default=1,
                    help="parallel distillation workers (db writes stay single-threaded)")
    pm.set_defaults(fn=cmd_mem)

    # jobs: the delegate surface — list jobs, confirm gated actions, read receipts.
    pj = sub.add_parser("jobs", help="delegated work: ls | inbox | run <cap> | confirm <nonce> | receipts")
    pj.add_argument("action",
                    choices=["ls", "inbox", "ask", "run", "confirm", "receipts",
                             "wake", "daemon", "web"])
    pj.add_argument("text", nargs="?", default="",
                    help="nonce (confirm/receipts) or capability name (run)")
    pj.add_argument("jargs", nargs="?", default="", help="JSON args for `run`")
    pj.add_argument("--goal", default="", help="job goal text (run)")
    pj.add_argument("--leash", default="", help="job leash as JSON (run)")
    pj.add_argument("--interval", default=60, type=float, help="daemon tick seconds")
    pj.add_argument("--port", default=0, type=int, help="dashboard port (web; default 8794)")
    pj.set_defaults(fn=cmd_jobs)

    pmis = sub.add_parser(
        "mission", help="durable campaigns: start/list/status/report/run/pause/resume/retry/cancel/reconcile")
    pmis.add_argument("action",
                      choices=["start", "ls", "status", "report", "run", "pause", "resume", "retry",
                               "cancel", "confirm", "continue", "accept", "check",
                               "reconcile"])
    pmis.add_argument("text", nargs="?", default="", help="goal (start) or mission id")
    pmis.add_argument("nonce", nargs="?", default="", help="confirmation nonce")
    autonomy = pmis.add_mutually_exclusive_group()
    autonomy.add_argument("--auto", action="store_true",
                          help="hands-off mode for this Mission (legacy explicit override)")
    autonomy.add_argument("--review", action="store_true",
                          help="confirm each irreversible external action for this Mission")
    pmis.add_argument("--domains", default="",
                      help="comma-separated browser domain allowlist (supports globs)")
    pmis.add_argument("--actions-per-hour", type=int, default=None,
                      help="durable rolling limit for irreversible actions")
    pmis.add_argument("--max-actions", type=int, default=None,
                      help="durable campaign total for irreversible actions")
    pmis.add_argument("--max-steps", type=int, default=None,
                      help="durable campaign model-decision ceiling")
    pmis.add_argument("--code", action="store_true",
                      help="enable durable code authority for this Mission")
    pmis.add_argument("--workspace", default="",
                      help="existing code workspace to bind (Collie never creates or deletes it)")
    pmis.add_argument("--overnight", action="store_true",
                      help="12-active-hour unattended subscription-only execution profile")
    pmis.add_argument(
        "--no-paid-overage", action="store_true",
        help="attest that paid usage credits/overage and auto-reload are disabled")
    pmis.add_argument(
        "--billing-evidence", default="",
        help="optional redacted account evidence for compatible non-native routes")
    pmis.add_argument("--verify-command", default="",
                      help="code completion check to run before reporting success")
    pmis.add_argument("--provider", dest="mission_provider", default="",
                      help="freeze this Mission to an explicit provider route")
    pmis.add_argument("--model", dest="mission_model", default="",
                      help="freeze this Mission to an explicit provider model/alias")
    pmis.add_argument("--run", action="store_true",
                      help="for start: run synchronously instead of leaving it queued")
    pmis.add_argument("--json", action="store_true")
    pmis.add_argument("--note", default="",
                      help="inspection note for recovery reconciliation or failed retry")
    pmis.add_argument(
        "--code-resolution", choices=("completed", "not_fired", "cancel"), default="",
        help="for reconcile: inspected outcome of an interrupted code-session tool")
    pmis.set_defaults(fn=cmd_mission)

    # init: front-load the lazy first-use costs (embedder download + code index) and optionally
    # have the model write AGENTS.md — the friendly "collie, meet my repo" moment.
    pi = sub.add_parser("init", help="project prep for this repo: warm the memory model + codemap; "
                                     "--rules writes AGENTS.md")
    pi.add_argument("--cwd", default=None)
    pi.add_argument("--no-config", action="store_true",
                    help="skip the provider/model prompt (CI / scripted runs)")
    pi.add_argument("--embed", default="auto", help="embedder (auto|granite|bge-m3|e5|bm25)")
    pi.add_argument("--rules", action="store_true",
                    help="also have the model explore the repo and write an AGENTS.md")
    pi.add_argument("--provider", default=None, help="provider for --rules (default: configured one)")
    pi.set_defaults(fn=cmd_init)

    # app: collie in a real desktop window (WebView2) — what the installer's shortcut launches
    pa = sub.add_parser("app", help="open collie in a native desktop window (not a browser tab)")
    pa.add_argument("--port", type=int, default=8787)
    pa.add_argument("--open", action="store_true", help=argparse.SUPPRESS)
    pa.set_defaults(fn=cmd_app)

    # setup: machine-level onboarding — deps + model + provider ("collie doctor" + one-click install)
    ps = sub.add_parser("setup", help="install deps, pick a provider, pre-download the model "
                                      "(--check = diagnose only)")
    ps.add_argument("--check", action="store_true", help="diagnose only; install nothing")
    ps.add_argument("--yes", action="store_true", help="install without prompting")
    ps.set_defaults(fn=cmd_setup)

    # config: scriptable settings.json access (the installer uses it to seed the UI language)
    pmb = sub.add_parser("menubar", help="collie in the menu bar — one click to ask, no Dock tile")
    pmb.add_argument("--port", type=int, default=8787)
    pmb.set_defaults(fn=cmd_menubar)

    pup = sub.add_parser("update", help="check for a newer collie and install it (--yes to install)")
    pup.add_argument("--yes", action="store_true", help="install it, not just report it")
    pup.set_defaults(fn=cmd_update)

    pu = sub.add_parser("uninstall", help="remove collie: the app bundle, ~/.collie, and the "
                                          "permissions macOS keeps after the app is gone")
    pu.add_argument("--yes", action="store_true", help="actually delete (without this it only lists)")
    pu.add_argument("--keep-config", action="store_true",
                    help="keep settings.json / mcp.json / remote.json — remove only caches and the app")
    pu.set_defaults(fn=cmd_uninstall)

    pc = sub.add_parser("config", help="read/write settings (config | config KEY | config KEY VALUE)")
    pc.add_argument("key", nargs="?", default="")
    pc.add_argument("value", nargs="?", default=None)
    pc.set_defaults(fn=cmd_config)

    # mcp: manage MCP servers. `connect <name>` is the one to reach for — for a service in the
    # catalog it fills in the address AND does the browser handshake, which is the whole setup.
    # The rest: list configured ones, OAuth-login to a remote, logout, or list tools.
    pmcp = sub.add_parser("mcp", help="manage MCP servers (connect | list | add | remove | enable | "
                                      "disable | login | logout | tools)")
    pmcp.add_argument("action", choices=["connect", "list", "add", "remove", "enable", "disable",
                                         "login", "logout", "tools"])
    pmcp.add_argument("name", nargs="?", default="")
    pmcp.add_argument("value", nargs="?", default="",
                      help="for `add`: an https:// URL (remote server) or a shell command (stdio). "
                           "Omit it for a service collie already knows: `collie mcp add slack`")
    pmcp.add_argument("--force", action="store_true", help="for `add`: overwrite an existing server")
    pmcp.set_defaults(fn=cmd_mcp)

    args = p.parse_args(argv)
    # any chat surface started interactively with nothing configured gets the one-time wizard —
    # without this, a fresh install's `collie web`/`collie tui`/`collie run` silently lands on the
    # mock provider and answers with canned "Based on the tool output" nonsense. Never prompts when:
    # the user already chose (--provider), the output is machine-read (--json/--stream-json may run
    # on an editor's pty — input() there would hang the protocol), or stdin/stdout isn't a tty.
    if (args.cmd in ("run", "repl", "tui", "web") and not getattr(args, "provider", None)
            and not (getattr(args, "json", False) or getattr(args, "stream_json", False))):
        _first_run_wizard()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
