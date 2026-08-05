"""The agentic loop — wires provider + tools + memory + context + recorder.

    while stop_reason == "tool_use":
        system, msgs, meta = composer.build(...)     # tiered prompt + auto-prefetch
        completion       = provider.complete(...)    # model turn
        run tools -> append tool_results
    consolidate(task, answer) -> memory.remember     # self-cleaning write path
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import time

from . import __version__
from . import redact as _redact
from . import settings as _settings
from .context import ContextComposer
from .providers import (ModelProvider, Usage, ToolCall, classify_error, is_overflow,
                        is_known_terminal, _error_completion)
from .recorder import Recorder, RunResult
from .tools import ToolRegistry, ToolCtx, repair_args
from .verifier import CodeReproVerifier, Mutation, Observation

# Output-truncation feedback (point 1): a tool call whose args were cut off at the output-token
# limit must NOT execute — its arguments may be silently incomplete. Tell the model to re-issue,
# and offer the split-into-smaller-edits escape so a hard cap isn't a dead end for weak models.
TRUNC_MSG = ("ERROR: not executed — the response hit the output-token limit, so these arguments "
             "may be truncated. Re-issue the call with complete arguments, or split a large edit "
             "into several smaller edit_file calls.")
TRUNC_CONTINUE = ("Your reply was cut off at the output-token limit — continue from where it "
                  "stopped, or use tool calls.")

VERIFY_NUDGE = ("Before finalizing: run the project's tests with `python -m pytest -q` "
                "(use the bash tool). If anything fails, read the error, fix it, and "
                "re-run. Only give your final answer once the relevant tests pass.")

# Evidence-gated verify (SWE): after an edit, don't accept "done" until a reproduction has
# actually been RUN on the fixed code and didn't error. This is the loop lever the audit +
# the Hermes diff (its verification_stop/verification_evidence modules) both point at — the
# one-shot advisory nudge let the model finish a wrong edit (right file, wrong change).
REPAIR_NUDGE = (
    "Your reproduction still fails or prints the wrong result AFTER your edit. Read the "
    "traceback/output above, FIX the code with edit_file, and RE-RUN the same reproduction. "
    "Do not finish until it prints the correct result.")


_REPRO_RE = re.compile(
    r'(^|[;&|]\s*)(python3?|py)\s+(-c\b|-u\b|-m\s+(?!pytest\b|pip\b|venv\b|tox\b|nox\b)\w|[\w./~-]*\.py\b)')
# Heredoc / stdin reproductions: `python <<'EOF' … EOF`, `python 2>&1 <<EOF`, `python - <<EOF`,
# `python3 -` (script on stdin). These are the most common way an agent runs a self-contained repro,
# and if the finish-gate doesn't recognize them a PASSING repro can't clear a stale failure flag —
# so the gate keeps nagging about a phantom failure it saw on an earlier command.
_REPRO_STDIN_RE = re.compile(r'(^|[;&|]\s*)(python3?|py)\b[^\n;|]*?(<<-?\s*[\'"]?\w|\s-\s*(<|$))')

# Non-Python evidence. Both regexes above only match `python`/`py`, so on a Go or JS repo the
# finish-gate saw NO evidence no matter what the agent ran: `go build ./...` was not a
# reproduction, the gate nagged for `verify_max` rounds with a Python instruction the agent could
# not satisfy, and then let it finish anyway. That is how a patch that does not even COMPILE got
# declared done on SWE-bench Pro's flipt instance. For compiled/typechecked languages the build
# itself is the most valuable evidence there is — cheap, unambiguous, and impossible to fake.
_REPRO_OTHER_RE = re.compile(
    r'(^|[;&|]\s*)('
    r'go\s+(build|vet|run)\b'
    r'|go\s+test\b[^\n;|]*\s-run\b'                 # targeted, not the suite
    r'|cargo\s+(check|clippy|build|run)\b'
    r'|cargo\s+test\b[^\n;|]*\S'                    # cargo test <name>
    r'|npx?\s+tsc\b|yarn\s+tsc\b|tsc\s+--noEmit\b'
    r'|node\s+(--check\b|[\w./~-]+\.(js|mjs|cjs)\b)'
    r'|npx\s+(jest|vitest|mocha|ava)\b[^\n;|]*\S'   # a named test file, not a bare suite run
    r'|(mvn|\./gradlew)\s+[^\n;|]*\b(compile|test-compile)\b'
    r')')

# Does the command actually CHECK a result, as opposed to merely proving the code builds?
# `\bassert\b` alone is a Python idiom; a Go agent asserts with t.Fatal/t.Error, a JS one with
# expect(). Running a TARGETED existing test counts too — it is an executable correctness check.
# `go build` deliberately does NOT count: compiling is necessary, never sufficient, and letting it
# satisfy require_assert would reopen the print-only hole in a new language.
_ASSERTED_RE = re.compile(
    r'\bassert\b|\bt\.(Fatal|Error)f?\b|\bexpect\(|\brequire\.\w|\bshould\b\.'
    r'|go\s+test\b[^\n;|]*\s-run\b|cargo\s+test\b[^\n;|]*\S'
    r'|npx\s+(jest|vitest|mocha|ava)\b[^\n;|]*\S')


def _budget_exceeded(model, total):
    """True once the run has spent past the configured $ or token ceiling (Settings panel /
    COLLIE_MAX_COST / COLLIE_MAX_TOTAL_TOKENS). 0/unset = no limit."""
    try:
        max_cost = float(os.environ.get("COLLIE_MAX_COST", "0") or 0)
    except ValueError:
        max_cost = 0.0
    try:
        max_tok = int(os.environ.get("COLLIE_MAX_TOTAL_TOKENS", "0") or 0)
    except ValueError:
        max_tok = 0
    if max_cost <= 0 and max_tok <= 0:
        return False
    tot = total.input_tokens + total.output_tokens + total.cache_read + total.cache_creation
    if max_tok > 0 and tot >= max_tok:
        return True
    if max_cost > 0:
        from .costs import cost_usd
        if cost_usd(model, total.input_tokens, total.output_tokens,
                    total.cache_read, total.cache_creation) >= max_cost:
            return True
    return False


def _is_repro_cmd(name, args):
    """A post-edit `python -c`/`python repro.py` we can gate finish on — never the suite, and
    NOT a command that merely MENTIONS python (e.g. `ln -sf "$(command -v python3)"` or
    `command -v python3` — those used to be mis-flagged as reproductions and fail the gate)."""
    if name != "bash":
        return False
    c = args.get("command") or ""
    # exclude test-suite / build runners too: a green `python -m unittest` / `python setup.py test`
    # is NOT the targeted reproduction the finish-gate is meant to key on (the whole point is a
    # focused repro of THIS bug, never "the suite passes").
    if any(b in c.lower() for b in ("pytest", "pip ", "pip3 ", "python -m venv", "tox", "nox",
                                    "unittest", "nose", "setup.py")):
        return False
    return (bool(_REPRO_RE.search(c)) or bool(_REPRO_STDIN_RE.search(c))
            or bool(_REPRO_OTHER_RE.search(c)))


def _repro_failed(output) -> bool:
    """Did a post-edit reproduction actually FAIL? Ground truth is the process exit code (the bash
    tool prefixes '[exit N]' for nonzero) or a tool-level ERROR — NOT a bare 'Traceback' substring.
    A passing repro can print 'Traceback' (testing error handling: a caught exception echoed via
    traceback.print_exc, or the word appearing in data) and still exit 0; reading that as failure
    made the finish-gate nag the model to 'fix' correct code it could never satisfy (the phantom
    failure that made a self-audit give up). Any real uncaught exception — including an
    AssertionError in assert-mode — exits nonzero, so the exit-code signal keeps assert-verify."""
    o = output if isinstance(output, str) else str(output)
    return o.startswith("ERROR") or o.startswith("[exit")

# When force_edit is on (a task we KNOW requires a code change, e.g. SWE fixing) and the
# agent burns turns exploring without ever editing, converge it. On SWE-bench, collie's
# empty patches came from spending all 25 turns on code_search/read/grep and never calling
# edit_file — a same-model competitor (Hermes) that committed to an edit resolved them.
EDIT_FORCE_NUDGE = (
    "You have used many turns exploring without making any edit. STOP searching and "
    "reading now. Based on what you have already found, use `edit_file` THIS turn to make "
    "the concrete fix. Producing no edit scores zero — a focused, imperfect edit is far "
    "better than none. If the fix requires changes in more than one file, edit EACH file.")

# Multi-file coverage: collie under-covered pylint-4551 (edited 2 of the 4 files the gold
# fix touches). After it edits and tries to finish, give it one chance to find sibling
# files that need the same change — the fix often spans the class's callers/writers.
COVERAGE_NUDGE = (
    "Before you finish: does this fix belong in OTHER files too? Many issues need the "
    "same change across related modules — the code that CALLS what you changed, the "
    "writer/serializer that consumes it, or sibling files in the same package. Use "
    "`code_search` or `grep` to check for other spots, and `edit_file` them. If you have "
    "genuinely covered every file, briefly say so and finish.")

# White-flag guard: sphinx-10435 made a fix, got gate-bounced (correctly), REVERTED it, then
# thrashed in analysis until the spin-break closed the run — net diff zero, 322K tokens for an
# empty patch. The model knows WHY it reverted (it was one turn from the right synthesis), so
# rescue turn(s) beat a blind mechanical restore; the restore is the belt when rescue fails too.
ROLLBACK_NUDGE = (
    "STOP — you are about to finish with ZERO net changes: every edit you made was reverted. "
    "An empty patch always scores zero; a focused partial fix can score. Within the next few "
    "turns, either (a) re-apply your earlier fix, corrected for whatever made you revert it, or "
    "(b) make the single smallest edit you are most confident addresses the issue. Then finish.")

_JUNK_UNTRACKED = ("__pycache__", ".pyc", "venv/", ".venv/", "node_modules/",
                   ".egg-info", ".dist-info", ".pytest_cache")


def _tree_diff(cwd):
    """Net worktree diff vs HEAD (tracked files — the shape of a code fix). '' on non-git/error,
    which also disarms the whole guard: no snapshot -> no nudge -> no restore."""
    try:
        r = subprocess.run(["git", "diff", "HEAD"], cwd=cwd, capture_output=True,
                           text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _tree_empty(cwd):
    """True when the worktree holds NO net change: no tracked diff and no non-junk untracked
    file (a new-file fix is a real change — never nudge/restore over one)."""
    if _tree_diff(cwd).strip():
        return False
    try:
        r = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd,
                           capture_output=True, text=True, timeout=30)
        return not [p for p in r.stdout.splitlines()
                    if p.strip() and not any(j in p for j in _JUNK_UNTRACKED)]
    except Exception:
        return True


def _apply_diff(cwd, diff):
    """Re-apply a captured diff; --3way fallback for drifted context. True on success."""
    for extra in ([], ["--3way"]):
        try:
            r = subprocess.run(["git", "apply", "--whitespace=nowarn"] + extra, cwd=cwd,
                               input=diff, capture_output=True, text=True, timeout=60)
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


class Harness:
    def __init__(self, provider: ModelProvider, memory, registry: ToolRegistry,
                 composer: ContextComposer, recorder: Recorder,
                 cwd: str, project: str = "global", mode: str = "act",
                 max_turns: int = 50, self_verify: bool = True,
                 force_edit: bool = False):
        self.provider = provider
        self.memory = memory
        self.registry = registry
        self.composer = composer
        self.recorder = recorder
        self.cwd = cwd
        self.project = project
        self.mode = mode
        self.max_turns = max_turns
        self.stream_cb = None            # set by interactive surfaces -> real token streaming
        self.self_verify = self_verify   # after an edit, nudge once to run tests
        self.verify_nudge = None         # override VERIFY_NUDGE (e.g. SWE: quick python -c, not pytest)
        self.force_edit = force_edit     # converge to an edit if exploring too long
        self.verify_gate = False   # gate finish on an actually-run post-edit reproduction (SWE)
        self.verify_max = 2        # bounded reproduce->repair rounds (no spinning)
        self.repair_nudge = None   # override REPAIR_NUDGE
        # ASSERT-mode: a post-edit repro only counts as verification if it EXECUTES an
        # `assert expected==actual`. Closes the hole that sank the old no-traceback gate —
        # collie's wrong edits don't raise, they print WRONG output, so "ran without error"
        # passed them. Requiring an assert turns the model's own correctness judgment into a
        # gate-checkable signal (a wrong fix -> AssertionError -> Traceback -> repair round).
        self.require_assert = False
        self.coverage_gate = False # SWE multi-file: re-surface uncovered siblings at finish
        self.coverage_max = 2      # bounded coverage rounds (ADVISORY, not hard-forced)
        self.cov_thresh = 1.9      # min related_scored to re-surface (same-package strong match)
        # adversarial critic gate: an INDEPENDENT fresh-context review of the diff before finish.
        # Self-attack fails (shares the model's misread); a fresh read that sees ONLY issue+diff does not.
        self.critic = False        # SWE: attack the fix with a second, independent read
        self.critic_issue = ""     # the issue text handed to the critic
        self.critic_max = 2        # bounded critic->repair rounds
        self.critic_fn = None      # optional INVESTIGATIVE critic (fn(issue,diff,cwd)->(ok,objection))
        self.critic_provider = None  # optional SECOND MODEL for the critic; None -> self.provider.
                                   # The critic's whole claim is that a separate read does not share
                                   # the author's blind spot — which only fully holds once the reader
                                   # is a different model. See swe._critic_provider.
                                   # — a fresh agent WITH read-only tools that inspects the codebase
                                   # itself (catches under-coverage a diff-only review can't). Falls
                                   # back to the one-shot _run_critic when None.
        # host-owned retry policy (point 5): classify a transport error and back off in ONE place,
        # instead of a provider-internal 3× loop multiplying with nothing. Settings-panel knobs.
        from . import settings as _settings
        try:
            self.max_retries = max(0, int(_settings.get("RETRIES", "3")))
        except (TypeError, ValueError):
            self.max_retries = 3
        try:
            self.retry_base = max(0.0, float(_settings.get("RETRY_BASE", "2")))
        except (TypeError, ValueError):
            self.retry_base = 2.0
        # context-overflow recovery (point 9): on an input-too-long error, shrink the history once
        # and retry the turn. COLLIE_OVERFLOW_RECOVERY=0 restores the old die-on-overflow behavior.
        self.overflow_recovery = _settings.get("OVERFLOW_RECOVERY", "1") not in ("0", "false", "off")
        # optional NDJSON event sink for streaming UX (CLI --stream-json, an editor extension,
        # or the ACP adapter). Default None = zero cost, no behavior change. Set h.emit = fn.
        self.emit = None
        # optional mid-run steering: a callable -> list[str] of user messages typed while the run is
        # in flight (point 13). Interactive surfaces (TUI) set it; None = zero cost, benchmark path
        # byte-identical. Drained only at safe points (turn start / voluntary finish).
        self.steering = None
        # The authority half. `gate` decides allow/deny/ask for each proposed call; `approve` is
        # the surface that answers an "ask" — a TUI prompt, a web card, ACP's native permission
        # request, a phone. They are separate on purpose: the loop must not know which surface it
        # is talking to, which is what lets an attended run and an unattended one share this path.
        #
        # gate=None means UNGATED, and is the pre-existing behaviour for callers that have not been
        # taught about the gate yet (benchmarks, pack, embedded uses). New surfaces set it.
        # approve=None with a gate set is the honest headless case: nothing off-machine may run,
        # because there is nobody to ask — see _authorize.
        self.gate = None
        self.approve = None
        # Durable record of what the gate decided. None = not recording (benchmarks, tests,
        # embedded uses); a user-facing surface attaches an AuditLog.
        self.audit = None

    def _emit(self, kind, **data):
        if self.emit:
            try:
                self.emit(kind, data)
            except Exception:
                pass

    def _authorize(self, tc, tool):
        """Decide whether this call may run. Returns None to allow, or the reason it was
        refused (which becomes the call's result, so the model can route around it).

        Called with the REPAIRED but NOT secret-restored args, and that ordering is
        load-bearing. `_redact.restore` swaps `{{SECRET:…}}` back to real credentials one
        line before `tool.run`; anything the approval path touches — the prompt on screen,
        an audit row, a notification pushed to a phone — must see the placeholder version.
        Authorizing after the restore would leak the very secrets the redaction exists to
        keep out of sight.
        """
        if self.gate is None:
            return None                       # ungated caller (benchmarks, embedded uses)
        try:
            d = self.gate.evaluate(tc.name, tc.args, tool)
        except Exception as e:
            # A broken gate must not become an open gate.
            return "the permission gate failed (%s: %s)" % (type(e).__name__, e)

        if d.allowed:
            if d.rule:
                self._emit("gate", name=tc.name, decision="allowed", rule=d.rule,
                           risk=d.risk)
            # Consequential AND unprompted is the case the audit exists for: the row has to
            # be able to answer "why was I not asked about that?". Reads are not recorded —
            # they have no side effect to account for, and drowning the log in them is how
            # an audit trail stops being read.
            if d.risk != "read":
                self._audit(tc, d, stage="auto", outcome="allowed")
            return None

        if not d.needs_user:
            self._emit("gate", name=tc.name, decision="denied", reason=d.reason, risk=d.risk)
            self._audit(tc, d, stage="denied", outcome="refused")
            return d.reason

        if self.approve is None:
            # Nobody to ask. This is the honest headless answer: refuse, and say why, so
            # the model can finish the parts that need no permission and report the rest.
            # Treating "unattended" as "allowed" would make the gate decorative exactly
            # when it matters most — when no one is watching.
            self._emit("gate", name=tc.name, decision="denied", risk=d.risk,
                       reason="no approver attached")
            self._audit(tc, d, stage="denied", outcome="refused",
                        reason="nobody was available to approve it")
            return ("%s, and there is nobody to approve it in this run. Do the parts that "
                    "need no approval and describe this step instead of doing it." % d.reason)

        d.call_id = tc.id           # the idempotency key a parked approval is filed under
        self._emit("gate", name=tc.name, decision="asking", risk=d.risk,
                   target=d.target, reason=d.reason, rule_offer=d.rule_offer)
        try:
            outcome = self.approve(tc.name, tc.args, d)
        except Exception as e:
            return "could not ask for approval (%s: %s)" % (type(e).__name__, e)

        from .gate import ALLOWING, Outcome
        try:
            outcome = Outcome(str(outcome))
        except ValueError:
            outcome = Outcome.REJECT_ONCE     # an unparseable answer is not consent
        self.gate.apply_outcome(outcome, tc.name, d.target)
        allowed = outcome in ALLOWING
        self._emit("gate", name=tc.name, decision="approved" if allowed else "denied",
                   outcome=outcome.value, risk=d.risk, target=d.target)
        self._audit(tc, d, stage="approved" if allowed else "denied",
                    outcome=outcome.value, reason="answered by the user")
        return None if allowed else "the user declined this action"

    def _audit(self, tc, decision, *, stage, outcome, reason=None):
        """Record one gate decision. Best-effort and lazily opened, so a run with no audit
        db (a test, a read-only home) is unaffected — and note the args passed are
        `tc.args`, the pre-restore ones, for the same reason the approval prompt gets them.
        """
        if self.audit is None:
            return
        try:
            self.audit.record(
                session=getattr(self, "_audit_session", "") or self.project,
                cwd=self.cwd, tool=tc.name, risk=decision.risk,
                target=decision.target or "", stage=stage, outcome=outcome,
                reason=reason if reason is not None else decision.reason,
                rule=decision.rule, args=tc.args)
        except Exception:
            pass                       # never fail a run over its own bookkeeping

    def _drain_steering(self):
        """Pull any queued mid-run user messages (point 13). Same exception discipline as _emit —
        a broken callback must never crash the run."""
        if not self.steering:
            return []
        try:
            return [s.strip() for s in (self.steering() or []) if isinstance(s, str) and s.strip()]
        except Exception:
            return []

    def _run_critic(self, issue, diff):
        """Independent adversarial review — a FRESH provider call seeing ONLY the issue + the diff
        (not the main model's reasoning or its self-written test), so it does not inherit the main
        model's blind spot. Self-attack shares the misread; a fresh read does not. Returns
        (ok, objection): ok=True means finish is allowed; otherwise `objection` is fed back."""
        sysp = ("You are an adversarial code reviewer. Given a GitHub ISSUE and a candidate DIFF, find "
                "ONE concrete way the diff FAILS to do what the issue requires: a specific input/case it "
                "gets wrong, a required behavior or default value it misses, a wrong name/signature, or a "
                "sibling/call-site it should have changed but did not. Judge ONLY against the issue's "
                "actual requirement, not style. If the diff genuinely and COMPLETELY satisfies the issue, "
                "reply with exactly CORRECT. Otherwise reply with the single most important concrete "
                "concern in 1-2 sentences, naming the exact case or behavior.")
        msg = "ISSUE:\n%s\n\nCANDIDATE DIFF:\n%s" % (str(issue)[:6000], str(diff)[:9000])
        self._critic_usage = None
        try:
            reviewer = self.critic_provider or self.provider
            comp = reviewer.complete(sysp, [{"role": "user", "content": msg}], [])
            self._critic_usage = comp.usage   # the caller folds this into the run's token/$ total —
            text = (comp.text or "").strip()   # a critic call spends real tokens; the receipt must show them
        except Exception:
            return True, ""            # a critic failure must never block a finish
        if not text or text.upper().lstrip("*# `").startswith("CORRECT"):
            return True, ""
        return False, text

    def _repro_verified(self, did_edit, last_edit_turn, last_repro_turn,
                        last_repro_failed, last_repro_asserted) -> bool:
        """Single source of truth for the assert-verify gate: delegated to
        harness.verifier.CodeReproVerifier so the code gate here and the world
        done-checks (ListingVerifier, …) share ONE decision implementation.
        Returns True iff finishing as verified is allowed. The three former inline
        copies (spin-break guard, finish gate, final receipt verdict) now all call
        this; equivalence with the historical logic is pinned by
        tests/test_verifier.py::test_matches_loop_gate."""
        if not did_edit:
            return False
        return CodeReproVerifier(require_assert=self.require_assert).verdict(
            [Mutation(at=last_edit_turn)],
            [Observation(channel="exit-code", at=last_repro_turn,
                         ok=not last_repro_failed, asserted=last_repro_asserted)],
        ).verified

    def run(self, task_id: str, user_msg: str, consolidate: bool = True,
            history: list = None) -> RunResult:
        t0 = time.time()
        rid = self.recorder.start_run(task_id, "collie", self.provider.model,
                                      self.provider.name, note="v" + __version__)
        res = RunResult(run_id=rid, task_id=task_id, harness="collie",
                        model=self.provider.model, provider=self.provider.name)
        ctx = ToolCtx(cwd=self.cwd, project=self.project, memory=self.memory,
                      recorder=self.recorder, registry=self.registry)
        # Snapshot the tree BEFORE anything is edited, so a run can be undone wholesale. Taken
        # here rather than at the first edit: by the time an edit lands a command may already have
        # written files, and the point the user wants back is "before I asked for this".
        #
        # A failure to snapshot must not stop the task — but it must not be silent either, since
        # the user's willingness to let an agent loose depends on believing the undo exists. So
        # the reason travels to the UI in the same event that would have carried the checkpoint.
        res.checkpoint_ref = ""
        try:
            from . import checkpoints as _ckpt
            _ok, _why = _ckpt.available(self.cwd)
            if _ok:
                _cp = _ckpt.capture(self.cwd, str(task_id), rid, user_msg[:60])
                res.checkpoint_ref = _cp.ref
                self._emit("checkpoint", ok=True, ref=_cp.ref[:12], kind=_cp.kind)
            else:
                self._emit("checkpoint", ok=False, reason=_why)
        except Exception as _ce:                 # never block the run on bookkeeping
            self._emit("checkpoint", ok=False, reason="%s: %s" % (type(_ce).__name__, _ce))
        # history (prior thread) lets a session CONTINUE across CLI calls / repl turns; the
        # composer's own elision keeps a long continued thread from bloating the prefix.
        msgs0 = list(history) if history else []
        msgs0.append({"role": "user", "content": user_msg})
        session = {"messages": msgs0}
        # privacy: secrets found in tool output are swapped for {{SECRET:…}} placeholders before
        # they can reach ANY cloud provider; the vault (in-memory only, never persisted) lets the
        # execution boundary substitute real values back. Off only if the user disables the knob.
        _redact_on = (_settings.get("REDACT_SECRETS", "on") or "on") not in ("off", "0", "false")
        self._secret_vault = getattr(self, "_secret_vault", {})
        total = Usage()
        # --- cache-waste ledger (point #3): the prefix SHOULD cache turn-to-turn; when it doesn't,
        # attribute the re-billed tokens to a cause (schema change / history elision / TTL) and price
        # the waste. Seed reported_cache from the provider so a 100%-from-turn-0 bust still counts
        # (a bust reports zero cache fields, so the sticky flag would otherwise never arm).
        from .costs import cache_miss as _cache_miss, CACHE_TTL_S as _CACHE_TTL
        reported_cache = getattr(self.provider, "reports_cache", False)
        prev_prompt = 0
        prev_skey = None
        prev_elide_from = 0
        prev_t = None
        waste_tok = waste_usd = 0
        miss_n = 0
        trunc_rounds = 0            # output-truncation rounds (point 1), bounded like verify_max
        overflow_tried = False      # context-overflow recovery is once-per-run (point 9)
        last_stop = ""              # stop_reason of the last completion (for the memory-consolidation gate)
        answer = ""
        did_edit = verified = covered = multifile_hinted = edit_forced = False
        edited_files, last_edit_text, last_edit_path = set(), "", ""
        last_edit_turn = -100
        last_repro_turn, last_repro_failed, verify_rounds = -100, False, 0
        last_repro_asserted = False   # did the last post-edit repro actually run an `assert`?
        coverage_rounds = 0
        critic_rounds = 0
        best_diff, rollback_rounds = "", 0   # white-flag guard (see ROLLBACK_NUDGE)
        # Convergence thresholds scale WITH max_turns, so they must stay above the solve-turn
        # distribution (rebench: resolved median 23, so a 0.55 ratio -> force_at 27 sits just above
        # it). Env-tunable for the force_at-ratio study (COLLIE_FORCE_RATIO / COLLIE_HARD_RATIO).
        _fr = float(os.environ.get("COLLIE_FORCE_RATIO", "0.55"))
        _hr = float(os.environ.get("COLLIE_HARD_RATIO", "0.76"))
        force_at = max(3, int(self.max_turns * _fr))    # soft nudge to converge
        hard_at = max(force_at + 2, int(self.max_turns * _hr))  # then remove explore tools
        budget_hit = False
        # Ran out of turns, as opposed to deciding it was finished. Every voluntary ending leaves the
        # loop through a `break`, so `for … else` marks exactly the case where the range simply ran
        # out — mid-task, by definition. Without this the two endings were indistinguishable
        # afterwards and both reported the same word: "done".
        turns_exhausted = False
        try:
            for turn in range(self.max_turns):
                if turn > 0 and _budget_exceeded(self.provider.model, total):
                    budget_hit = True         # spent past the $/token ceiling — stop before another turn
                    res.turns = turn
                    break
                # mid-run steering (point 13): inject any user text typed while the run is in flight,
                # as a user message BEFORE this turn's build. Every mid-run `continue` funnels back
                # here, so this single site covers pi's loop-start AND after-tool-results polls.
                steers = self._drain_steering()
                if steers:
                    txt = "\n".join(steers)
                    session["messages"].append({"role": "user", "content": txt})
                    res.steer_count += 1
                    self._emit("steer", text=txt[:200])
                    self.recorder.log_turn(rid, turn, "steer", txt[:500], 0, 0, 0, 0)
                system, msgs, meta = self.composer.build(
                    session, user_msg, self.cwd, self.project, self.mode)
                if turn == 0:
                    res.prefix_tokens = meta.prefix_tokens
                    ceiling = getattr(self.composer.budgeter, "prefix_ceiling", 0)
                    if ceiling and meta.prefix_tokens > ceiling:
                        # #14: the ceiling was never enforced — WARN (don't hard-truncate; that
                        # would drop context mid-run). Emitted for surfaces + recorded so the
                        # benchmark/run paths (where emit is a no-op) still leave a trace.
                        self._emit("prefix_ceiling", est=meta.prefix_tokens, ceiling=ceiling)
                        if os.environ.get("COLLIE_DEBUG"):
                            print("WARN(prefix): est %d > ceiling %d" % (meta.prefix_tokens, ceiling))
                res.mem_recalls += meta.prefetched

                # Tell the provider where the byte-stable elided prefix ends, so it can put a
                # cache_control breakpoint there (Anthropic caches history turn-to-turn -> the big
                # win on long runs). Providers that don't cache ignore this attribute.
                self.provider.cache_stable_upto = meta.elide_from

                tt = time.time()
                schemas = self.registry.active_schemas()
                # structural convergence: text nudges don't stop DeepSeek exploring, so
                # past the hard deadline with no edit yet, hand it ONLY read/edit/write —
                # it can no longer search/grep/bash, so it must commit to a change.
                if self.force_edit and not did_edit and turn >= hard_at:
                    only = [s for s in schemas
                            if s["name"] in ("read_file", "edit_file", "write_file")]
                    if only:
                        schemas = only
                # --- provider call with host-owned bounded retry (point 5) + one-shot context-
                # overflow recovery (point 9). errors-as-data means complete() returns rather than
                # raising; the try is a belt for any provider not yet on that contract.
                attempts = 0
                overflow_now = False
                while True:
                    try:
                        comp = self.provider.complete(system, msgs, schemas, on_text=self.stream_cb)
                    except Exception as e:
                        comp = _error_completion(getattr(self.provider, "name", "?"), e)
                    total.add(comp.usage)   # a failed streaming attempt burned real tokens — count them
                    if comp.stop_reason != "error":
                        break
                    cls = classify_error(comp.error_detail or comp.text or "", comp.error_status)
                    if (cls == "overflow" and not overflow_tried and self.overflow_recovery
                            and turn < self.max_turns - 1):
                        overflow_tried = overflow_now = True
                        session["_overflow_shrink"] = True   # composer shrinks the history next build
                        self.recorder.log_turn(rid, turn, "overflow",
                                               (comp.error_detail or comp.text or "")[:200],
                                               comp.usage.input_tokens, comp.usage.output_tokens,
                                               meta.prefix_tokens, 0)
                        self._emit("overflow_recovery", detail=(comp.error_detail or comp.text or "")[:200])
                        break
                    if (cls == "retryable" and attempts < self.max_retries
                            and not _budget_exceeded(self.provider.model, total)):
                        delay = self.retry_base * (2 ** attempts)
                        attempts += 1
                        self.recorder.log_turn(rid, turn, "retry",
                            "%s in %.0fs: %s" % (cls, delay, (comp.error_detail or comp.text or "")[:120]),
                            comp.usage.input_tokens, comp.usage.output_tokens, meta.prefix_tokens, 0)
                        self._emit("retry", attempt=attempts, max=self.max_retries, delay_s=delay,
                                   error=(comp.error_detail or comp.text or "")[:200])
                        time.sleep(delay)
                        continue
                    # terminal / retries exhausted / overflow-already-tried: class-prefix res.error
                    # The HTTP status goes in too. Without it a recorded failure cannot be told
                    # apart afterwards: a 529 overload, a 429 rate limit and a 400 read identically
                    # once only the body survives, and "is this Anthropic having a bad minute or is
                    # it us?" is precisely the question the record has to be able to answer.
                    # The class stays the prefix — callers key off "<cls>:" — so the status follows it.
                    # Say what was DECIDED, not only what happened. "terminal" is the classifier's
                    # word for "not retried", and a reader has no way to know whether Collie tried
                    # three times or gave up on the first response. Worse, an error matching none of
                    # the patterns lands here too, so "we did not recognise this" and "we know this
                    # is fatal" printed identically — the mcp_ naming failure spent hours looking
                    # like a quota problem partly because nothing said the message was unrecognised.
                    known = is_known_terminal(comp.error_detail or comp.text or "")
                    note = ("not retried (fatal)" if known else
                            "not retried — this error matches no known pattern, so it was treated "
                            "as fatal rather than retried blindly; the text below is verbatim from "
                            "the provider and may not describe the real cause")
                    if attempts:
                        note = "gave up after %d retries" % attempts
                    comp.text = "%s: [%s] %s%s" % (
                        cls, note, ("HTTP %d " % comp.error_status) if comp.error_status else "",
                        comp.error_detail or comp.text or "provider error")
                    break
                if overflow_now:
                    continue   # rebuild context with shrunk history, then re-run this turn
                u = comp.usage

                # --- prefix measured from provider usage (point #2): on Anthropic the whole cached
                # segment IS system+schemas, so turn-0's cache tokens are the true prefix. DeepSeek's
                # 64-token auto-cache can include stale user bytes, so we only trust the in-run number
                # on Anthropic; DeepSeek uses the `collie prefix --measure` probe instead.
                if turn == 0 and self.provider.name in ("anthropic", "anthropic-oauth") \
                        and comp.stop_reason != "error" and (u.cache_creation + u.cache_read) > 0:
                    # NB anthropic-oauth's cached segment also holds the ~13-tok _CC_SYSTEM identity
                    # block — measured prefix is inflated by that on the OAuth path.
                    res.prefix_measured = u.cache_creation + u.cache_read

                # --- cache-waste detection (point #3)
                skey = ",".join(sorted(s["name"] for s in schemas))
                cause = []
                if prev_skey is not None and skey != prev_skey:
                    cause.append("schema")           # tool set changed (load_tools / hard_at restriction)
                if prev_elide_from and meta.elide_from > prev_elide_from and any(
                        m.get("role") == "tool" and isinstance(m.get("content"), str)
                        and len(m["content"]) > 240
                        for m in session["messages"][prev_elide_from:meta.elide_from]):
                    cause.append("elide")            # history elision newly stubbed a big tool output
                if prev_t and time.time() - prev_t > _CACHE_TTL:
                    cause.append("ttl?")             # NB completion-to-completion incl. generation time
                mt, mu = _cache_miss(prev_prompt, u, self.provider.model, reported_cache)
                c_str = "+".join(cause) or ("unexplained" if mt else "")
                if mt:
                    miss_n += 1; waste_tok += mt; waste_usd += mu
                    self._emit("cache_miss", tokens=mt, usd=mu, cause=c_str)
                prev_skey = skey
                prev_elide_from = meta.elide_from
                prev_t = time.time()
                reported_cache = reported_cache or (u.cache_read + u.cache_creation) > 0
                _p = u.input_tokens + u.cache_read + u.cache_creation
                if _p:
                    prev_prompt = _p

                self.recorder.log_turn(
                    rid, turn, comp.stop_reason,
                    (comp.text or "; ".join(c.name for c in comp.tool_calls))[:200],
                    u.input_tokens, u.output_tokens,
                    meta.prefix_tokens, int((time.time() - tt) * 1000),
                    cache_read=u.cache_read, cache_miss=mt, miss_cause=c_str)

                # Track the ACTUAL stop reason of this completion for the truncation marker + the
                # memory-consolidation gate. Latching only "length" (and never resetting) meant a run
                # that recovered from a mid-way truncation and then finished cleanly still got a false
                # "[answer truncated]" marker and had its correct answer silently dropped from memory.
                last_stop = comp.stop_reason

                # a provider/transport error is NOT the model's answer: don't finalize it
                # as `answer` and don't consolidate it into durable memory as a "fact".
                if comp.stop_reason == "error":
                    res.error = (comp.text or "provider error")[:300]
                    res.turns = turn + 1
                    break

                # --- output truncation (point 1): the response hit the output-token limit, so any
                # tool-call arguments may be silently incomplete. FAIL every call wholesale (you
                # can't tell which one was cut) and never execute them; for a truncated plain answer,
                # nudge to continue. Bounded by trunc_rounds (like verify_max) so it can't spin.
                if comp.stop_reason == "length":
                    trunc_rounds += 1
                    if comp.tool_calls:
                        session["messages"].append(
                            {"role": "assistant", "content": comp.text, "tool_calls": comp.tool_calls,
                             "thinking_blocks": comp.thinking_blocks})
                        for tc in comp.tool_calls:
                            session["messages"].append(
                                {"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                                 "content": TRUNC_MSG})
                            self._emit("tool", name=tc.name, args=tc.args, ok=False)  # visible to surfaces
                    else:
                        session["messages"].append({"role": "assistant", "content": comp.text or "(truncated)"})
                        session["messages"].append({"role": "user", "content": TRUNC_CONTINUE})
                    res.turns = turn + 1
                    # KEY: retrying at the SAME output ceiling truncates again -> the loop the user hit.
                    # Give the retry real room by escalating the cap (x2, bounded). A task that legit
                    # needs a big output finishes; a runaway is still stopped by the round bound below.
                    try:
                        cur = int(getattr(self.provider, "max_tokens", 0) or 0)
                        if cur:
                            self.provider.max_tokens = min(32768, cur * 2)
                    except (TypeError, ValueError):
                        pass
                    if trunc_rounds >= 3 or turn >= self.max_turns - 1:
                        # give up retrying: surface a partial plain answer (with a marker), else error
                        if not comp.tool_calls and (comp.text or "").strip():
                            answer = comp.text
                        else:
                            res.error = res.error or "output-limit truncation loop"
                        break
                    continue

                if comp.tool_calls:
                    session["messages"].append(
                        {"role": "assistant", "content": comp.text,
                         "tool_calls": comp.tool_calls,
                         # preserve signed thinking so the NEXT request can replay it (required by
                         # the API when extended thinking + tool use are both on). Empty when off.
                         "thinking_blocks": comp.thinking_blocks})
                    # ── pass 1: repair + AUTHORIZE every call in this turn, before running any ──
                    # Authorizing up front is the point: when the model proposes five calls, the
                    # human sees all five and decides, instead of discovering the third one only
                    # after the first two already happened irreversibly.
                    _prepared = []
                    for tc in comp.tool_calls:
                        tool = self.registry.get(tc.name)
                        repairs = []
                        if isinstance(tc.args, dict) and "_malformed_args" in tc.args:
                            _prepared.append((tc, tool, repairs, None))
                            continue
                        if tool is not None:
                            # repair known model quirks (json-string args, file_path->path) BEFORE
                            # dispatch. REBUILD tc (never mutate) so the session keeps the model's
                            # raw args for replay fidelity while the tool sees canonical args.
                            rargs, repairs = repair_args(tc.args, getattr(tool, "schema", {}) or {})
                            if repairs:
                                tc = ToolCall(tc.id, tc.name, rargs)
                                res.arg_repairs += 1
                                self._emit("repair", name=tc.name, kinds=repairs)
                        _prepared.append((tc, tool, repairs, self._authorize(tc, tool)))

                    # ── pass 2: execute what cleared ──
                    for tc, tool, repairs, _denied in _prepared:
                        # malformed/truncated JSON args (provider sentinel, point 7): report the REAL
                        # fault, not a misleading "missing required arg".
                        if isinstance(tc.args, dict) and "_malformed_args" in tc.args:
                            out = ("ERROR: tool call arguments were not valid JSON (truncated or "
                                   "malformed). Raw prefix: %s. Re-emit the call with valid JSON "
                                   "arguments." % str(tc.args.get("_malformed_args"))[:500])
                        elif _denied is not None:
                            # The refusal goes back to the model as this call's RESULT, never as a
                            # dropped call: an unpaired tool_use 400s the provider on the next turn
                            # (and on --continue), and a model that is told why can route around it
                            # — write the step into the report for a human instead of doing it.
                            out = "DENIED: %s" % _denied
                            res.denied_calls += 1
                        else:
                            try:                      # a malformed tool call must not abort the run
                                # privacy: placeholders the model emitted ({{SECRET:…}}) are swapped
                                # back to real values ONLY here, at the execution boundary — the
                                # model uses secrets it has never seen. EXCEPTION: the `remember`
                                # tool WRITES its args into durable memory.db (plaintext, and it
                                # reaches every future prompt via prefetch), so restoring here would
                                # persist a real credential the model only ever saw as a placeholder.
                                # For memory writes we keep the placeholder unrestored — this is the SOLE
                                # protection (RememberTool.run does no redaction of its own), so if this
                                # skip ever regressed, a real credential would persist to memory.db.
                                _skip_restore = tc.name == "remember"
                                _run_args = (_redact.restore(tc.args, self._secret_vault)
                                             if (_redact_on and not _skip_restore) else tc.args)
                                out = (tool.run(_run_args, ctx) if tool
                                       else "ERROR: no such tool %s" % tc.name)
                            except Exception as e:
                                out = "ERROR: tool %s failed: %s" % (tc.name, e)
                        if _redact_on and isinstance(out, str):
                            # privacy: credentials in tool OUTPUT (env files, key greps, tracebacks)
                            # never enter the conversation — any cloud provider sees placeholders.
                            out = _redact.redact(out, self._secret_vault)
                        res.tool_calls += 1
                        # Append the tool RESULT immediately, so every tool_use is ALWAYS paired even
                        # if the bookkeeping below throws on a malformed arg — an orphaned tool_use
                        # 400s the provider on the next turn AND on --continue of the saved thread.
                        _tmsg = {"role": "tool", "tool_call_id": tc.id,
                                 "name": tc.name, "content": out}
                        if repairs:
                            _tmsg["repairs"] = repairs   # zero-token; rides session JSON for post-hoc grep
                        session["messages"].append(_tmsg)
                        # Images a tool produced (screenshot) ride ctx, and become a real image block
                        # on the conversation HERE rather than inside the tool_result. Two reasons:
                        # OpenAI's tool-role messages cannot carry images at all, and every provider
                        # already reshapes images on a user message (Anthropic source blocks, OpenAI
                        # image_url, Ollama images[], claude-cli degrades to a marker). So one seam
                        # works everywhere and providers.py needs no change. Drained immediately so
                        # nothing leaks into the next tool call.
                        if getattr(ctx, "images", None):
                            for _img in ctx.images:
                                _lbl = _img.get("label") or "screen"
                                session["messages"].append({"role": "user", "content": [
                                    {"type": "text", "text": "[screenshot: %s]" % _lbl},
                                    {"type": "image", "media_type": _img.get("media_type", "image/png"),
                                     "data": _img["data"]}]})
                            ctx.images.clear()
                        # a compact result preview (first non-empty line + a "more" marker) so the
                        # UI can show a cc-style `⎿ result` under each tool call, not just the action.
                        _rprev = ""
                        if isinstance(out, str) and out.strip():
                            _first = next((ln for ln in out.splitlines() if ln.strip()), "")
                            _rprev = _first[:160] + (" …" if len(out) > len(_first) + 2 else "")
                        self._emit("tool", name=tc.name, args=tc.args,
                                   ok=not (isinstance(out, str) and out.startswith("ERROR")),
                                   result=_rprev)
                        try:            # edit-accounting + repro detection: best-effort bookkeeping
                            if os.environ.get("COLLIE_DEBUG"):
                                import json as _j
                                a = _j.dumps(tc.args, ensure_ascii=False)
                                print("  T%d %s(%s) -> %s" % (
                                    turn, tc.name, a[:90], str(out)[:120].replace("\n", " ")), flush=True)
                            # count an edit ONLY if it actually landed. edit_file/write_file
                            # return "ERROR: old_string not found/appears N times" WITHOUT writing —
                            # DeepSeek mis-quotes constantly, and treating that as success flipped
                            # did_edit=True and disabled every convergence guard.
                            edit_ok = (tc.name in ("write_file", "edit_file")
                                       and isinstance(out, str) and not out.startswith("ERROR"))
                            if edit_ok:
                                did_edit = True
                                last_edit_turn = turn
                                # A landed edit INVALIDATES any prior reproduction: tool calls in one
                                # turn are processed in order, so a pass+assert repro that ran BEFORE a
                                # same-turn breaking edit would otherwise share this turn's key and read
                                # as fresh (last_repro_turn >= last_edit_turn), stamping a broken edit
                                # VERIFIED. Reset so only a repro that runs AFTER this edit can clear
                                # the gate. (audit: same-turn repro-then-edit false-verify.)
                                last_repro_turn, last_repro_failed, last_repro_asserted = -100, False, False
                                p = tc.args.get("path", "")
                                if p:
                                    p = p if isinstance(p, str) else str(p)   # malformed non-str path
                                    rp = (os.path.relpath(p, self.cwd)
                                          if os.path.isabs(p) else p)
                                    edited_files.add(rp)
                                    last_edit_path = rp
                                last_edit_text = (tc.args.get("new_string")
                                                  or tc.args.get("content") or last_edit_text)
                                self._emit("edit", path=last_edit_path,
                                           old=tc.args.get("old_string", ""),
                                           new=tc.args.get("new_string") or tc.args.get("content", ""))
                                if self.force_edit:
                                    # snapshot the net tree state after every landed edit — the
                                    # white-flag guard restores the LAST non-empty one if the
                                    # model later reverts itself into an empty patch
                                    best_diff = _tree_diff(self.cwd) or best_diff
                            # reproduction evidence: a post-edit `python -c` we can gate finish on
                            if did_edit and _is_repro_cmd(tc.name, tc.args):
                                last_repro_turn = turn
                                o = out if isinstance(out, str) else str(out)
                                last_repro_failed = _repro_failed(o)
                                # \bassert\b (not a bare substring) so "reassert"/print("assert")
                                # don't satisfy the assert-mode gate without a real assertion.
                                last_repro_asserted = bool(
                                    _ASSERTED_RE.search(tc.args.get("command") or ""))
                                self._emit("repro", passed=not last_repro_failed,
                                           asserted=last_repro_asserted,
                                           cmd=(tc.args.get("command") or "")[:200])
                        except Exception as _acc_e:
                            if os.environ.get("COLLIE_DEBUG"):
                                print("  [accounting error, continuing] %s" % _acc_e, flush=True)
                    res.turns = turn + 1
                    # converge: still exploring past the deadline with no edit -> nudge ONCE
                    # (then hard tool-restriction at hard_at does the structural forcing;
                    # re-injecting every turn just accumulated duplicate identical messages).
                    if (self.force_edit and not did_edit and not edit_forced
                            and turn + 1 >= force_at and turn < self.max_turns - 1):
                        session["messages"].append(
                            {"role": "user", "content": EDIT_FORCE_NUDGE})
                        edit_forced = True
                    # embedding-driven multi-file coverage: right after the first edit,
                    # surface sibling locations (by similarity to the edit) that likely
                    # need the same change — proactive, not "please go grep".
                    elif (self.force_edit and did_edit and not multifile_hinted
                          and last_edit_text and self.registry.get("code_search")
                          and turn < self.max_turns - 1):
                        from .codeindex import related_locations
                        # k=8, not 4: a real gold sibling (pylint-4551 writer.py) can sit at
                        # rank ~6, invisible at k=4. More candidates cost one message; the
                        # model filters. Recall matters more than precision for coverage.
                        rels = related_locations(self.cwd, last_edit_text,
                                                 last_edit_path, edited_files, k=8)
                        multifile_hinted = True
                        if rels:
                            # NOTE (honest negative): a stronger "you MUST edit each" wording was
                            # tried and gave NO coverage gain across pylint-4551/4604/seaborn-3187
                            # (DeepSeek-V3 reliably fixes the primary file and won't commit
                            # coordinated sibling edits even when told + given turns — a model
                            # ceiling, not a prompt bug) and risked over-editing. Kept the mild,
                            # neutral wording; only the k (recall) bump above is retained.
                            session["messages"].append({"role": "user", "content":
                                "Embedding-related locations in OTHER files that may need "
                                "the SAME change — check each and `edit_file` the ones that "
                                "do (ignore those that don't):\n" + "\n".join(rels)})
                    # cap post-edit churn: once edited AND coverage has been offered, if the
                    # model keeps calling tools for several turns without a NEW successful
                    # edit, it is spinning (re-reading, testing a broken env, chasing files
                    # that don't need changes) — finish with what we have. On flask this cut
                    # a 35-turn run to ~20 without losing the fix.
                    # Window = 5: kept. Tried 8 to give the multi-file hint room, but the model
                    # doesn't commit sibling edits regardless (see the note above), so a wider
                    # window only re-inflated single-file runs (flask 20→23) for zero coverage
                    # gain. 5 preserves the flask 35→20 efficiency win.
                    elif (self.force_edit and did_edit and multifile_hinted
                          and turn - last_edit_turn >= (8 if (self.coverage_gate or self.verify_gate)
                                                        else 5)):
                        # don't spin-break OUT of an UNSATISFIED verify gate — the break used to let
                        # a post-edit tool-spin finish with the reproduction never passing (or never
                        # run), defeating verify_gate/require_assert. Push a repair nudge instead
                        # (bounded by verify_max); otherwise break as before.
                        if self.verify_gate:
                            _repro_ok = self._repro_verified(
                                did_edit, last_edit_turn, last_repro_turn,
                                last_repro_failed, last_repro_asserted)
                            if not _repro_ok and verify_rounds < self.verify_max:
                                session["messages"].append(
                                    {"role": "user", "content": self.repair_nudge or REPAIR_NUDGE})
                                verify_rounds += 1
                                res.turns = turn + 1
                                continue
                        # white-flag guard: don't spin-break out holding an EMPTY tree when a
                        # non-empty edit state existed — rescue turn(s) first (the spin window
                        # re-arms, so the model gets a bounded second chance to land something)
                        if (rollback_rounds < 1 and best_diff
                                and turn < self.max_turns - 1 and _tree_empty(self.cwd)):
                            session["messages"].append(
                                {"role": "user", "content": ROLLBACK_NUDGE})
                            rollback_rounds += 1
                            res.turns = turn + 1
                            continue
                        break
                    continue

                # reproduce -> verify -> repair, EVIDENCE-gated (not a single advisory nudge).
                # Don't accept "done" after an edit until a reproduction actually ran on the
                # FIXED code (turn >= last edit) and its last run didn't error. Bounded so a
                # stubborn model can't spin; falls back to the old one-shot nudge when gate off.
                if self.self_verify and did_edit and turn < self.max_turns - 1:
                    if self.verify_gate:
                        # assert-mode: a print-only repro (no `assert`) is NOT verification —
                        # the wrong-output-doesn't-raise hole. Decision lives in verifier.py.
                        repro_ok = self._repro_verified(
                            did_edit, last_edit_turn, last_repro_turn,
                            last_repro_failed, last_repro_asserted)
                        if not repro_ok and verify_rounds < self.verify_max:
                            nudge = ((self.verify_nudge or VERIFY_NUDGE)
                                     if last_repro_turn < last_edit_turn
                                     else (self.repair_nudge or REPAIR_NUDGE))
                            session["messages"].append({"role": "assistant", "content": comp.text})
                            session["messages"].append({"role": "user", "content": nudge})
                            verify_rounds += 1
                            res.turns = turn + 1
                            continue
                    elif not verified:
                        session["messages"].append({"role": "assistant", "content": comp.text})
                        session["messages"].append(
                            {"role": "user", "content": self.verify_nudge or VERIFY_NUDGE})
                        verified = True
                        res.turns = turn + 1
                        continue

                # the model wants to finish. If it never edited on a fix task, don't
                # accept the empty result — push it to make the change.
                if (self.force_edit and not did_edit and turn < self.max_turns - 1):
                    session["messages"].append({"role": "assistant", "content": comp.text})
                    session["messages"].append({"role": "user", "content": EDIT_FORCE_NUDGE})
                    res.turns = turn + 1
                    continue

                # edited and finishing: coverage pass for multi-file fixes.
                if self.force_edit and did_edit and turn < self.max_turns - 1:
                    if self.coverage_gate and self.registry.get("code_search"):
                        # RECOMPUTE against the grown edited_files (the one-shot hint only used
                        # the first edit's exclude set, so already-edited siblings never got
                        # re-surfaced). Re-surface still-uncovered strong same-package siblings,
                        # bounded + ADVISORY (the calibration showed a score threshold can't tell
                        # a needed sibling from an incidental same-package file, so we must NOT
                        # hard-force — we trust Opus to filter, unlike DeepSeek). Score-scoped so
                        # a single-file fix surfaces at most a short list it can dismiss.
                        from .codeindex import related_scored
                        cand = related_scored(self.cwd, last_edit_text, last_edit_path,
                                              edited_files, k=8, min_score=self.cov_thresh)
                        if cand and coverage_rounds < self.coverage_max:
                            locs = "\n".join("%s (rel %.2f)" % (l, s) for l, s in cand)
                            session["messages"].append({"role": "assistant", "content": comp.text})
                            session["messages"].append({"role": "user", "content":
                                COVERAGE_NUDGE + "\nSame-package files closest to your change "
                                "(edit the ones that need the SAME fix; ignore those that "
                                "don't, then finish):\n" + locs})
                            coverage_rounds += 1
                            res.turns = turn + 1
                            continue
                    elif not covered:
                        session["messages"].append({"role": "assistant", "content": comp.text})
                        session["messages"].append({"role": "user", "content": COVERAGE_NUDGE})
                        covered = True
                        res.turns = turn + 1
                        continue

                # adversarial critic: an INDEPENDENT fresh read attacks the fix before we accept it.
                # Self-attack shares the model's blind spot (a misread attacks from the same misread);
                # a separate read that sees ONLY issue+diff catches under-coverage and misreads a
                # self-nudge cannot. Bounded critic->repair rounds.
                if (self.critic and did_edit and turn < self.max_turns - 1
                        and critic_rounds < self.critic_max):
                    _cdiff = _tree_diff(self.cwd)
                    if _cdiff:
                        _ok, _obj = (self.critic_fn(self.critic_issue, _cdiff, self.cwd)
                                     if self.critic_fn else
                                     self._run_critic(self.critic_issue, _cdiff))
                        if getattr(self, "_critic_usage", None):   # count the critic's own tokens/$
                            total.add(self._critic_usage); self._critic_usage = None
                        if not _ok:
                            session["messages"].append({"role": "assistant", "content": comp.text})
                            session["messages"].append({"role": "user", "content":
                                "An INDEPENDENT reviewer (fresh read of the issue — did NOT see your "
                                "reasoning or your test) examined your diff and raised this concern:\n\n"
                                + _obj + "\n\nIf it is valid, fix it and re-verify in run_in_env. If you "
                                "are confident it is unfounded, prove it with a run_in_env check of "
                                "exactly that case, then finish."})
                            critic_rounds += 1
                            res.turns = turn + 1
                            continue

                # steering finish-interception (point 13, point B): if the user typed something while
                # the model was deciding to finish, honor it instead of stopping — same gate pattern
                # as verify/coverage. Guard BEFORE draining so a steer typed on the LAST turn stays
                # queued for the next REPL prompt rather than vanishing.
                if turn < self.max_turns - 1:
                    steers = self._drain_steering()
                    if steers:
                        txt = "\n".join(steers)
                        session["messages"].append({"role": "assistant", "content": comp.text})
                        session["messages"].append({"role": "user", "content": txt})
                        res.steer_count += 1
                        self._emit("steer", text=txt[:200])
                        self.recorder.log_turn(rid, turn, "steer", txt[:500], 0, 0, 0, 0)
                        res.turns = turn + 1
                        continue

                # white-flag guard (voluntary finish): the model says done but the tree holds
                # ZERO net changes after edits happened — it reverted itself (sphinx-10435).
                if (self.force_edit and did_edit and rollback_rounds < 1 and best_diff
                        and turn < self.max_turns - 1 and _tree_empty(self.cwd)):
                    session["messages"].append({"role": "assistant", "content": comp.text})
                    session["messages"].append({"role": "user", "content": ROLLBACK_NUDGE})
                    rollback_rounds += 1
                    res.turns = turn + 1
                    continue

                answer = comp.text
                res.turns = turn + 1
                break
            else:
                turns_exhausted = True

            # mechanical white-flag restore (the belt to ROLLBACK_NUDGE's braces): every rescue
            # is spent and the tree is STILL empty — put the last non-empty edit state back.
            # A wrong patch can score at eval; an empty one is a guaranteed zero.
            if self.force_edit and did_edit and best_diff and _tree_empty(self.cwd):
                ok = _apply_diff(self.cwd, best_diff)
                self.recorder.log_turn(rid, res.turns, "rollback",
                                       "empty tree at finish — restored last non-empty diff "
                                       "(%d B): %s" % (len(best_diff), "ok" if ok else "FAILED"),
                                       0, 0, 0, 0)
                self._emit("rollback", ok=ok, size=len(best_diff))

            if not answer:
                # The loop ended WITHOUT the voluntary no-tool finish (spin-break, range exhaustion,
                # or a tool call on the FINAL available turn — a common case). Never return an empty
                # answer while a valid edit may have landed: prefer the last completion's text, else
                # do ONE final no-tools completion to synthesize a summary from the thread.
                # BUT: an error completion is NOT an answer (points 4/5/9) — leave `answer` empty so
                # surfaces fall through to res.error and memory never consolidates the error text.
                last_err = "comp" in dir() and getattr(comp, "stop_reason", "") == "error"
                last_text = (getattr(comp, "text", "") or "").strip() if "comp" in dir() else ""
                if res.error or last_err:
                    pass                          # keep answer empty -> `res.answer or res.error` shows the error
                elif last_text:
                    answer = comp.text
                elif budget_hit:                  # don't spend MORE past the ceiling on a synthesis
                    answer = "(stopped at budget — see the edits/tools above)"
                else:
                    # A run cut off mid-task must not fall back on the word "done". Measured: with a
                    # tight turn budget the loop ends here, the synthesis comes back empty, and every
                    # run answered "(done — see the edits/tools above)" having never run a single
                    # check — in the verify-gated mode too, since running out of turns leaves the
                    # loop from outside the gate.
                    _unfinished = "(ran out of turns — UNFINISHED; see the edits/tools above)"
                    _placeholder = _unfinished if turns_exhausted else "(done — see the edits/tools above)"
                    try:
                        # synthesize from the ELIDED history (composer.build), not the raw thread —
                        # the raw thread is the single most likely place to actually overflow.
                        _sys2, msgs2, _m2 = self.composer.build(
                            session, user_msg, self.cwd, self.project, self.mode)
                        fin = self.provider.complete(_sys2, msgs2, [], on_text=self.stream_cb)
                        total.add(fin.usage)
                        if fin.stop_reason == "error":   # don't let a failed synthesis become the answer
                            res.error = res.error or (fin.text or "provider error")[:300]
                            answer = _placeholder
                        else:
                            answer = (fin.text or "").strip() or _placeholder
                    except Exception:
                        answer = _placeholder
            if budget_hit and answer:
                answer += "\n\n_[stopped: budget ceiling reached]_"
            if turns_exhausted and answer and "ran out of turns" not in answer:
                # The cost ceiling has always said so; the turn ceiling never did, so a summary
                # written mid-task read as a finished report — including when no check had run.
                answer += ("\n\n_[stopped: ran out of turns (%d) — this task was NOT finished, and "
                           "nothing above was necessarily verified]_" % self.max_turns)
            if last_stop == "length" and answer and "truncated" not in answer:
                answer += "\n\n_[answer truncated at output-token limit]_"   # visible half of point 1

            res.answer = answer
            # never consolidate MOCK runs — their canned "Based on the tool output: …" answers are
            # test plumbing, not durable facts, and were polluting memory.db on every selftest.
            # Also skip a length-stopped answer: an incomplete "fact" shouldn't enter durable memory.
            if (consolidate and answer and getattr(self.provider, "name", "") != "mock"
                    and last_stop != "length"):
                # self-cleaning write: distill task+answer into a durable fact
                self.memory.remember(
                    text="Task '%s' -> %s" % (task_id, answer[:200]),
                    keys=task_id, project=self.project)
        except Exception as e:
            res.error = "%s: %s" % (type(e).__name__, e)

        res.input_tokens = total.input_tokens
        res.output_tokens = total.output_tokens
        res.cache_read = total.cache_read
        res.cache_creation = total.cache_creation
        res.cache_miss_tokens = waste_tok
        res.cache_waste_usd = round(waste_usd, 6)
        res.total_tokens = (total.input_tokens + total.output_tokens +
                            total.cache_read + total.cache_creation)
        from .costs import cost_usd            # $ was never computed -> recorder logged 0
        res.cost_usd = cost_usd(self.provider.model, res.input_tokens,
                                res.output_tokens, res.cache_read, res.cache_creation)
        res.wall_ms = int((time.time() - t0) * 1000)
        # ensure the thread ENDS with the final answer (the no-tool-call path breaks without
        # appending it) so a --continue'd next turn sees what this turn concluded.
        m = session["messages"]
        if answer and not (m and m[-1].get("role") == "assistant" and m[-1].get("content") == answer):
            m.append({"role": "assistant", "content": answer})
        res.messages = m                      # expose the thread so a session can be saved/continued
        self.recorder.finish_run(res)
        # final receipt — the honest token/time/$ tally + the verification verdict, for the
        # streaming UX / editor / ACP surfaces (the "$" the brand promises, now on the wire).
        # verified = edited + a repro ran on the FIXED code + it didn't fail + (in assert-mode) it
        # actually executed an assertion — matching the gate's own definition, so the receipt can't
        # claim "verified" for a print-only repro under require_assert. Same verifier.py decision
        # as the finish gate, so the receipt can never disagree with why the run was allowed to stop.
        res.verified = self._repro_verified(
            did_edit, last_edit_turn, last_repro_turn,
            last_repro_failed, last_repro_asserted)
        self._emit("receipt", verified=res.verified,
                   prefix_tokens=res.prefix_tokens, prefix_measured=res.prefix_measured,
                   input_tokens=res.input_tokens,
                   output_tokens=res.output_tokens, total_tokens=res.total_tokens,
                   turns=res.turns, tool_calls=res.tool_calls,
                   wall_ms=res.wall_ms, cost_usd=res.cost_usd,
                   cache_waste_usd=res.cache_waste_usd, cache_misses=miss_n, error=res.error)
        # Debug: dump the FULL transcript (messages + tool outputs) for offline diagnosis of
        # loop behavior (e.g. why the assert-verify loop doesn't converge on a hard instance).
        # COLLIE_DUMP_TRANSCRIPT=<dir> writes <dir>/<task>_<runid>.json. Opt-in, no prod cost.
        dump_dir = os.environ.get("COLLIE_DUMP_TRANSCRIPT")
        if dump_dir:
            try:
                os.makedirs(dump_dir, exist_ok=True)
                with open(os.path.join(dump_dir, "%s_%s.json" % (task_id, rid)), "w") as f:
                    json.dump({"task": task_id, "run_id": rid, "turns": res.turns,
                               "wall_ms": res.wall_ms, "total_tokens": res.total_tokens,
                               "messages": session["messages"]}, f, default=str, ensure_ascii=False)
            except Exception:
                pass
        return res
