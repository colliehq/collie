"""The agentic loop — wires provider + tools + memory + context + recorder.

    while stop_reason == "tool_use":
        system, msgs, meta = composer.build(...)     # tiered prompt + auto-prefetch
        completion       = provider.complete(...)    # model turn
        run tools -> append tool_results
    consolidate(task, answer) -> memory.remember     # self-cleaning write path
"""
from __future__ import annotations
import ast
import hashlib
import itertools
import json
import os
import re
import shlex
import subprocess
import threading
import time

from . import __version__
from . import compaction as _compaction
from . import preflight as _preflight
from . import redact as _redact
from . import run_ownership as _ownership
from . import settings as _settings
from .context import ContextComposer
from .hooks import HookManager
from .providers import (ModelProvider, Usage, ToolCall, classify_error, content_text,
                        is_overflow, is_known_terminal, issued_requests, request_count_of,
                        _error_completion, provider_retry_at)
from .recorder import Recorder, RunResult
from . import tools as _tools
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

# A response-contract miss is not a transport outage: the model request completed, but its output
# could not safely drive the executor.  This nudge is temporary (never written to the durable
# conversation) and the loop permits it exactly once per run, through the ordinary provider budget
# and request-authority path.
FORMAT_REPAIR_NUDGE = (
    "Your previous response could not be parsed by the active structured response contract. "
    "Retry the same next action now. Produce exactly one valid response: one tool call with an "
    "object of arguments, or one final answer. Do not return a list or batch, multiple alternatives, "
    "extra keys, Markdown fences, or prose outside the required response envelope.")

# The one corrective turn above used to be blind: whatever the model got wrong, it was told only
# that the reply "could not be parsed", and a real run still failed after its repair.
# The refused text was not saved, so that run's exact cause remains unknown. The provider
# now classifies the shape (providers.CONTRACT_MISS_REASONS) without retaining the text, naming the
# one fact that decides the next reply.  Each hint stays content-free: it describes the SHAPE that
# was refused, never what the model wrote.
CONTRACT_REPAIR_HINTS = {
    "empty_response": "Your previous reply carried no text at all — emit the response object itself.",
    "no_json_object": "Your previous reply was prose with no JSON object in it.",
    "malformed_json": "Your previous reply was JSON-shaped but did not parse. Escape string "
                      "contents properly (\\\\ for a backslash, \\\" for a quote, \\n for a "
                      "newline) — a raw newline or a stray backslash inside a string breaks it.",
    "ambiguous_json": "Your previous reply parsed but used a duplicate object key or a non-finite "
                      "number (NaN/Infinity), which the contract refuses.",
    "not_a_json_object": "Your previous reply was valid JSON but not a single object; a list or "
                         "batch of actions is refused. Send one action.",
    "multiple_envelopes": "Your previous reply contained more than one response object.",
    "extra_keys": "Your previous reply carried keys outside the contract: a call has exactly "
                  "\"tool\" and \"args\", an answer has exactly \"answer\".",
    "args_not_object": "In your previous reply \"args\" was not a JSON object; it must be an "
                       "object, even when empty.",
    "answer_not_string": "In your previous reply \"answer\" was not a string.",
    "unknown_tool": "Your previous reply named a tool the executor does not have.",
    "not_an_envelope": "Your previous reply was a JSON object that was neither a tool call nor "
                       "an answer.",
    "provider_schema_refusal": "The provider's own formatter refused your previous reply against "
                               "this contract's schema.",
    # Kept in lockstep with providers.CONTRACT_MISS_REASONS: a category with no
    # hint would silently degrade the one corrective turn back to generic advice.
    "read_batch_not_allowed": "Your previous reply used a \"reads\" batch, which this request "
                              "does not offer. Send one tool call or one answer.",
    "read_batch_invalid": "Your previous reply's \"reads\" batch was refused as a whole: it must "
                          "be 1-8 entries, each an object with a non-empty \"path\" and at most "
                          "\"offset\", \"limit\" and \"max_bytes\" (positive integers).",
}


def _safe_contract_reason(completion) -> str:
    """Only known categories may cross into events, receipts and persisted records."""
    reason = getattr(completion, "contract_reason", "")
    return reason if type(reason) is str and reason in CONTRACT_REPAIR_HINTS else ""


def format_repair_nudge(reason: str = "", tool_names=()) -> str:
    """The corrective user turn, sharpened by a content-free miss category.

    ``tool_names`` is the host's own active allowlist, not anything read out of
    the refused reply, so naming it cannot leak model output back to the model.
    """
    hint = CONTRACT_REPAIR_HINTS.get(str(reason or ""), "")
    names = [str(name) for name in (tool_names or []) if str(name)]
    if reason == "unknown_tool" and names:
        hint += " The executor's tools are: %s." % ", ".join(sorted(names))
    return FORMAT_REPAIR_NUDGE + ((" " + hint) if hint else "")


# Wording shared by every post-edit reminder, so the run-hygiene rule and the "answer the
# user, not the reminder" rule cannot drift apart between the variants below.
_VERIFY_HYGIENE = ("Run the check directly, without piping to head/tail, appending echo, or "
                   "hiding its exit status; the tool already bounds long output. ")
_VERIFY_TAIL = ("This is an internal verification reminder for the original task, not a new "
                "user request. Then answer the original request in the user's requested format "
                "and level of detail, briefly noting the result and any remaining limitations.")

VERIFY_NUDGE = ("Before finalizing, use the bash tool to run the project's relevant tests "
                "(`python -m pytest -q`, `npm test`, `go test ./...`, `cargo test`, or this "
                "repository's equivalent). If anything fails, read the error, fix it, and re-run. "
                + _VERIFY_HYGIENE + _VERIFY_TAIL)

# The same reminder when the host could look at the workspace and found NO check to run.
# A JSON/CSV/Markdown deliverable has no suite, and naming pytest anyway is what makes a model
# run a test runner in a directory it has just confirmed holds no tests — which costs a turn and
# leaves a .pytest_cache behind in a workspace whose task never authorized one.  This text must
# not imply the cheap check is the grade: re-reading a file proves what it contains and nothing
# more, so the model is asked to say which requirements remain unverified.
#
# It must also stay TASK-NEUTRAL.  An earlier version named "the JSON or CSV" and its fields,
# which reads as an instruction about a deliverable the task may not have: the observed run was
# a README edit in a Python workspace, and the reminder answered it with a data-file recipe.
_NO_CHECK_VERIFY_NUDGE = (
    "Before finalizing: the host did not detect a supported test, build or typecheck command "
    "in this workspace. Follow any project instructions or existing applicable checks; the "
    "detector does not recognize every project layout. When no applicable suite exists, do "
    "not install a toolchain or create a test project just to run a check, and do not invoke "
    "a test runner known to collect nothing. Instead inspect what you actually produced%s "
    "in whatever form it takes, using a command that writes nothing into this workspace. "
    "Inspecting the result proves what it contains; it does not prove the request was "
    "satisfied, so state briefly which of the request's requirements you checked and which "
    "remain unverified. " + _VERIFY_HYGIENE + _VERIFY_TAIL)

# The reminder when this run ALREADY executed a check here and the host watched it succeed.
# Static discovery answers "what check does this project appear to own?"; an executed command
# answers "what check does this project actually run, in this environment, right now" — which
# is strictly the better-evidenced of the two, so it names the command.  It is still only
# WORDING: the earlier run happened before the later edit and therefore certifies nothing about
# the bytes on disk now, which is exactly why the model is being asked to run it again.
_RAN_CHECK_VERIFY_NUDGE = (
    "Before finalizing, use the bash tool to re-run the check that already ran successfully in "
    "this workspace: `%s`. You have edited files since that run, so its result does not cover "
    "them. If anything fails, read the error, fix it, and re-run. "
    + _VERIFY_HYGIENE + _VERIFY_TAIL)

_VERIFY_EDITED_NAMES = 4
# A reminder quotes a command back to the model, so keep it to something a person would
# recognize as one line of shell rather than pasting an arbitrarily long payload.
_VERIFY_COMMAND_CHARS = 300


def _same_dir(a, b) -> bool:
    """Whether two paths name the same directory, as far as this host can tell."""
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
            os.path.abspath(str(b)))
    except (OSError, ValueError, TypeError):
        return False


def _reusable_check_command(ran_checks, cwd=None) -> str:
    """The most recent host-observed successful check command worth quoting, or ``""``.

    Entries recorded by the loop are ``(cwd, command)``: a command is only worth re-running
    where it ran.  ``python -m unittest -q`` that passed in one directory says nothing about a
    workspace the run has since moved to, and quoting it there would name a check that may not
    even exist.  ``ran_checks`` is also a per-run list, never persisted, so a success from an
    earlier run cannot reach this at all.  A bare string is accepted for callers that pass one
    explicitly, and carries no directory claim.
    """
    for entry in reversed(list(ran_checks or [])):
        if isinstance(entry, (tuple, list)) and len(entry) == 2:
            where, command = entry
            if cwd is not None and not _same_dir(where, cwd):
                continue
        elif isinstance(entry, str):
            command = entry
        else:
            continue
        if not isinstance(command, str):
            continue
        text = command.strip()
        if text and len(text) <= _VERIFY_COMMAND_CHARS and "\n" not in text:
            return text
    return ""


def verify_nudge_for(cwd, edited_paths=(), ran_checks=()) -> str:
    """The post-edit reminder, chosen from what this workspace can actually verify.

    ``detect_verification_commands`` is the product's existing evidence-based answer to "what
    check does this project own?" — it reads markers, executes nothing, and is already what the
    UI proposes and what Test mode allowlists.  The loop simply never asked it, so the advisory
    named pytest first on every task, including ones with no code in them at all.

    ``ran_checks`` are check commands the HOST watched exit zero earlier in this same run (see
    ``_host_observed_check``).  They outrank detection for wording, because an execution that
    happened is better evidence that a command exists and works here than a marker file is.

    This selects *wording* only.  Whether a finish is accepted stays with the verify gate and
    with the host check receipt; neither is consulted or relaxed here.  In particular a reused
    command is named precisely because its earlier run is stale — it is never counted as the
    fresh evidence the gate is asking for.
    """
    reusable = _reusable_check_command(ran_checks, cwd)
    if reusable:
        return _RAN_CHECK_VERIFY_NUDGE % reusable
    if not cwd:
        return VERIFY_NUDGE
    try:
        from .verification import detect_verification_commands
        candidates = detect_verification_commands(str(cwd))
    except Exception:
        # Detection is an optimization, never a precondition: an unreadable or exotic
        # workspace keeps the original generic reminder rather than losing the reminder.
        return VERIFY_NUDGE
    if candidates:
        first = candidates[0]
        return (("Before finalizing, use the bash tool to run this project's own check: `%s` "
                 "(detected from %s), or this repository's equivalent. If anything fails, read "
                 "the error, fix it, and re-run. ") % (first["command"], first["source"])
                + _VERIFY_HYGIENE + _VERIFY_TAIL)
    names = [str(p) for p in (edited_paths or []) if str(p).strip()]
    listed = (" (%s)" % ", ".join(sorted(names)[:_VERIFY_EDITED_NAMES])) if names else ""
    return _NO_CHECK_VERIFY_NUDGE % listed


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

# A real test runner is stronger evidence than a hand-written one-off reproduction.  The original
# gate deliberately excluded whole suites, while its own default nudge told the model to run pytest;
# ordinary Web Required therefore had no command that could satisfy it.  Keep this anchored to shell
# command boundaries so prose such as ``echo pytest`` is not mistaken for execution.
_TEST_RUNNER_RE = re.compile(
    r'(^|[;&|]\s*)\s*('
    # Common non-executing interpreter flags (not -c, help/version, or -O).
    # In particular -B is used for artifact checks that must not create caches.
    r'(python(?:3(?:\.\d+)?)?|py)(?:\.exe)?'
    r'(?:\s+-(?:[BEIsSu]+|3(?:\.\d+)?))*\s+-m\s+(pytest|unittest|nose)\b'
    r'|(uv|poetry)\s+run\s+(pytest|python\s+-m\s+pytest)\b'
    r'|pytest\b|tox\b|nox\b'
    r'|(npm|pnpm|yarn|bun)\s+(run\s+)?test(?=[:\s]|$)'
    r'|(npx|npm\s+exec|pnpm\s+exec|yarn\s+exec)\s+(jest|vitest|mocha|ava)\b'
    r'|go\s+test\b|cargo\s+(test|nextest\s+run)\b'
    r'|(mvn|\./mvnw)\s+[^\n;|]*\b(test|verify)\b'
    r'|\./gradlew(?:\.bat)?\s+[^\n;|]*\btest\b'
    r'|dotnet\s+test\b|swift\s+test\b|mix\s+test\b'
    r'|(?:bundle\s+exec\s+)?rspec\b|(?:vendor/bin/)?phpunit\b|make\s+test\b'
    r')', re.IGNORECASE)


def _shell_unquoted_at(text: str, index: int) -> bool:
    """Whether ``index`` is outside shell string literals/backticks.

    Regex command boundaries alone are insufficient: ``python -c \"print('&& pytest')\"`` contains
    exactly the same bytes as a chained runner but executes only a print.  A tiny lexer is enough
    to reject that evidence without interpreting the shell command itself.
    """
    single = double = backtick = escaped = False
    for ch in text[:index]:
        if escaped:
            escaped = False
            continue
        if ch == "\\" and not single:
            escaped = True
        elif ch == "'" and not double and not backtick:
            single = not single
        elif ch == '"' and not single and not backtick:
            double = not double
        elif ch == "`" and not single:
            backtick = not backtick
    return not (single or double or backtick)


_HEREDOC_TOKEN_RE = re.compile(
    r"<<(?P<tabs>-)?\s*(?:'(?P<sq>[^'\r\n]+)'|\"(?P<dq>[^\"\r\n]+)\"|"
    r"(?P<bare>[A-Za-z_][A-Za-z0-9_]*))")


def _split_first_heredoc(command: str):
    """Return ``(intro, body, suffix)`` for the first complete, unquoted here-document."""
    for match in _HEREDOC_TOKEN_RE.finditer(command):
        if not _shell_unquoted_at(command, match.start()):
            continue
        line_end = command.find("\n", match.end())
        if line_end < 0:
            return None
        delimiter = match.group("sq") or match.group("dq") or match.group("bare")
        body_start = line_end + 1
        pos = body_start
        while pos <= len(command):
            next_end = command.find("\n", pos)
            record_end = len(command) if next_end < 0 else next_end
            record = command[pos:record_end].rstrip("\r")
            compared = record.lstrip("\t") if match.group("tabs") else record
            if compared == delimiter:
                suffix_at = len(command) if next_end < 0 else next_end + 1
                return command[:line_end], command[body_start:pos], command[suffix_at:]
            if next_end < 0:
                break
            pos = next_end + 1
        return None
    return None


def _shell_control_surface(command: str) -> str:
    """Remove here-document payloads, whose punctuation is data rather than shell syntax."""
    surface = ""
    remaining = command
    while True:
        parts = _split_first_heredoc(remaining)
        if parts is None:
            return surface + remaining
        intro, _body, suffix = parts
        surface += intro
        if not suffix.strip():
            return surface
        # A real command after the delimiter is a new shell command and must remain visible to the
        # unsafe-control check below. Multiple here-documents are handled one suffix at a time.
        surface += "\n"
        remaining = suffix


def _has_unsafe_test_shell_control(command: str) -> bool:
    """Reject shell composition that can hide a failing test runner.

    Required verification trusts the tool's process exit code.  A pipeline normally reports the
    last process and ``||``/``;``/background execution can similarly turn a failed test into a
    successful shell command.  ``&&`` is safe because a failing runner still makes the whole chain
    fail.  Redirection forms such as ``2>&1`` and ``&>log`` do not change the exit status.

    This is intentionally conservative: an unfamiliar compound command should not count as proof
    even if it might be safe under one particular shell configuration.
    """
    command = _shell_control_surface(command)
    single = double = backtick = escaped = False
    i = 0
    while i < len(command):
        ch = command[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\" and not single:
            escaped = True
            i += 1
            continue
        if ch == "'" and not double and not backtick:
            single = not single
            i += 1
            continue
        if ch == '"' and not single and not backtick:
            double = not double
            i += 1
            continue
        if ch == "`" and not single:
            backtick = not backtick
            i += 1
            continue
        if single or double or backtick:
            i += 1
            continue
        if ch in ("\n", "\r", ";", "|"):
            return True
        if ch == "$" and i + 1 < len(command) and command[i + 1] == "(":
            return True
        if ch == "&":
            previous = command[i - 1] if i else ""
            following = command[i + 1] if i + 1 < len(command) else ""
            if following == "&":
                i += 2
                continue
            if previous == "&":  # already consumed by the paired branch above
                i += 1
                continue
            if previous == ">" or following == ">":  # 2>&1 / &>file redirection
                i += 1
                continue
            return True
        i += 1
    return False


def _is_test_runner_cmd(command: str) -> bool:
    """True only for a runner that will execute tests, not collect/build/list them."""
    c = str(command or "")
    if _has_unsafe_test_shell_control(c):
        return False
    if not any(_shell_unquoted_at(c, match.start(2))
               for match in _TEST_RUNNER_RE.finditer(c)):
        return False
    try:
        words = shlex.split(c)
    except ValueError:
        return False
    listing_flags = {"--collect-only", "--co", "--no-run", "--listtests", "--list-tests",
                     "--help", "-h", "--version", "--fixtures", "--fixtures-per-test",
                     "--markers"}
    return not any(word == "-V" or word.lower() in listing_flags for word in words)


# Does the command actually CHECK a result, as opposed to merely proving the code builds?
# `\bassert\b` alone is a Python idiom; a Go agent asserts with t.Fatal/t.Error, a JS one with
# expect(). Running an actual test runner counts too — it is executable correctness evidence.
# `go build` deliberately does NOT count: compiling is necessary, never sufficient, and letting it
# satisfy require_assert would reopen the print-only hole in a new language.
_ASSERTED_RE = re.compile(
    r'\bassert\b|\bt\.(Fatal|Error)f?\b|\bexpect\(|\brequire\.\w|\bshould\b\.'
    r'|go\s+test\b[^\n;|]*\s-run\b|cargo\s+test\b[^\n;|]*\S'
    r'|npx\s+(jest|vitest|mocha|ava)\b[^\n;|]*\S|'
    + _TEST_RUNNER_RE.pattern, re.IGNORECASE)


def _is_asserting_cmd(command: str) -> bool:
    """Whether a recognized reproduction contains an assertion or executes a real test runner.

    Parse Python ``-c`` payloads so ``python -c \"print('assert')\"`` cannot satisfy Required merely
    because an assertion-shaped word appeared inside a string literal.
    """
    c = str(command or "")
    # An assertion only proves anything if its own failure controls the tool's exit status. This
    # also covers hand-written ``python -c 'assert ...'`` reproductions, not just test runners.
    if _has_unsafe_test_shell_control(c):
        return False
    # A recognized runner in discovery/build-only mode is explicitly non-evidence. Check this
    # before the broader regex below, whose runner alternative intentionally shares the syntax.
    if _TEST_RUNNER_RE.search(c):
        return _is_test_runner_cmd(c)
    heredoc = _split_first_heredoc(c)
    if heredoc is not None and _REPRO_STDIN_RE.search(heredoc[0]):
        try:
            tree = ast.parse(heredoc[1])
        except (SyntaxError, ValueError):
            return False
        return any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    try:
        words = shlex.split(c, posix=True)
    except ValueError:
        words = []
    for i, word in enumerate(words):
        exe = os.path.basename(word).lower().removesuffix(".exe")
        if not (exe == "py" or re.fullmatch(r"python\d*(?:\.\d+)?", exe)):
            continue
        for j in range(i + 1, min(len(words), i + 5)):
            if words[j] in ("&&", "||", ";", "|"):
                break
            if words[j] == "-c" and j + 1 < len(words):
                try:
                    tree = ast.parse(words[j + 1])
                except (SyntaxError, ValueError):
                    return False
                return any(isinstance(node, ast.Assert) for node in ast.walk(tree))
    return bool(_ASSERTED_RE.search(c))


def _budget_exceeded(model, total, subscription_only=False, limits=None):
    """True once the run has spent past the $ or token ceiling it is authorized to spend.

    ``limits`` is the run's own frozen snapshot (``settings.RunLimits``), taken once when the
    run started or when its request was accepted. Passing it is how a Settings save stops being
    retroactive: this used to read COLLIE_MAX_COST / COLLIE_MAX_TOTAL_TOKENS at every call, so a
    cap typed for future work was applied to spend a run had already made. ``Harness._over_budget``
    supplies it at every call site inside the loop.

    Without one this still reads the environment, unchanged, for standalone callers that have no
    run to snapshot for (Pack's own aggregate accounting, an automation's subprocess env). 0 or
    unset means no limit either way.
    """
    if limits is not None:
        max_cost = max(0.0, float(getattr(limits, "max_cost", 0.0) or 0.0))
        max_tok = max(0, int(getattr(limits, "max_total_tokens", 0) or 0))
        if max_cost <= 0 and max_tok <= 0:
            return False
        return _spend_exceeded(model, total, subscription_only, max_cost, max_tok)
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
    return _spend_exceeded(model, total, subscription_only, max_cost, max_tok)


def _spend_exceeded(model, total, subscription_only, max_cost, max_tok) -> bool:
    """Has this run's accumulated usage crossed either ceiling? (0 = no ceiling.)"""
    tot = total.input_tokens + total.output_tokens + total.cache_read + total.cache_creation
    if max_tok > 0 and tot >= max_tok:
        return True
    if max_cost > 0 and not subscription_only:
        from .costs import cost_usd
        if cost_usd(model, total.input_tokens, total.output_tokens,
                    total.cache_read, total.cache_creation) >= max_cost:
            return True
    return False


def _is_repro_cmd(name, args):
    """A post-edit focused repro, compile check, or real test execution we can gate finish on.

    A command that merely mentions a runner/interpreter (``echo pytest``, ``command -v python``)
    remains non-evidence.

    ``run_in_env`` counts for the same reason ``bash`` does, and more so: it executes the command
    in the instance's REAL environment (deps installed, edits replayed — tools.py:588-680), which
    is the ONLY execution the SWE prompt accepts as proof ("a local check that 'passes' is
    meaningless", swe.py:534-544). Accepting bash alone made the mandated tool produce zero
    evidence, so a correct patch verified RED→GREEN in the container still finished as
    "verification required but no executed post-edit assertion passed". Nothing it PRINTS is
    trusted: ``_repro_failed`` reads the host-minted ``ExecReceipt`` the tool returns instead.
    """
    if name not in ("bash", "run_in_env"):
        return False
    c = args.get("command") or ""
    if _has_unsafe_test_shell_control(c):
        return False
    if _is_test_runner_cmd(c):
        return True
    if any(_shell_unquoted_at(c, match.start(2)) for match in _TEST_RUNNER_RE.finditer(c)):
        # A recognized runner in help/list/collection mode must not fall through
        # to the broader Python/compile-check patterns below.
        return False
    if any(b in c.lower() for b in ("pip ", "pip3 ", "python -m venv", "setup.py")):
        return False
    return (bool(_REPRO_RE.search(c)) or bool(_REPRO_STDIN_RE.search(c))
            or bool(_REPRO_OTHER_RE.search(c)))


def _repro_failed(output, name: str = "bash", command: str = "", receipt=None) -> bool:
    """Did a post-edit reproduction actually FAIL? Ground truth is the process exit code (the bash
    tool prefixes '[exit N]' for nonzero) or a tool-level ERROR — NOT a bare 'Traceback' substring.
    A passing repro can print 'Traceback' (testing error handling: a caught exception echoed via
    traceback.print_exc, or the word appearing in data) and still exit 0; reading that as failure
    made the finish-gate nag the model to 'fix' correct code it could never satisfy (the phantom
    failure that made a self-audit give up). Any real uncaught exception — including an
    AssertionError in assert-mode — exits nonzero, so the exit-code signal keeps assert-verify.

    ``run_in_env`` reports a DUAL execution (original code vs. the same command with the edits
    applied), which no single exit code can express, and its text is interleaved with output the
    model's own command wrote. Its verdict comes from ``_env_repro_failed`` reading the
    host-minted ``ExecReceipt`` (``receipt``), not from this string at all."""
    o = output if isinstance(output, str) else str(output)
    if name == "run_in_env":
        return _env_repro_failed(receipt)
    if (name == "bash" and _is_test_runner_cmd(command)
            and re.search(r"\s-m\s+unittest\b", command)
            and re.search(r"(?m)^\s*Ran 0 tests? in \S+", o)):
        # Older supported Python versions exit zero when discovery runs no tests
        # (3.14 already uses exit 5). This can only withhold verification;
        # a positive printed count never proves success.
        return True
    return o.startswith("ERROR") or o.startswith("[exit")


# How many distinct successful check commands to remember for reminder wording. Small on
# purpose: the reminder quotes ONE command, and a longer memory is just state to get wrong.
_HOST_CHECK_MEMORY = 3
# How much of a result to read when looking for the host's own outcome markers. They are
# written at the FRONT of the result (see tools.BashTool), so a bounded head is enough and a
# huge captured output cannot turn this into an unbounded scan.
_HOST_CHECK_HEAD_CHARS = 600
# Markers that mean "this did not finish", "this was not allowed to run" or "nobody can say
# what this did". Each one is text the HOST writes around a tool result, not text a command
# prints: `ERROR:`/`[exit N]`/`[WARNING:` come from BashTool, `DENIED:` from the permission
# path in Harness, `CANCELED: run stopped before execution` from the cancellation path that
# fills in results for calls that were never dispatched.
_HOST_CHECK_REJECT = (
    "error", "denied", "[exit", "canceled", "cancelled", "refused", "blocked",
    "[warning:", "timed out", "not allowed", "not permitted", "permission denied",
    "was not executed", "did not finish", "could not be confirmed",
)
# Output that says, in the runner's own words, that it collected nothing. A green exit from a
# runner that ran zero tests is the single most misleading thing that can be quoted back as
# "the check that already ran successfully", because it is simultaneously true and worthless.
_HOST_CHECK_ZERO_RE = re.compile(
    r"\bran\s+0\s+tests?\b|\bno\s+tests?\s+ran\b|\bcollected\s+0\s+items?\b"
    r"|\bno\s+tests?\s+(were\s+)?(found|collected|executed|to\s+run)\b"
    r"|\b0\s+tests?\s+(ran|collected|executed|found)\b|\bno\s+test\s+files\b",
    re.IGNORECASE)


def _host_observed_check(name, args, out) -> str:
    """A check command THIS host watched run to a successful exit, or ``""``.

    Used for reminder wording only, never as verification evidence: the whole reason the
    reminder fires is that the workspace changed after this ran.  It is deliberately as strict
    as the finish gate about what counts as an executed success, because a command that was
    denied, refused, cancelled, killed at its deadline, exit-masked by shell composition or
    told to collect nothing is not a check anybody should be invited to "re-run" as if it had
    worked.

    WHERE THE OUTCOME COMES FROM, precisely: the ``bash`` tool returns a plain ``str`` and mints
    NO ``ExecReceipt`` — only ``run_in_env`` does, and its receipt describes a dual red→green
    pair rather than "the suite is green".  So the only outcome channel bash actually supplies
    is the text the HOST prepends: ``[exit N]`` for a non-zero status, ``ERROR: ...`` for a
    timeout/cancel/launch failure, ``[WARNING: ...]`` for an effect it could not account for,
    and nothing at all on a clean zero exit.  This reads exactly that channel; it does not
    invent a receipt API bash does not have, and it does not parse a status back out of what
    the command itself printed.  Because "success" is therefore the ABSENCE of a marker, every
    result that is not plainly a finished bash run is refused:

      * ``bash`` only, and a real ``str`` result — ``None``, bytes or an object stands for a
        call that never produced host text, which is not an observation of anything.
      * a receipt, if one is ever attached, must belong to THIS tool and command, and must
        carry a zero status; a foreign receipt means this is not bash's own result.
      * ``_is_test_runner_cmd`` — rejects pipelines, ``|| true``, ``;`` chains, backgrounding
        and collect-only/list-only modes, exactly as the gate does.
      * no failure/interruption marker in the head, and no "ran 0 tests".

    Anything uncertain returns ``""``, which costs only the better wording: the caller falls
    back to static discovery, and the verify gate is not consulted here either way.
    """
    if name != "bash":
        return ""
    command = args.get("command") if isinstance(args, dict) else ""
    if not isinstance(command, str) or not command.strip():
        return ""
    if not _is_test_runner_cmd(command):
        return ""
    if not isinstance(out, str):
        return ""
    receipt = _tools.exec_receipt(out)
    if receipt is not None:
        # bash mints none today; if that ever changes, the host-minted status wins and a
        # receipt belonging to some other call is not this command's outcome.
        if (receipt.tool != name or receipt.command != command
                or receipt.dual or receipt.edit_rc not in (0, None)):
            return ""
    text = out.strip()
    if not text or text == "(no output)":
        # A runner that finished without printing a single line gives nothing to stand on.
        return ""
    head = text[:_HOST_CHECK_HEAD_CHARS].lower()
    if any(marker in head for marker in _HOST_CHECK_REJECT):
        return ""
    if _HOST_CHECK_ZERO_RE.search(text[:_HOST_CHECK_HEAD_CHARS]) or _HOST_CHECK_ZERO_RE.search(
            text[-_HOST_CHECK_HEAD_CHARS:]):
        return ""
    return command.strip()


def _bound_receipt(out, tc, run_args):
    """The execution receipt belonging to THIS dispatched call, or None.

    Lifted off the tool's return value at the Harness execution boundary, then bound: same tool,
    same provider-authored call id, same command string that was actually handed to ``run()``. A
    receipt that reached here on some other call's result — a forwarded inner RPC result, an object
    held over from an earlier verification before a later edit — does not describe this call and is
    discarded rather than credited.
    """
    r = _tools.exec_receipt(out)
    if r is None:
        return None
    if r.tool != tc.name or r.call_id != str(getattr(tc, "id", "") or ""):
        return None
    want = run_args.get("command") if isinstance(run_args, dict) else None
    if r.command != (want if isinstance(want, str) else ""):
        return None
    return r


def _env_repro_failed(receipt) -> bool:
    """Read run_in_env's HOST-MINTED receipt. ONLY a complete red→green dual execution passes.

    The two exit codes come off ``subprocess.CompletedProcess`` inside the tool and travel on the
    result object (tools.ExecReceipt); nothing here reads the tool's printed text. The first
    version of this check parsed ``--- ORIGINAL code [exit N] --- … --- WITH YOUR EDITS [exit M]``
    out of that text — bytes the executed command can print itself. With a real base_rc=1 /
    edit_rc=1 (a still-failing edit), a baseline stdout containing a forged
    ``--- WITH YOUR EDITS [exit 0] ---`` line paired the real ORIGINAL header with the forged one
    and verified the broken fix. Command-controlled stdout is never the signal.

    Fail-closed in every other case:
      * no receipt at all — a tool-level ERROR, an unconfigured tool, a result that is just prose
        claiming "RED→GREEN"/"all tests passed";
      * a single-run receipt (``dual`` False): no baseline half means nothing to compare, so there
        is no red/green evidence regardless of what the one run exited;
      * ``base_rc == 0``: the check ALSO passes on the original code, so it reproduces nothing and
        validates nothing (the false green tools.py:665-669 warns about);
      * ``edit_rc != 0``: still failing, or a regression.
    """
    if not isinstance(receipt, _tools.ExecReceipt) or receipt.dual is not True:
        return True
    base_rc, edit_rc = receipt.base_rc, receipt.edit_rc
    if type(base_rc) is not int or type(edit_rc) is not int:
        return True
    return not (base_rc != 0 and edit_rc == 0)

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

# A real capped run returned its next read_file call as prose during no-tools synthesis.
# Tell the model that execution ended; merely removing the schemas leaves that ambiguous.
def final_summary_instruction(turns_exhausted: bool, turn_cap: int = 0) -> str:
    """Explain the final no-tools turn without inventing an unobserved stop reason."""
    if turns_exhausted and turn_cap:
        why = ("You have used all %d turns allowed for this task, so the host ended execution "
               "here." % turn_cap)
    elif turns_exhausted:
        why = "The host ended execution here because this task's turn limit was reached."
    else:
        # This also covers a voluntary but empty answer, not just a host guard.
        why = "Execution has ended without a usable final answer."
    return (
        "HOST NOTICE — EXECUTION HAS ENDED. %s\n\n"
        "This turn is the FINAL SUMMARY of the work already shown above, written for the person "
        "who asked. No tools are available on this turn and nothing you write will be executed: "
        "you cannot read, edit, search or run anything further, and any tool call you emit now is "
        "discarded, not run. Do not ask for permission, approval or another reply on this turn.\n\n"
        "Write a short report, grounded ONLY in the tool results above:\n"
        "- what you actually accomplished;\n"
        "- what remains unfinished, including the step you were about to take.\n\n"
        "Do not claim the task is complete. Only mention checks, tests or verification supported "
        "by tool results. Do not claim this conversation or final report has been saved or "
        "delivered; persistence happens after this response. You may describe files already "
        "written by recorded tools. If nothing useful was accomplished, say so plainly." % why)


_JUNK_UNTRACKED = ("__pycache__", ".pyc", "venv/", ".venv/", "node_modules/",
                   ".egg-info", ".dist-info", ".pytest_cache")


def _tree_diff(cwd, binary=True):
    """Net worktree diff vs HEAD (tracked files — the shape of a code fix). '' on non-git/error,
    which also disarms the whole guard: no snapshot -> no nudge -> no restore."""
    try:
        # windowless like every other spawn: this one runs on EVERY turn, so under pythonw it was
        # a console window per turn on top of one per shell command.
        from . import plat as _plat
        # Bytes, decoded here: _apply_diff may re-apply this, so it must come back exactly. Text
        # mode lost it -- one byte that is not UTF-8 made stdout None on Windows, and reading
        # folded CRLF to LF, so a CRLF file's diff no longer matched the file.
        # Pinned format, whatever the user's config says: color.diff=always, an external diff
        # tool or diff.noprefix all produced a "diff" git apply could not take back.
        # binary: what _apply_diff needs to restore a binary file; a reader (the critic, which
        # sees the first 9000 characters) is better off without the base85.
        r = subprocess.run(["git", "diff", "--no-color", "--no-ext-diff"]
                           + (["--binary"] if binary else [])
                           + ["--src-prefix=a/", "--dst-prefix=b/", "HEAD"],
                           cwd=cwd, capture_output=True, timeout=30,
                           **_plat.no_window_kwargs())
        return r.stdout.decode("utf-8", "surrogateescape") if r.returncode == 0 else ""
    except Exception:
        return ""


def _tree_empty(cwd):
    """True when the worktree holds NO net change: no tracked diff and no non-junk untracked
    file (a new-file fix is a real change — never nudge/restore over one)."""
    if _tree_diff(cwd).strip():
        return False
    try:
        from . import plat as _plat
        r = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=cwd,
                           capture_output=True, encoding="utf-8", errors="replace", timeout=30,
                           **_plat.no_window_kwargs())
        return not [p for p in r.stdout.splitlines()
                    if p.strip() and not any(j in p for j in _JUNK_UNTRACKED)]
    except Exception:
        return True


def _apply_diff(cwd, diff):
    """Re-apply a captured diff; --3way fallback for drifted context. True on success."""
    # As bytes: in text mode Windows wrote every "\n" as "\r\n", so no patch of an LF file ever
    # applied there (and the rollback reported FAILED).
    patch = diff.encode("utf-8", "surrogateescape")
    for extra in ([], ["--3way"]):
        try:
            from . import plat as _plat
            r = subprocess.run(["git", "apply", "--whitespace=nowarn"] + extra, cwd=cwd,
                               input=patch, capture_output=True, timeout=60,
                               **_plat.no_window_kwargs())
            if r.returncode == 0:
                return True
        except Exception:
            pass
    return False


class Harness:
    def __init__(self, provider: ModelProvider, memory, registry: ToolRegistry,
                 composer: ContextComposer, recorder: Recorder,
                 cwd: str, project: str = "global", mode: str = "act",
                 max_turns: int = 0, self_verify: bool = True,
                 force_edit: bool = False, limits=None):
        self.provider = provider
        self.memory = memory
        self.registry = registry
        self.composer = composer
        self.recorder = recorder
        self.cwd = cwd
        self.project = project
        self.mode = mode
        self.max_turns = max_turns
        # The $/token ceilings this harness is authorized to spend, frozen (settings.RunLimits).
        # None means "this run takes its own snapshot when it starts", which is what every
        # ordinary caller wants: the panel's value at the moment work began, held steady for the
        # whole run, and re-read for the next one. A surface replaying a durable request supplies
        # the snapshot taken when the person accepted it instead, so a cap moved while the
        # request waited neither loosens nor tightens what was agreed.
        self.limits = limits
        self._active_limits = None       # the snapshot in force for the run currently executing
        # Optional hard ceiling for physical provider requests in this Harness
        # run. Mission code slices set it from their outer durable budget;
        # ordinary interactive runs leave it unlimited (zero).
        self.max_model_calls = 0
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
        # One correction per malformed response episode, separate from transport retries.
        # A valid response restores this allowance; actual calls and tokens still consume
        # the run's shared budget. Tests/embedders may lower it to zero.
        self.max_contract_repairs = 1
        # context-overflow recovery (point 9): on an input-too-long error, shrink the history once
        # and retry the turn. COLLIE_OVERFLOW_RECOVERY=0 restores the old die-on-overflow behavior.
        self.overflow_recovery = _settings.get("OVERFLOW_RECOVERY", "1") not in ("0", "false", "off")
        # Semantic compaction of a long conversation (compaction.py). Same user-facing switch as
        # above — OVERFLOW_RECOVERY is the "auto-compact and retry" toggle the Settings panel
        # already describes — plus its own thresholds. Set to None to disable it alone.
        self.compaction = _compaction.CompactionPolicy.from_settings()
        # optional NDJSON event sink for streaming UX (CLI --stream-json, an editor extension,
        # or the ACP adapter). Default None = zero cost, no behavior change. Set h.emit = fn.
        self.emit = None
        # optional mid-run steering: a callable -> list[str] of user messages typed while the run is
        # in flight (point 13). Interactive surfaces (TUI) set it; None = zero cost, benchmark path
        # byte-identical. Drained only at safe points (turn start / voluntary finish).
        # Volatile by construction: whatever it returns was never on disk, so it
        # remains for embedders that have no durable conversation. Surfaces with
        # one use the inbox below instead, and MUST NOT wire both to the same
        # text — this loop appends what the callback returns, and appends the
        # inbox entry it claims, so a shared source would be inserted twice.
        self.steering = None
        # The durable half of the same idea. `run_owner` is a session_owner lease
        # a surface already holds (it took it BEFORE reading the journal and keeps
        # it through its own final save); run() validates and never releases it.
        # With no lease supplied and a durable session id, run() takes one for
        # itself — a direct embedder still must not be the second executor on a
        # conversation somebody else is running.
        self.run_owner = None
        # A task_inbox entry a surface claimed and is handing over as THIS run's
        # initial request. The loop stamps it onto the first user message (it does
        # not insert the text a second time) and acknowledges it only once that
        # message is durable.
        self.input_entry = None
        # The chronology boundary for mid-run steering: only input accepted ABOVE
        # this sequence belongs to this run. A surface that takes execution
        # ownership before setting up a provider captures it there (nothing
        # accepted during that gap is then lost); left None, the run captures its
        # own floor under the lease, before any work.
        self.steering_after_seq = None
        self._lease = None               # the lease in force for the current run
        self._input_entry = None         # the STORE's copy, validated under the lease
        self._steer_floor = 0
        self._input_failures = []        # durable-input failures, reported on the result
        # Cooperative cancellation owned by an embedding surface. It is deliberately a callback,
        # not a transport type: the web server uses an Event, while CLI/editor callers can use any
        # durable flag. Checked before every model turn and every individual tool execution.
        self.cancelled = None
        # Pack may attach one aggregate budget shared by all candidate harnesses.  It receives every
        # provider usage record exactly once and can stop later candidates/turns when the ONE user
        # run reaches its cap. None preserves the standalone/legacy path.
        self.shared_budget = None
        self.checkpoint_scope = ""       # surfaces may narrow undo below the shared memory project
        # Hosts that will run a stronger out-of-process verifier set this before run(). It prevents
        # a brief/incorrect durable promotion between the in-loop repro and that final verdict.
        self.defer_memory_promotion = False
        # When a surface supplies its durable conversation id, persist the full
        # transcript at every model/tool boundary. This makes a killed process
        # resumable without pretending an interrupted external tool never fired.
        self.durable_session_id = ""
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
        # Deterministic lifecycle policy. Project hooks are discovered only for a
        # workspace explicitly trusted with ``collie trust``; user hooks remain
        # available everywhere. Embedders/tests may replace this manager.
        self.hooks = HookManager(cwd)

    def resolve_limits(self):
        """The ceilings this run will be held to, decided ONCE before the first request.

        Order: what the caller froze for this harness, then what the parent of a delegated run
        authorized, then the values configured right now. The middle step is what keeps a child
        honest — it already shares the parent's ledger through ``shared_budget``, so it must be
        measured against the same ceilings the parent started under, not against a panel value
        that moved while the parent was working. Pack's aggregate budget carries no ceilings of
        its own and falls through to the third case, exactly as before.
        """
        limits = getattr(self, "limits", None)
        if limits is None:
            limits = getattr(getattr(self, "shared_budget", None), "limits", None)
        if limits is None:
            limits = _settings.current_limits()
        return limits

    def _over_budget(self, total) -> bool:
        """Has this run spent past the ceiling it started under?"""
        return _budget_exceeded(
            self.provider.model, total,
            bool(getattr(self.provider, "subscription_only", False)),
            limits=self._active_limits)

    def _emit(self, kind, **data):
        if self.emit:
            try:
                # Events fan out to SSE, mirrors, notifications and sometimes
                # durable receipts.  Sanitize the complete nested payload at
                # this common boundary so an exception or custom hook cannot
                # bypass the tool-output-specific redaction path.
                safe = _redact.redact_obj(
                    data, getattr(self, "_secret_vault", {}))
                self.emit(kind, safe)
            except Exception:
                pass

    def _hook(self, event, payload=None, subject=""):
        """Dispatch one lifecycle event and mirror bounded receipts to surfaces."""
        manager = getattr(self, "hooks", None)
        if manager is None:
            return None
        if event == "SessionStart":
            for pending in getattr(manager, "pending", ()):
                self._emit("hook_pending", path=pending.get("path", ""),
                           sha256=pending.get("sha256", ""))
        try:
            result = manager.dispatch(event, payload or {}, subject=subject)
        except Exception as exc:
            # A policy dispatcher itself failing at an authority boundary must
            # fail closed. HookResult is imported lazily to keep this helper tiny.
            from .hooks import HookResult
            result = HookResult(allowed=event not in (
                "UserPromptSubmit", "PreToolUse", "PermissionRequest", "Stop", "TaskCompleted"),
                reason="hook dispatcher failed: %s: %s" % (type(exc).__name__, exc))
        for receipt in result.receipts:
            self._emit("hook", hook_event=event,
                       source=receipt.get("source", ""),
                       allowed=bool(receipt.get("allowed", True)),
                       reason=receipt.get("reason", ""),
                       wall_ms=receipt.get("wall_ms", 0),
                       timed_out=bool(receipt.get("timed_out", False)))
        return result

    def _durable_session_id(self) -> str:
        """The durable conversation id this run persists to, or "" when it has none."""
        sid = getattr(self, "durable_session_id", "")
        if not sid:
            scope = getattr(self, "checkpoint_scope", "") or ""
            if scope.startswith(("web:", "session:")):
                sid = scope.split(":", 1)[1]
        return sid or ""

    def _session_checkpoint(self, messages, run_id, turn, state, detail=None,
                            terminal=False):
        sid = self._durable_session_id()
        if not sid:
            return True
        try:
            from . import sessions as _sessions
            _sessions.checkpoint(
                sid, messages, project=self.project, cwd=self.cwd,
                run_id=run_id, turn=turn, state=state, detail=detail,
                terminal=terminal)
            self._emit("session_checkpoint", session=sid, state=state,
                       turn=turn, terminal=terminal)
            return True
        except Exception as exc:
            # Model-only work can still report this failure. A caller about to execute a tool uses
            # the False return to fail closed rather than voiding the crash-recovery promise.
            self._emit("session_checkpoint", session=sid, state=state,
                       turn=turn, ok=False,
                       error="%s: %s" % (type(exc).__name__, exc))
            return False

    def _authorize(self, tc, tool, still_active=None):
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
        if still_active is not None and not still_active():
            return "parent execute_code invocation is no longer active"
        try:
            d = self.gate.evaluate(tc.name, tc.args, tool)
        except Exception as e:
            # A broken gate must not become an open gate.
            return "the permission gate failed (%s: %s)" % (type(e).__name__, e)

        if d.allowed:
            if d.rule:
                self._emit("gate", name=tc.name, decision="allowed", rule=d.rule,
                           risk=d.risk, effect=d.effect, action=d.action,
                           authorization_basis=d.authorization_basis,
                           notify=d.notify)
            # Consequential AND unprompted is the case the audit exists for: the row has to
            # be able to answer "why was I not asked about that?". Reads are not recorded —
            # they have no side effect to account for, and drowning the log in them is how
            # an audit trail stops being read.
            if d.risk != "read" and not self._audit(
                    tc, d, stage="auto", outcome="allowed"):
                reason = "the audit ledger is unavailable; consequential action was not executed"
                self._emit("gate", name=tc.name, decision="denied", reason=reason, risk=d.risk)
                return reason
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
                   target=d.target, reason=d.reason, rule_offer=d.rule_offer,
                   effect=d.effect, action=d.action,
                   grant_options=list(d.grant_options or ()))
        try:
            outcome = self.approve(tc.name, tc.args, d)
        except Exception as e:
            return "could not ask for approval (%s: %s)" % (type(e).__name__, e)

        # An execute_code subprocess may have timed out while an approval UI was parked. A late
        # Allow must neither execute the stale call nor mint a standing rule/audit receipt for it.
        if still_active is not None and not still_active():
            self._emit("gate", name=tc.name, decision="denied", risk=d.risk,
                       reason="parent execute_code invocation ended before approval returned")
            return "parent execute_code invocation is no longer active"

        from .gate import ALLOWING, Outcome
        try:
            # TTY/Inbox approvers return the enum itself; API-style embedders often
            # return its string value.  Accept both without stringifying an Enum to
            # ``Outcome.ALLOW_ONCE`` (which is not one of the wire values).
            outcome = outcome if isinstance(outcome, Outcome) else Outcome(str(outcome))
        except ValueError:
            outcome = Outcome.REJECT_ONCE     # an unparseable answer is not consent
        allowed = outcome in ALLOWING
        audit_ok = self._audit(tc, d, stage="approved" if allowed else "denied",
                               outcome=outcome.value, reason="answered by the user")
        if allowed and not audit_ok:
            reason = "the audit ledger is unavailable; approved action was not executed"
            self._emit("gate", name=tc.name, decision="denied", reason=reason,
                       risk=d.risk, target=d.target)
            return reason
        try:
            self.gate.apply_outcome(outcome, tc.name, d.target, decision=d)
        except Exception as exc:
            reason = "the permission decision could not be persisted (%s: %s)" % (
                type(exc).__name__, exc)
            self._emit("gate", name=tc.name, decision="denied", reason=reason,
                       risk=d.risk, target=d.target)
            return reason
        self._emit("gate", name=tc.name, decision="approved" if allowed else "denied",
                   outcome=outcome.value, risk=d.risk, target=d.target)
        return None if allowed else "the user declined this action"

    def _audit(self, tc, decision, *, stage, outcome, reason=None):
        """Record one gate decision and report whether a configured ledger accepted it.

        A run with no audit db (a test, a read-only embedder) is unaffected. Once a surface attaches
        an audit ledger, however, its failure is load-bearing for consequential actions: permission
        without a durable receipt is not auditable authority. Args remain pre-secret-restore.
        """
        if self.audit is None:
            return True
        try:
            self.audit.record(
                session=getattr(self, "_audit_session", "") or self.project,
                cwd=self.cwd, tool=tc.name, risk=decision.risk,
                target=decision.target or "", stage=stage, outcome=outcome,
                reason=reason if reason is not None else decision.reason,
                rule=decision.rule, args=tc.args,
                effect=getattr(decision, "effect", ""),
                action=getattr(decision, "action", ""),
                authorization_basis=getattr(decision, "authorization_basis", ""))
            return True
        except Exception:
            return False

    def _drain_steering(self):
        """Pull any queued mid-run user messages (point 13). Same exception discipline as _emit —
        a broken callback must never crash the run."""
        if not self.steering:
            return []
        try:
            return [s.strip() for s in (self.steering() or []) if isinstance(s, str) and s.strip()]
        except Exception:
            return []

    # ---- durable input (task_inbox) -------------------------------------------------
    def _inbox_ready(self):
        """The (session, lease) this run may read and write durable input under, or None.

        Both halves are required and neither is assumed: a benchmark harness has
        no session, and a run whose lease was refused never got here at all.
        """
        sid = self._durable_session_id()
        lease = self._lease
        if not sid or lease is None:
            return None
        return sid, lease

    def _inbox_failed(self, action, exc, **data):
        """Report a durable-storage failure. The one thing we never do is hide it.

        The volatile steer queue swallowed exceptions and answered "queued" to a
        person whose instruction had just been dropped. Every failure here is
        emitted with the action that failed AND kept on the result, so a surface
        that renders no events still ends up holding the fact.
        """
        detail = exc if isinstance(exc, str) else "%s: %s" % (type(exc).__name__, exc)
        self._input_failures.append(
            {"action": action, "id": str(data.get("id") or ""), "error": detail})
        self._emit("inbox", action=action, ok=False, error=detail, **data)

    def _inbox_reconcile(self):
        """Resolve a previous executor's crash from the JOURNAL, never from memory.

        Uncheckpointed in-memory messages are not proof of anything: settling a
        claim from them marks an accepted request consumed with no transcript row
        behind it, which is the one loss no later reconcile can detect. Returns
        an error string when durable state could not be established at all — the
        caller stops, because "we cannot tell" must not be spent as "there was
        nothing waiting".
        """
        ready = self._inbox_ready()
        if ready is None:
            return ""
        sid, lease = ready
        try:
            result = _ownership.reconcile(sid, lease)
        except Exception as exc:
            self._inbox_failed("reconcile", exc, session=sid)
            return ("durable input for %s could not be reconciled, so this run "
                    "cannot tell which accepted requests are still waiting: %s: %s"
                    % (sid, type(exc).__name__, exc))
        if result["consumed"] or result["released"] or result["conflicts"]:
            self._emit("inbox", action="reconcile", ok=True, session=sid,
                       consumed=list(result["consumed"]),
                       released=list(result["released"]),
                       conflicts=list(result["conflicts"]))
        return ""

    def _prepare_durable_input(self, user_msg, authority_msg):
        """Establish this run's durable input state before it touches anything.

        Three things, in this order and all before the first journal write, the
        first hook and the first provider call:

        * settle the previous executor's crash window, so an entry whose message
          is already in the transcript is never handed to the model again;
        * validate the claimed initial request against the STORE — still claimed,
          claimed by this lease, same payload, and not already delivered — rather
          than trusting the dict a surface passed in;
        * record the sequence floor above which mid-run steering belongs to this
          run, so an instruction accepted for an earlier (perhaps canceled) run
          cannot arrive after, and override, the newer one just sent.

        Returns an error string; non-empty means refuse the run.
        """
        self._input_entry = None
        self._steer_floor = 0
        ready = self._inbox_ready()
        supplied = getattr(self, "input_entry", None)
        if ready is None:
            if supplied is not None:
                return ("input_entry was supplied for a run with no durable "
                        "session and no execution lease")
            return ""
        sid, lease = ready
        failure = self._inbox_reconcile()
        if failure:
            return failure
        stored = None
        if supplied is not None:
            try:
                stored = _ownership.claimed_entry(sid, lease, supplied)
                _ownership.initial_request_content(
                    sid, stored, content=user_msg,
                    authority=authority_msg if isinstance(authority_msg, str) else "")
            except Exception as exc:
                self._inbox_failed("input_entry", exc, session=sid,
                                   id=(supplied or {}).get("id", "")
                                   if isinstance(supplied, dict) else "")
                return "%s" % exc
            self._input_entry = stored
        floor = getattr(self, "steering_after_seq", None)
        if floor is None:
            # No surface-supplied boundary: take one now, under the lease and
            # before any work, so a steer accepted during a slow provider setup
            # still counts as belonging to this run.
            try:
                floor = (stored["seq"] if stored is not None
                         else _ownership.sequence_floor(sid, lease))
            except Exception as exc:
                self._inbox_failed("sequence_floor", exc, session=sid)
                return ("durable input for %s could not be read, so this run cannot "
                        "tell which instructions are new: %s: %s"
                        % (sid, type(exc).__name__, exc))
        if isinstance(floor, bool) or not isinstance(floor, int) or floor < 0:
            return "steering_after_seq must be a non-negative integer, not %r" % (floor,)
        self._steer_floor = floor
        return ""

    def _stamp_input_entry(self, message, entry):
        """Mark the initial user message as the delivery of this accepted entry.

        The text is NOT inserted again: the surface already passed it as
        ``user_msg`` (expanded with attachments) and ``authority_msg`` (verbatim).
        What is added is identity — ``inbox_id`` is the only name ``reconcile``
        can find this message by after a crash — and the tags that keep it the
        person's own instruction rather than harness chatter.
        """
        message.update(source="user", kind=entry.get("mode") or "steer",
                       inbox_id=entry.get("message_id") or entry["id"])
        return message

    def _ack_input_entry(self, entry, checkpointed):
        """Acknowledge the initial entry — after its message is durable, never before.

        A failed checkpoint means the transcript on disk does not contain the
        instruction yet, so acknowledging it would be a lie the next run cannot
        detect. Leaving it claimed is recoverable: the end-of-run settle and the
        next run's reconcile both decide it from the journal.
        """
        ready = self._inbox_ready()
        if entry is None or ready is None:
            return False
        sid, lease = ready
        if checkpointed is False:
            self._inbox_failed(
                "ack", "the transcript containing this request could not be "
                       "persisted; it stays claimed for recovery",
                session=sid, id=entry["id"])
            return False
        try:
            from . import task_inbox as _inbox
            _inbox.ack(sid, lease, entry["id"],
                       message_id=entry.get("message_id") or entry["id"])
        except Exception as exc:
            # The message is durable, so this is a bookkeeping failure, not a lost
            # request: report it and let reconcile settle the entry from the
            # journal it is already in. It never re-delivers on a later turn.
            self._inbox_failed("ack", exc, session=sid, id=entry["id"])
            self._inbox_reconcile()
            return False
        self._emit("inbox", action="ack", ok=True, session=sid, id=entry["id"],
                   state="consumed")
        return True

    def _consume_durable_steering(self, session, res, rid, turn, prelude=None):
        """Adopt durable steer input at a safe model boundary.

        The order is the whole contract: claim (so no other executor can take the
        same entry), append ONE journal message per entry, checkpoint, then
        acknowledge. Reversing the last two would leave the inbox claiming a
        delivery no transcript contains.

        Only entries accepted above this run's sequence floor are taken: an
        instruction typed at an earlier, perhaps canceled, run is not an amendment
        to the request the person has just sent, and appending it afterwards would
        let the older text override the newer one.

        Returns (count, error). A non-empty error is fatal to the run: it means an
        accepted request could not be delivered as accepted — or that we cannot
        tell whether one exists — and the honest response is to stop at this
        boundary, before any further provider or tool work, leaving the input
        visible for correction rather than sending the model a different request.
        """
        ready = self._inbox_ready()
        if ready is None:
            return 0, ""
        sid, lease = ready
        try:
            entries = _ownership.claim_steer(sid, lease,
                                             after_seq=getattr(self, "_steer_floor", 0))
        except Exception as exc:
            # A torn store or a lost lease. Nothing was claimed, so nothing is
            # lost — but a correction the person has already sent may be sitting
            # in there unreadable, and continuing would answer the older request
            # as though they had never sent it.
            self._inbox_failed("claim", exc, session=sid)
            return 0, ("durable input for %s could not be read at this boundary, so "
                       "this run cannot tell whether a correction is waiting: %s: %s"
                       % (sid, type(exc).__name__, exc))
        if not entries:
            return 0, ""
        _redact_on = getattr(self, "_redact_on", True)
        appended = 0
        for index, entry in enumerate(entries):
            try:
                content = _ownership.entry_content(sid, entry)
            except Exception as exc:
                # Attachments that cannot be read back. Return every entry in this
                # batch (including this one) to pending so the person can fix or
                # cancel it, and stop the run.
                self._inbox_failed("attachments", exc, session=sid, id=entry["id"])
                try:
                    _ownership.release(sid, lease,
                                       entry_ids=[e["id"] for e in entries[index:]],
                                       reason="attachments unreadable")
                except Exception as release_exc:
                    self._inbox_failed("release", release_exc, session=sid)
                return appended, ("accepted input %s could not be delivered as "
                                  "accepted: %s" % (entry["id"], exc))
            safe_content = (_redact.redact_obj(content, self._secret_vault)
                            if _redact_on else content)
            safe_text = (_redact.redact(entry["text"], self._secret_vault)
                         if _redact_on else entry["text"])
            from . import task_inbox as _inbox
            message = _inbox.journal_message(entry)
            message["content"] = safe_content
            if prelude is not None and appended == 0:
                session["messages"].append(prelude)
            session["messages"].append(message)
            appended += 1
            # Authority comes from the words the person wrote, never from the
            # project files or images the surface attached around them.
            if self.gate is not None and hasattr(self.gate, "extend_request"):
                self.gate.extend_request(safe_text)
            checkpointed = self._session_checkpoint(
                session["messages"], rid, turn, "turn_boundary",
                {"inbox_id": entry["id"]})
            if checkpointed is False:
                # The transcript on disk does not contain this instruction, so the
                # entry stays claimed and NO consumed event is emitted: a green
                # "delivered" line under a failed write is exactly the false
                # acknowledgement this whole stack exists to end. Stop here,
                # before the next provider or tool boundary.
                self._inbox_failed(
                    "checkpoint", "the transcript containing this request could not "
                    "be persisted; it stays claimed for recovery",
                    session=sid, id=entry["id"])
                return appended, ("accepted input %s could not be recorded in the "
                                  "transcript at this boundary, so this run stopped "
                                  "before acting on it" % entry["id"])
            try:
                _inbox.ack(sid, lease, entry["id"],
                           message_id=entry.get("message_id") or entry["id"])
            except Exception as exc:
                # The message IS durable; the record of it is not. Settle what the
                # journal proves, then stop: whatever broke the inbox write is
                # equally able to hide the person's next correction.
                self._inbox_failed("ack", exc, session=sid, id=entry["id"])
                self._inbox_reconcile()
                return appended, ("accepted input %s was delivered but could not be "
                                  "recorded as delivered: %s" % (entry["id"], exc))
            res.steer_count += 1
            self._emit("steer", session=sid, id=entry["id"], text=safe_text[:200],
                       state="consumed")
            self.recorder.log_turn(rid, turn, "steer", safe_text[:500], 0, 0, 0, 0)
        return appended, ""

    def _settle_durable_input(self, res=None):
        """Settle every accepted instruction this run touched, from the journal.

        Cancel, error, budget stop, a blocking lifecycle hook and a clean finish
        all owe the same thing — but "release everything I claimed" is not it. An
        entry whose message reached the journal one line before the end IS
        delivered, and reopening it would let a person edit or re-send an
        instruction the model already has. So reconcile against the transcript
        first and hand back only the remainder; if the transcript cannot be read,
        hand back nothing and say so, because a claim left standing is
        recoverable and a claim wrongly reopened is not.
        """
        released = []
        ready = self._inbox_ready()
        if ready is not None:
            sid, lease = ready
            try:
                settled = _ownership.settle_and_release(sid, lease, reason="run ended")
            except Exception as exc:
                self._inbox_failed("settle", exc, session=sid)
            else:
                released = list(settled["released"])
                recovered = settled["reconciled"]
                if released or recovered["consumed"] or recovered["released"]:
                    self._emit("inbox", action="release", ok=True, session=sid,
                               released=released,
                               consumed=list(recovered["consumed"]),
                               recovered=list(recovered["released"]))
        if res is not None:
            # The result carries the failures too: a surface that renders no
            # events still has to be able to tell the person what did not happen.
            res.input_failures = list(self._input_failures)
        return released

    def _verification_context(self, messages):
        """Carry the last host-executed check's verdict into this model turn.

        The evidence exists only in ``run_receipts``, which nothing in a resumed
        conversation reads, so the next turn could not tell whether the project's
        tests had passed, failed or been stopped — and the model's own recollection
        of "I ran the tests" is not evidence about anything. Bounded to the newest
        receipt and deduplicated by digest, so a long thread gains at most one
        short host-authored message per distinct check.
        """
        ready = self._inbox_ready()
        if ready is None:
            return None
        sid, lease = ready
        try:
            row = _ownership.verification_row(sid, lease)
            if row is None or _ownership.already_projected(messages, row["digest"]):
                return None
            message = _ownership.context_message(row)
            if getattr(self, "_redact_on", True):
                # A recorded command can carry a credential (``--token …``). It
                # passes through the same vault as any other model-facing text,
                # so the receipt cannot become the one place a secret is quoted
                # back in full. The digest is computed from the stored row, so
                # deduplication is unaffected.
                message["content"] = _redact.redact(
                    message["content"], self._secret_vault)
        except Exception as exc:
            self._inbox_failed("verification_context", exc, session=sid)
            return None
        self._emit("verification_context", session=sid, outcome=row["outcome"],
                   command=row["command"], exit_code=row["exit_code"],
                   digest=row["digest"])
        return message

    def _cancel_requested(self):
        try:
            return bool(self.cancelled and self.cancelled())
        except Exception:
            return False

    def _close_unanswered_calls(self, messages, state, detail):
        """Give every unanswered tool_use an honest, protocol-valid result.

        A run that stops mid-batch leaves tool_use blocks with no paired result;
        providers reject that thread, so the next turn in this same process would
        fail on history the user cannot see or fix.  Closing them here says only
        what the host actually knows: the call that was RUNNING keeps its
        uncertainty, the calls that never started say they never started.  This
        is transcript hygiene, not reconciliation — the durable recovery fence is
        written separately and is not cleared by anything here.
        """
        pending = {}
        for msg in messages:
            if msg.get("role") == "assistant":
                for call in msg.get("tool_calls") or []:
                    cid = (call.get("id") if isinstance(call, dict)
                           else getattr(call, "id", None))
                    if cid:
                        pending[cid] = (call.get("name") if isinstance(call, dict)
                                        else getattr(call, "name", "")) or "tool"
            elif msg.get("role") == "tool":
                pending.pop(msg.get("tool_call_id"), None)
        if not pending:
            return 0
        from . import sessions as _sessions
        detail = detail if isinstance(detail, dict) else {}
        running = (detail.get("tool_call_id")
                   if state in ("executing_tool", "external_action") else None)
        safe_read = _sessions.replay_safe_boundary(state, detail)
        for cid, name in pending.items():
            if cid == running and safe_read:
                content = ("INTERRUPTED: this call stopped before returning a result. "
                           "It is host-attested as effect-free, so run it again if its "
                           "output is still needed.")
            elif cid == running:
                content = ("INTERRUPTED: this call stopped while it was running. Its effect "
                           "is UNKNOWN — check the outside world before requesting it again.")
            else:
                content = "CANCELED: run stopped before execution"
            messages.append({"role": "tool", "tool_call_id": cid,
                             "name": name, "content": content})
            self._emit("tool", name=name, args={}, ok=False, canceled=True,
                       result=content.split(":", 1)[0].lower())
        return len(pending)

    def _account_usage(self, total, usage, model=None):
        """Add one provider usage record to local totals and an optional Pack-wide budget."""
        total.add(usage)
        if self.shared_budget is not None:
            self.shared_budget.account(model or self.provider.model, usage)

    # ---- semantic compaction (compaction.py) ------------------------------------------
    def _compaction_policy(self):
        """The active policy, or None when compaction must not run at all.

        OVERFLOW_RECOVERY is the user-facing switch for exactly this behaviour ("auto-compact
        and retry the turn"), so turning it off keeps the pre-compaction loop verbatim.
        """
        policy = getattr(self, "compaction", None)
        if policy is None or not getattr(self, "overflow_recovery", True):
            return None
        return policy if getattr(policy, "enabled", False) else None

    def _restore_compaction(self, session):
        """Adopt a persisted checkpoint for a resumed thread, if it still fits the transcript.

        The fingerprint does the deciding: a fork, a hand-edited session file or a merged
        history simply fails to match and the run starts from the full transcript again.
        """
        if self._compaction_policy() is None:
            return
        sid = self._durable_session_id()
        if not sid:
            return
        stored = _compaction.load_checkpoint(sid)
        if stored is None:
            return
        valid = _compaction.validate_checkpoint(session.get("messages") or [], stored)
        if valid:
            session[_compaction.SESSION_KEY] = valid
            self._emit("compaction", status="restored", cutoff=valid["cutoff"],
                       generation=int(valid.get("generation") or 1),
                       kept=len(session.get("messages") or []) - valid["cutoff"])
        else:
            self._emit("compaction", status="ignored",
                       reason="stored checkpoint does not match this transcript")

    def _maybe_compact(self, session, system, msgs, total, rid, turn, model_calls,
                       reason="threshold"):
        """Spend at most ONE physical provider request folding old history into a summary.

        Returns None when no request was made, else ``(applied, request_count)`` — the caller
        adds the count to the run's model_calls whether or not the summary was usable, because
        the request was really issued and really billed. The count is the provider's own, so a
        summary the provider refused to issue at all (a denied request reservation) adds 0.
        """
        policy = self._compaction_policy()
        forced = bool(session.pop(_compaction.FORCE_KEY, False))
        if policy is None:
            return None
        messages = session.get("messages") or []
        if len(messages) < policy.min_messages:
            # Short task: not even the estimate is worth walking the thread. Say so when an
            # actual overflow asked for help, so "why did it not compact?" has an answer.
            if forced:
                self._emit("compaction", status="skipped", reason="short",
                           source_messages=len(messages))
            return None
        try:
            schemas = self.registry.active_schemas()
        except Exception:
            schemas = []
        before = _compaction.estimate_request(system, msgs, schemas)
        plan, why = _compaction.plan(
            messages, session.get(_compaction.SESSION_KEY),
            session.get(_compaction.GATE_KEY), policy=policy, total_tokens=before,
            force=forced, reason=reason)
        if plan is None:
            if forced:
                # A real overflow that compaction cannot help with is worth saying out loud;
                # an ordinary turn under the threshold is not (it happens every turn).
                self._emit("compaction", status="skipped", reason=why,
                           before_tokens=before, source_messages=len(messages))
            return None
        # Budget and cancellation are the caller's boundaries, not this feature's: a summary is
        # never worth crossing a ceiling the user set, and a cancelled run stops here too.
        if self._cancel_requested():
            return None
        call_cap = max(0, int(getattr(self, "max_model_calls", 0) or 0))
        if call_cap and model_calls >= call_cap:
            return None
        if self.shared_budget is not None and self.shared_budget.exceeded():
            return None
        if self._over_budget(total):
            return None
        # Build the digest BEFORE spending anything. It is bounded by whole messages and can
        # refuse (a user message too large to hand over complete, a span too small once it is
        # trimmed to fit); refusing here costs no request and leaves the full thread being sent,
        # which is the honest outcome — summarizing a span the summarizer only half saw is not.
        prepared = _compaction.prepare(messages, plan, policy)
        if not prepared.ok:
            self._emit("compaction", status="skipped", reason=prepared.reason,
                       before_tokens=before, cutoff=plan.cutoff,
                       source_messages=plan.source_messages)
            return None
        self._emit("compaction", status="started", reason=plan.reason,
                   before_tokens=before, cutoff=prepared.cutoff,
                   kept=plan.source_messages - prepared.cutoff,
                   source_messages=plan.source_messages,
                   user_messages=prepared.user_messages,
                   span_truncated=prepared.span_truncated)
        digest = prepared.digest
        started = time.time()
        try:
            # No tools (nothing may execute during a summary), no stream callback (the summary
            # is private context, not this run's answer), and this run's own provider only —
            # a summary produced by some other endpoint would not be the same conversation.
            from .cancellation import complete as complete_cancelable
            comp = complete_cancelable(
                self.provider, _compaction.SUMMARY_SYSTEM,
                [{"role": "user", "content": digest}], [], cancelled=self.cancelled)
        except Exception as exc:
            comp = _error_completion(getattr(self.provider, "name", "?"), exc)
        # From here every path returns (applied, requests): whatever the attempt physically cost
        # lands in the ledger, the token total and the shared budget whether the summary was
        # usable, malformed, refused or arrived after a cancellation. `issued_requests` keeps the
        # historical ceiling on a bogus count (beyond it, fall back to one) and the historical
        # default of one for an unreadable count, while letting a truthful 0 — the provider never
        # issued the summary request — stay 0.
        requests = issued_requests(comp, maximum=_compaction.MAX_SUMMARY_REQUESTS)
        self._account_usage(total, comp.usage)
        ok, summary, failure = _compaction.validate_summary(comp, policy)
        elapsed = int((time.time() - started) * 1000)
        if ok and self._cancel_requested():
            # Cancelled while the summary was in flight: pay for it, adopt nothing. The next
            # run re-plans from the transcript, which was never touched.
            self._emit("compaction", status="skipped", reason="canceled",
                       before_tokens=before, cutoff=prepared.cutoff)
            return False, requests
        checkpoint = _compaction.make_checkpoint(
            messages, plan, summary, policy, prepared) if ok else None
        if checkpoint is None:
            failure = failure or "checkpoint refused the summary"
            gate = _compaction.note_failure(session, messages)
            self.recorder.log_turn(
                rid, turn, "compaction", "compaction failed: %s (attempt %d)" % (
                    failure, gate["failures"]),
                comp.usage.input_tokens, comp.usage.output_tokens, 0, elapsed,
                cache_read=comp.usage.cache_read)
            self._emit("compaction", status="failed", reason=failure,
                       before_tokens=before, cutoff=prepared.cutoff,
                       failures=gate["failures"])
            return False, requests
        session[_compaction.SESSION_KEY] = checkpoint
        session[_compaction.GATE_KEY] = {}       # a success clears the failure cooldown
        # Run-scoped, on the session the caller owns — not on the Harness, which an embedder
        # may reuse for the next conversation.
        session[_compaction.PENDING_KEY] = (checkpoint, elapsed, comp.usage)
        return True, requests

    def _settle_compaction(self, session, system, msgs, rid, turn):
        """Measure what the compaction actually bought, then record and persist it."""
        pending = session.pop(_compaction.PENDING_KEY, None)
        if not pending:
            return
        checkpoint, elapsed, usage = pending
        try:
            schemas = self.registry.active_schemas()
        except Exception:
            schemas = []
        after = _compaction.estimate_request(system, msgs, schemas)
        policy = getattr(self, "compaction", None) or _compaction.CompactionPolicy()
        checkpoint = _compaction.record_projection(session, checkpoint, after, policy)
        before = int(checkpoint.get("before_tokens") or 0)
        # Persist beside the transcript so the next turn/process/resume reuses this summary
        # instead of paying for it again. A failure to write is not a failure to compact.
        sid = self._durable_session_id()
        persisted = _compaction.save_checkpoint(sid, checkpoint) if sid else False
        # The ledger line says what was summarized THIS time (the span, not the whole prefix —
        # generations 2+ only re-read the newest span, the rest is carried in the previous
        # summary) and how much recoverable payload was abbreviated inside it.
        self.recorder.log_turn(
            rid, turn, "compaction",
            "summarized messages %d-%d of %d (%d from the user, %d payload chars abbreviated); "
            "est %d -> %d tokens" % (
                checkpoint["summarized_from"], max(0, checkpoint["cutoff"] - 1),
                checkpoint["source_messages"], checkpoint["user_messages_summarized"],
                checkpoint["payload_chars_elided"], before, after),
            usage.input_tokens, usage.output_tokens, 0, elapsed,
            cache_read=usage.cache_read)
        self._emit("compaction", status="applied", reason=checkpoint.get("reason", ""),
                   before_tokens=before, after_tokens=after, cutoff=checkpoint["cutoff"],
                   kept=int(checkpoint["source_messages"]) - int(checkpoint["cutoff"]),
                   source_messages=checkpoint["source_messages"],
                   summarized_from=checkpoint["summarized_from"],
                   messages_summarized=checkpoint["messages_summarized"],
                   user_messages_summarized=checkpoint["user_messages_summarized"],
                   payload_chars_elided=checkpoint["payload_chars_elided"],
                   span_truncated=bool(checkpoint.get("span_truncated")),
                   generation=int(checkpoint.get("generation") or 1),
                   improved=bool(checkpoint.get("improved")), persisted=bool(persisted))

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
        self._critic_model = None
        self._critic_request_count = None
        try:
            reviewer = self.critic_provider or self.provider
            from .cancellation import complete as complete_cancelable
            comp = complete_cancelable(reviewer, sysp, [{"role": "user", "content": msg}],
                                       [], cancelled=self._cancel_requested)
            self._critic_usage = comp.usage   # the caller folds this into the run's token/$ total —
            self._critic_request_count = issued_requests(comp)
            # Lightweight/custom providers used by embedders are only required to implement
            # ``complete``.  Accounting metadata must not turn a successfully returned objection
            # into an exception and silently approve the candidate.
            self._critic_model = getattr(reviewer, "model", None)
            text = (comp.text or "").strip()   # a critic call spends real tokens; the receipt must show them
            if getattr(comp, "stop_reason", "") == "error":
                # The reviewer never produced a review (denied request reservation, transport
                # failure). Its error prose is NOT a finding: handing it back as one would spend
                # a repair round arguing with "ERROR(...): model request reservation denied".
                # Same fail-open as the exception path below; whatever it cost is still accounted.
                return True, ""
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

    def _verification_preflight(self, did_edit, edited_files, host_checks, last_edit_turn,
                                last_repro_turn, last_repro_failed, last_repro_asserted,
                                detect_cache, edit_generation=0) -> list:
        """Zero or one request-scoped message stating the CURRENT verification state.

        The finish gate below already refuses an unverified finish — but only once the model
        has composed the answer it is refusing, which in recorded runs meant a long answer, a
        response-contract repair and then a second long answer. This says the same thing on
        the request the loop is making anyway, right after an edit lands, so the model can do
        the check before it writes the expensive part. It spends no model call of its own.

        It is guidance, never evidence: the verdict below is computed from exactly the same
        accounting whether or not this fires, so a model that ignores the hint meets the
        unchanged gate. Three conditions keep it off the tasks it has no business on —
        ``self_verify`` (an explicitly disabled or externally-owned verification stays off),
        a landed edit (a read-only or question-answering run is never asked to run anything),
        and a verdict that is still missing or failed (a check that genuinely passes on the
        latest edit makes the hint disappear on the next turn, because it is recomputed from
        live state rather than remembered).
        """
        if not (self.self_verify and did_edit):
            return []
        if self._repro_verified(did_edit, last_edit_turn, last_repro_turn,
                                last_repro_failed, last_repro_asserted):
            return []
        fresh = last_repro_turn >= last_edit_turn
        command, source = "", ""
        # An explicit required-verification wording owns what counts here; naming a detected
        # command beside it would invite the wrong check (SWE's reproduction, not pytest).
        if not self.verify_nudge:
            # Same precedence as verify_nudge_for: a command this host watched succeed here
            # beats a marker file. Still only wording — its run predates the edit above.
            command = _reusable_check_command(host_checks, self.cwd)
            if command:
                source = "already ran successfully in this workspace"
            elif detect_cache.get("generation") == edit_generation:
                # Same workspace contents as when this was detected: no edit has landed
                # since, so re-walking the markers could only produce the same answer.
                command, source = detect_cache["command"], detect_cache["source"]
            else:
                try:
                    from .verification import detect_verification_commands
                    found = detect_verification_commands(str(self.cwd)) if self.cwd else []
                except Exception:
                    found = []
                # Detection touches the filesystem, so it is memoized — but keyed on the
                # count of edits that have LANDED, never on the run. The primary workflow
                # here is a project being created or reshaped: the first turn may edit a
                # README in a workspace with no runner at all, and the turn that adds
                # package.json (or renames the script a previous detection named) must not
                # keep being told there is nothing to run. An unreadable workspace simply
                # leaves the hint without a named command.
                #
                # Bounded: one detection per edit generation at most, and a generation only
                # advances on a write that actually landed — a rejected edit, a read, a
                # check run or a plain answer all reuse the memo. detection itself is the
                # non-recursive marker scan ``verify_nudge_for`` below already performs on
                # every post-edit turn without any memo at all.
                command, source = ((found[0]["command"], "detected from %s" % found[0]["source"])
                                   if found else ("", ""))
                detect_cache.update(generation=edit_generation, command=command, source=source)
        hint = _preflight.verification_state_hint(
            edited_paths=edited_files, check_ran_after_edit=fresh,
            check_failed=bool(fresh and last_repro_failed),
            check_inconclusive=bool(fresh and not last_repro_failed),
            command=command, command_source=source,
            required_override=bool(self.verify_nudge))
        return [hint] if hint else []

    def run(self, task_id: str, user_msg, consolidate: bool = True,
            history: list = None, authority_msg=None) -> RunResult:
        """Execute one run under exactly one execution lease for its session.

        The lease is the outermost thing this run does, because everything below
        it — reading the journal, reconciling the inbox, appending messages,
        writing the final checkpoint — is only safe while no second executor can
        be doing the same to the same conversation. A surface that already holds
        the lease passes it as ``run_owner`` and keeps it afterwards (its final
        save is still to come); a direct embedder with a durable session id gets
        this wrapper's own lease for the length of the call. A run with no durable
        session has nothing to serialize and takes nothing.
        """
        sid = self._durable_session_id()
        self._input_failures = []
        try:
            with _ownership.hold(sid, label="native-run",
                                 existing=getattr(self, "run_owner", None)) as lease:
                self._lease = lease
                res = None
                run_provider = self.provider
                original_max_tokens = getattr(run_provider, "max_tokens", None)
                try:
                    # Durable input state is established BEFORE the first hook, the
                    # first journal write and the first provider call, so a request
                    # that must not be executed is refused while the transcript is
                    # still untouched.
                    refusal = self._prepare_durable_input(user_msg, authority_msg)
                    res = (self._refusal(task_id, refusal, "input_refused") if refusal
                           else self._run(task_id, user_msg, consolidate=consolidate,
                                          history=history, authority_msg=authority_msg))
                finally:
                    # After the run's own final journal write and before the lease
                    # goes. Here rather than inside _run so that every ending — a
                    # blocking hook, an exception, a stop — settles the same way.
                    try:
                        self._settle_durable_input(res)
                    finally:
                        self._lease = None
                        # Output-truncation recovery borrows more room for this
                        # run. A reused provider must start the next task with
                        # its configured value, including after an exception.
                        if (original_max_tokens is not None and
                                getattr(run_provider, "max_tokens", None) != original_max_tokens):
                            run_provider.max_tokens = original_max_tokens
                return res
        except _ownership.OwnershipRefused as exc:
            res = self._refusal(task_id, str(exc), "ownership_refused")
            self._emit("ownership", ok=False, session=getattr(exc, "session", ""),
                       busy=bool(getattr(exc, "busy", False)), error=res.error)
            return res

    def _refusal(self, task_id, error, stop_reason) -> RunResult:
        """Refuse the run without touching the transcript it does not own.

        Nothing has been read or written at this point, which is the property
        that matters: neither a second executor nor a run holding a request it
        may not deliver appends one message, one checkpoint or one receipt.
        """
        res = RunResult(run_id=0, task_id=task_id, harness="collie",
                        model=getattr(self.provider, "model", ""),
                        provider=getattr(self.provider, "name", ""),
                        parent_run_id=getattr(self, "parent_run_id", None))
        res.error = error
        res.messages = []
        res.stop_reason = stop_reason
        res.success = False
        res.input_failures = list(self._input_failures)
        return res

    def _run(self, task_id: str, user_msg, consolidate: bool = True,
             history: list = None, authority_msg=None) -> RunResult:
        t0 = time.time()
        # One snapshot, before the first request, for every budget question this run will ask.
        # After this line nothing in the loop looks at COLLIE_MAX_COST/COLLIE_MAX_TOTAL_TOKENS
        # again, so a Settings save lands on the NEXT run instead of on the one in flight.
        self._active_limits = self.resolve_limits()
        # Redact before *any* model-facing or durable copy is made.  Previously
        # only tool output was protected, while a credential pasted in the user
        # prompt or carried by resumed history was checkpointed and sent raw.
        # The same in-memory vault still restores placeholders only at the tool
        # execution boundary, so key-using workflows continue to work.
        _redact_on = (_settings.get("REDACT_SECRETS", "on") or "on") not in (
            "off", "0", "false")
        self._redact_on = _redact_on     # durable input joins the same policy
        self._secret_vault = getattr(self, "_secret_vault", {})
        # Keep canonical multimodal blocks intact.  Turning a list into ``str``
        # protects neither its structure nor the image path: providers would see
        # Python repr text and the durable thread would permanently lose the
        # attachment.  ``redact_obj`` masks only nested strings.
        normalized_user_msg = (user_msg if isinstance(user_msg, (str, list))
                               else str(user_msg or ""))
        safe_user_msg = (_redact.redact_obj(normalized_user_msg, self._secret_vault)
                         if _redact_on else normalized_user_msg)
        # Some first-party surfaces frame an exact user command with untrusted window metadata for
        # the model. Only the separately authenticated, verbatim command may mint action authority;
        # the framing remains useful model context but can never expand the user's grant.
        normalized_authority = (authority_msg if isinstance(authority_msg, (str, list))
                                else str(authority_msg or ""))
        safe_authority_msg = (_redact.redact_obj(normalized_authority, self._secret_vault)
                              if _redact_on else normalized_authority)
        # The authenticated user's message is the only model-adjacent text allowed to
        # create Authority v2 grants. This happens before provider/tool output exists.
        if self.gate is not None and hasattr(self.gate, "begin_request"):
            self.gate.begin_request(safe_authority_msg or safe_user_msg,
                                    project=self.project, mission_id=task_id)
        rid = self.recorder.start_run(task_id, "collie", self.provider.model,
                                      self.provider.name, note="v" + __version__)
        res = RunResult(run_id=rid, task_id=task_id, harness="collie",
                        model=self.provider.model, provider=self.provider.name,
                        parent_run_id=getattr(self, "parent_run_id", None))
        ctx = ToolCtx(cwd=self.cwd, project=self.project, memory=self.memory,
                      recorder=self.recorder, registry=self.registry,
                      checkpoint_scope=self.checkpoint_scope,
                      # The surface's Stop, reachable from INSIDE a running tool. The loop's own
                      # cancellation checks sit between calls, so a tool that owns a subprocess was
                      # the one place Stop could not reach: the command kept running to its own
                      # deadline. Passing the bound method (not self.cancelled) keeps the late-bound
                      # lookup and the never-raises discipline of _cancel_requested.
                      cancelled=self._cancel_requested)
        if getattr(self, "capabilities", None) is not None:
            ctx.capabilities = dict(self.capabilities)
        self._hook("SessionStart", {
            "run_id": rid, "task_id": task_id, "project": self.project,
            "provider": self.provider.name, "model": self.provider.model,
        }, subject=self.project)
        submitted = self._hook("UserPromptSubmit", {
            "run_id": rid, "task_id": task_id, "prompt": safe_user_msg,
            "project": self.project,
        }, subject=self.project)
        if submitted is not None and not submitted.allowed:
            res.error = "prompt blocked by lifecycle hook: %s" % (
                submitted.reason or "policy rejected the prompt")
            res.wall_ms = int((time.time() - t0) * 1000)
            res.messages = [{"role": "user", "content": safe_user_msg}]
            self.recorder.finish_run(res)
            self._emit("receipt", verified=False, prefix_tokens=0,
                       input_tokens=0, output_tokens=0, total_tokens=0,
                       turns=0, tool_calls=0, wall_ms=res.wall_ms,
                       cost_usd=0.0, cache_waste_usd=0.0, cache_misses=0,
                       error=res.error, canceled=False)
            self._hook("SessionEnd", {"run_id": rid, "task_id": task_id,
                                      "error": res.error, "success": False},
                       subject=self.project)
            return res
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
                _cp = _ckpt.capture(
                    self.cwd, str(task_id), rid, content_text(safe_user_msg)[:60])
                res.checkpoint_ref = _cp.ref
                # ``kind`` is the event-name parameter of _emit(); using it for
                # checkpoint metadata raises before a success reaches the UI.
                self._emit("checkpoint", ok=True, ref=_cp.ref[:12],
                           checkpoint_kind=_cp.kind)
            else:
                self._emit("checkpoint", ok=False, reason=_why)
        except Exception as _ce:                 # never block the run on bookkeeping
            self._emit("checkpoint", ok=False, reason="%s: %s" % (type(_ce).__name__, _ce))
        # history (prior thread) lets a session CONTINUE across CLI calls / repl turns; the
        # composer's own elision keeps a long continued thread from bloating the prefix.
        msgs0 = list(history) if history else []
        if _redact_on:
            msgs0 = _redact.redact_obj(msgs0, self._secret_vault)
        submitted_context = (submitted.additional_context
                             if submitted is not None else [])
        prompt_content = safe_user_msg
        if submitted_context:
            context_text = "\n".join(submitted_context)
            if _redact_on:
                context_text = _redact.redact(context_text, self._secret_vault)
            context_block = "\n\n[Trusted lifecycle context]\n" + context_text
            if isinstance(prompt_content, list):
                prompt_content = list(prompt_content) + [
                    {"type": "text", "text": context_block}]
            else:
                prompt_content += context_block
        # What the host actually observed about the last check on this thread, in
        # front of the new request rather than lost in a receipt file nothing reads.
        verification_context = self._verification_context(msgs0)
        if verification_context is not None:
            msgs0.append(verification_context)
        prompt_message = {"role": "user", "content": prompt_content}
        # Validated against the store under this run's lease before anything was
        # written; never the dict the surface happened to pass in.
        input_entry = self._input_entry
        if input_entry is not None:
            self._stamp_input_entry(prompt_message, input_entry)
        msgs0.append(prompt_message)
        session = {"messages": msgs0}
        # A resumed thread may already carry a validated handoff summary; adopt it before the
        # first build so a continued long conversation does not re-summarize what it just did.
        self._restore_compaction(session)
        journal_state = "turn_boundary"
        journal_detail = {}
        # A failed pre-action journal write stops this run, including later inner RPCs.
        durability_fault = ""
        checkpointed = self._session_checkpoint(session["messages"], rid, 0, journal_state)
        # Acknowledge the accepted request only now — the transcript that contains
        # it is on disk, so "delivered" is a fact rather than an intention.
        initial_acked = self._ack_input_entry(input_entry, checkpointed)
        if checkpointed is False:
            # The same store outage is a host error mid-run and at the terminal save;
            # timing must not decide how a surface classifies it.
            from .recorder import note_host_error as _note_host_error
            _note_host_error(res, "the initial request could not be persisted; this run "
                                  "stopped before calling the model")
        elif input_entry is not None and not initial_acked:
            res.error = "the initial request could not be acknowledged; this run stopped before calling the model"
        # Tool output uses the same vault initialized before the prompt above.
        total = Usage()
        model_calls = 0
        consecutive_contract_repairs = 0
        if not getattr(self, "delegation_depth", 0):
            parent = self

            class DelegationBudget:
                def account(_budget, model, usage):
                    parent._account_usage(total, usage, model)

                def exceeded(_budget):
                    return (parent._cancel_requested() or
                            bool(parent.shared_budget and parent.shared_budget.exceeded()) or
                            bool(parent._over_budget(total)))

            # The child is measured against the ceilings its parent was authorized to spend, not
            # against whatever the panel says by the time it starts. run_child assigns this
            # object as the child's shared_budget, so resolve_limits finds it there.
            DelegationBudget.limits = self._active_limits

            def delegate_runner(task, limit):
                nonlocal model_calls
                from .delegate import DelegatedInterrupt, run_child
                budget = DelegationBudget()
                cap = max(0, int(self.max_model_calls or 0))
                if budget.exceeded() or (cap and model_calls >= cap):
                    return 'ERROR: parent run has no remaining delegation budget or was canceled'
                try:
                    child_result, payload = run_child(
                        self, task, limit, budget, max(0, cap - model_calls) if cap else 0,
                        parent_run_id=rid, parent_request=safe_user_msg,
                        # The policy THIS turn is running under, not a fresh read of
                        # the panel: an accepted request's frozen grants bind the
                        # subtasks it spawns too, exactly as its ceilings do above.
                        capabilities=getattr(ctx, "capabilities", None))
                except DelegatedInterrupt as exc:
                    model_calls += exc.result.model_calls
                    raise
                model_calls += child_result.model_calls
                return payload

            ctx.delegate_runner = delegate_runner
        # --- cache-waste ledger (point #3): the prefix SHOULD cache turn-to-turn; when it doesn't,
        # attribute the re-billed tokens to a cause (schema change / history elision / TTL) and price
        # the waste. Seed reported_cache from the provider so a 100%-from-turn-0 bust still counts
        # (a bust reports zero cache fields, so the sticky flag would otherwise never arm).
        from .costs import cache_miss as _cache_miss, CACHE_TTL_S as _CACHE_TTL
        reported_cache = getattr(self.provider, "reports_cache", False)
        prev_prompt = 0
        prev_skey = None
        prev_system = None
        prev_elide_from = None
        prev_compact_gen = 0
        prev_t = None
        waste_tok = waste_usd = 0
        miss_n = 0
        trunc_rounds = 0            # output-truncation rounds (point 1), bounded like verify_max
        overflow_tried = False      # context-overflow recovery is once-per-run (point 9)
        # ...except when a compaction has genuinely changed what would be sent since the last
        # overflow. That is a different request, not a retry of the same one, so it earns one
        # more attempt. Cleared on use, and a second compaction needs real source progress, so
        # this cannot become an unbounded retry loop.
        compaction_since_overflow = False
        last_stop = ""              # stop_reason of the last completion (for the memory-consolidation gate)
        answer = ""
        interrupt_partial = []      # streamed text of a completion that never returned
        did_edit = verified = covered = multifile_hinted = edit_forced = False
        edited_files, last_edit_text, last_edit_path = set(), "", ""
        last_edit_turn = -100
        last_repro_turn, last_repro_failed, verify_rounds = -100, False, 0
        last_repro_asserted = False   # did the last post-edit repro actually run an `assert`?
        # Check commands this run executed successfully, oldest first. Reminder wording only —
        # a later edit still invalidates the reproduction accounting above, untouched.
        host_checks = []
        # Memo for the preflight hint's static command discovery, keyed on edit_generation
        # below: at most one marker scan per landed edit, and none on turns that changed no
        # file. Empty until the first hint actually needs it.
        preflight_detected = {}
        # Number of edits that have LANDED this run. Only used to invalidate the memo above —
        # the verification accounting keys off last_edit_turn exactly as before.
        edit_generation = 0
        coverage_rounds = 0
        critic_rounds = 0
        hook_stop_rounds = 0
        best_diff, rollback_rounds = "", 0   # white-flag guard (see ROLLBACK_NUDGE)
        # Quality supplies a convergence target, not necessarily a hard stop.  Interactive runs use
        # max_turns=0 (unlimited) while still getting the same useful commit/verify nudges; bounded
        # automation, benchmark and explicitly capped runs keep their finite range.
        try:
            turn_cap = max(0, int(self.max_turns or 0))
        except (TypeError, ValueError, OverflowError):
            turn_cap = 0
        try:
            turn_target = max(1, int(getattr(self, "turn_target", 0) or turn_cap or 50))
        except (TypeError, ValueError, OverflowError):
            turn_target = turn_cap or 50

        def _has_next_turn(turn):
            return not turn_cap or turn < turn_cap - 1

        # Convergence thresholds scale WITH the quality target, so they must stay above the solve-turn
        # distribution (rebench: resolved median 23, so a 0.55 ratio -> force_at 27 sits just above
        # it). Env-tunable for the force_at-ratio study (COLLIE_FORCE_RATIO / COLLIE_HARD_RATIO).
        _fr = float(getattr(self, "force_ratio", None) or
                    os.environ.get("COLLIE_FORCE_RATIO", "0.55"))
        _hr = float(getattr(self, "hard_ratio", None) or
                    os.environ.get("COLLIE_HARD_RATIO", "0.76"))
        force_at = max(3, int(turn_target * _fr))    # soft nudge to converge
        hard_at = max(force_at + 2, int(turn_target * _hr))  # then remove explore tools
        budget_hit = False
        canceled = False
        interrupted_child = False   # a nested run must not swallow the user's Ctrl-C
        # Ran out of turns, as opposed to deciding it was finished. Every voluntary ending leaves the
        # loop through a `break`, so `for … else` marks exactly the case where the range simply ran
        # out — mid-task, by definition. Without this the two endings were indistinguishable
        # afterwards and both reported the same word: "done".
        turns_exhausted = False
        try:
            for turn in (range(turn_cap) if turn_cap else itertools.count()):
                if res.error:
                    res.turns = turn
                    break
                call_cap = max(0, int(getattr(self, "max_model_calls", 0) or 0))
                if call_cap and model_calls >= call_cap:
                    budget_hit = True
                    res.turns = turn
                    break
                if self._cancel_requested():
                    canceled = True
                    res.error = "canceled by user"
                    res.turns = turn
                    self._emit("canceled", at="turn_boundary")
                    break
                shared_budget_hit = bool(self.shared_budget is not None
                                         and self.shared_budget.exceeded())
                if shared_budget_hit or (turn > 0 and self._over_budget(total)):
                    budget_hit = True         # spent past the $/token ceiling — stop before another turn
                    res.turns = turn
                    break
                # mid-run steering (point 13): inject any user text typed while the run is in flight,
                # as a user message BEFORE this turn's build. Every mid-run `continue` funnels back
                # here, so this single site covers pi's loop-start AND after-tool-results polls.
                steers = self._drain_steering()
                if steers:
                    txt = "\n".join(steers)
                    if self.gate is not None and hasattr(self.gate, "extend_request"):
                        self.gate.extend_request(txt)
                    session["messages"].append({"role": "user", "content": txt})
                    res.steer_count += 1
                    self._emit("steer", text=txt[:200])
                    self.recorder.log_turn(rid, turn, "steer", txt[:500], 0, 0, 0, 0)
                # ...and the durable half: instructions accepted on any surface,
                # which survived the process that took them. Same boundary, one
                # message each, acknowledged only once they are in the journal.
                _drained, inbox_error = self._consume_durable_steering(
                    session, res, rid, turn)
                if inbox_error:
                    res.error = inbox_error
                    res.turns = turn
                    break
                system, msgs, meta = self.composer.build(
                    session, safe_user_msg, self.cwd, self.project, self.mode)
                # Long-conversation compaction (compaction.py). Costs nothing until the
                # estimated request crosses the threshold (or a real overflow forces it), and
                # then costs exactly one request, accounted below like every other one.
                compacted = self._maybe_compact(
                    session, system, msgs, total, rid, turn, model_calls)
                if compacted is not None:
                    model_calls += compacted[1]
                    if compacted[0]:
                        system, msgs, meta = self.composer.build(
                            session, safe_user_msg, self.cwd, self.project, self.mode)
                        self._settle_compaction(session, system, msgs, rid, turn)
                        compaction_since_overflow = True
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
                # ...and where the durable history ends: anything added after it for this request
                # only (the preflight, a repair nudge) is not worth a cache entry. Overflow
                # recovery moves its window every turn, so its tail is not marked at all.
                self.provider.cache_history_end = (0 if session.get("_overflow_shrink")
                                                   else len(msgs))

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
                # Finish preflight (preflight.py): after a landed edit whose verification is
                # still missing or failed, tell the model the state NOW — on the request it is
                # already paying for — instead of letting it compose a full answer first and
                # meeting the reminder afterwards. Attached to THIS request only, exactly like
                # the format_repair correction below: session["messages"] is not touched, so
                # nothing is duplicated into durable history, re-checkpointed, compacted, or
                # billed as cached prefix, and the hint is recomputed from live state each turn
                # (a check that really passes on the latest edit simply stops producing one).
                # It rides after the composed history, i.e. after meta.elide_from, so the stable
                # cached prefix the provider was told about is unchanged.
                preflight = self._verification_preflight(
                    did_edit, edited_files, host_checks, last_edit_turn, last_repro_turn,
                    last_repro_failed, last_repro_asserted, preflight_detected,
                    edit_generation)
                base_messages = (list(msgs) + preflight) if preflight else msgs
                call_messages = base_messages
                while True:
                    call_cap = max(0, int(getattr(self, "max_model_calls", 0) or 0))
                    if call_cap and model_calls >= call_cap:
                        budget_hit = True
                        break
                    if self._cancel_requested():
                        canceled = True
                        res.error = "canceled by user"
                        break
                    try:
                        journal_state = "calling_model"
                        self._session_checkpoint(
                            session["messages"], rid, turn, journal_state,
                            {"attempt": attempts + 1})
                        from .cancellation import complete as complete_cancelable
                        # Ctrl-C during generation raises through the provider, so the
                        # text the user already watched arrive would be lost with it.
                        # Tap it ONLY when streaming is already on: handing a provider
                        # an on_text it was not given would change its request mode.
                        del interrupt_partial[:]
                        on_text = self.stream_cb
                        if on_text is not None:
                            def on_text(piece, _cb=self.stream_cb):
                                interrupt_partial.append(piece)
                                _cb(piece)
                        comp = complete_cancelable(
                            self.provider, system, call_messages, schemas,
                            on_text=on_text, cancelled=self.cancelled)
                    except Exception as e:
                        comp = _error_completion(getattr(self.provider, "name", "?"), e)
                    # The completion owns its text from here; only an unreturned call
                    # leaves the tap as the sole record of what was produced.
                    del interrupt_partial[:]
                    journal_state = "model_complete"
                    self._session_checkpoint(
                        session["messages"], rid, turn, journal_state,
                        {"stop_reason": comp.stop_reason,
                         "tool_calls": [c.name for c in comp.tool_calls]})
                    # A failed streaming attempt burned real tokens too. Pack's aggregate observer
                    # sees the same record exactly once, so N candidates share one budget.
                    self._account_usage(total, comp.usage)
                    # What the provider says it physically issued: a repairing adapter's real
                    # attempts count in full, and an attempt that never left the host (a denied
                    # request reservation) counts as the 0 it reports — the run's receipt is a
                    # record of provider usage, not of intentions.
                    model_calls += issued_requests(comp)
                    if self._cancel_requested():
                        canceled = True
                        res.error = "canceled by user"
                        if comp.stop_reason != "error" and comp.text:
                            answer = comp.text
                        break
                    if comp.stop_reason != "error":
                        consecutive_contract_repairs = 0
                        break
                    cls = classify_error(
                        comp.error_detail or comp.text or "", comp.error_status,
                        getattr(comp, "error_code", ""))
                    retry_at = provider_retry_at(getattr(comp, "retry_at", 0))
                    if cls in ("retryable", "exhausted") and retry_at:
                        # Checkpoint instead of sleeping or spending more calls
                        # before a provider-attested quota reset. Mission owns
                        # the durable timer; a foreground run returns its reason.
                        # `exhausted` is here too: the usage-limit envelope that
                        # now classifies as a spent plan is the very error this
                        # wait was built for, and reading it as anything else
                        # would spend the reset window on doomed calls or on a
                        # silent step down a model nobody chose.
                        res.retry_at = retry_at
                        comp.text = "%s: [provider quota reset at %s UTC] HTTP %d %s" % (
                            cls, time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(retry_at)),
                            comp.error_status, comp.error_detail or "rate limit")
                        self._emit("provider_wait", retry_at=retry_at,
                                   error_code=getattr(comp, "error_code", ""))
                        break
                    if (cls == "overflow" and self.overflow_recovery
                            and (not overflow_tried or compaction_since_overflow)
                            and _has_next_turn(turn)):
                        overflow_tried = overflow_now = True
                        compaction_since_overflow = False
                        session["_overflow_shrink"] = True   # composer shrinks the history next build
                        # Ask compaction to run on the next build regardless of the estimate —
                        # the provider just told us the estimate was wrong. It still refuses on a
                        # short history, so the tiny-history one-shot retry above is unchanged.
                        session[_compaction.FORCE_KEY] = True
                        self.recorder.log_turn(rid, turn, "overflow",
                                               (comp.error_detail or comp.text or "")[:200],
                                               comp.usage.input_tokens, comp.usage.output_tokens,
                                               meta.prefix_tokens, 0)
                        self._emit("overflow_recovery", detail=(comp.error_detail or comp.text or "")[:200])
                        break
                    shared_exhausted = bool(self.shared_budget is not None
                                            and self.shared_budget.exceeded())
                    call_cap = max(0, int(getattr(self, "max_model_calls", 0) or 0))
                    if (cls == "protocol"
                            and consecutive_contract_repairs < max(
                                0, int(getattr(self, "max_contract_repairs", 1) or 0))
                            and (not call_cap or model_calls < call_cap)
                            and not shared_exhausted
                            and not self._over_budget(total)):
                        res.contract_repairs += 1
                        consecutive_contract_repairs += 1
                        reason = _safe_contract_reason(comp)
                        # Do not append either the rejected output or this synthetic correction to
                        # session["messages"].  The next successful tool/answer is the only assistant
                        # turn that becomes durable history.  Built on base_messages so a repair
                        # does not silently drop this turn's verification state, which is the one
                        # sequence (long answer → contract error → repair) this lane is about.
                        call_messages = list(base_messages) + [{
                            "role": "user",
                            "content": format_repair_nudge(
                                reason, [s.get("name") for s in schemas]),
                            "source": "harness", "kind": "format_repair",
                        }]
                        self.recorder.log_turn(
                            rid, turn, "format_repair",
                            "response_contract_error%s; corrective request %d/%d" % (
                                (" [%s]" % reason) if reason else "",
                                consecutive_contract_repairs, self.max_contract_repairs),
                            comp.usage.input_tokens, comp.usage.output_tokens,
                            meta.prefix_tokens, 0, cache_read=comp.usage.cache_read)
                        self._emit(
                            "format_repair", attempt=consecutive_contract_repairs,
                            total_repairs=res.contract_repairs,
                            max=self.max_contract_repairs,
                            reason=reason,
                            error_code=(getattr(comp, "error_code", "")
                                        or "response_contract_error"))
                        continue
                    if (cls == "retryable" and attempts < self.max_retries
                            and (not call_cap or model_calls < call_cap)
                            and not shared_exhausted
                            and not self._over_budget(total)):
                        delay = self.retry_base * (2 ** attempts)
                        attempts += 1
                        self.recorder.log_turn(rid, turn, "retry",
                            "%s in %.0fs: %s" % (cls, delay, (comp.error_detail or comp.text or "")[:120]),
                            comp.usage.input_tokens, comp.usage.output_tokens, meta.prefix_tokens, 0)
                        self._emit("retry", attempt=attempts, max=self.max_retries, delay_s=delay,
                                   error=(comp.error_detail or comp.text or "")[:200])
                        if not self.cancelled:
                            time.sleep(delay)          # preserve the zero-overhead/default contract
                        else:
                            deadline = time.time() + delay
                            while time.time() < deadline:
                                if self._cancel_requested():
                                    canceled = True
                                    res.error = "canceled by user"
                                    break
                                time.sleep(min(.1, max(0, deadline - time.time())))
                            if canceled:
                                break
                        continue
                    # Exhaustion never changes the accepted model. A different
                    # model needs a new explicit selection, with its own receipt.
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
                    if cls == "protocol":
                        # Content-free terminal form: malformed assistant text must not enter the
                        # result, recorder, session checkpoint, or memory through an error string.
                        # The structural category DOES belong here: without it a finished run is
                        # undiagnosable afterwards -- "the model wrote prose", "its JSON did not
                        # escape a newline" and "it named a tool we do not have" all read alike,
                        # and they call for three different next actions.  It said
                        # "structured-response" even for runs that never used structured mode.
                        note = ("gave up after %d response-contract repair%s" % (
                            consecutive_contract_repairs, "" if consecutive_contract_repairs == 1 else "s")
                                if consecutive_contract_repairs else
                                "response-contract repair unavailable at the request budget")
                        reason = _safe_contract_reason(comp)
                        comp.text = "protocol: [%s] %sresponse_contract_error%s" % (
                            note, ("HTTP %d " % comp.error_status) if comp.error_status else "",
                            (" [%s]" % reason) if reason else "")
                    elif cls == "exhausted":
                        # A spent plan is the one failure where the provider's own words are the
                        # least useful part: the envelope names a plan_type and never the provider,
                        # so "which subscription ran out, and what else do I have" — the only two
                        # questions the reader has — are answered here or nowhere.
                        from .providers import explain_exhausted
                        comp.text = explain_exhausted(
                            getattr(self.provider, "name", ""),
                            comp.error_detail or comp.text or "", comp.error_status)
                    elif cls == "overflow":
                        # A context overflow IS a recognised failure; saying "matches no known
                        # pattern" sent a reader looking for a provider problem that was not there.
                        note = ("the conversation was still too long after it was shrunk once"
                                if overflow_tried else
                                "the conversation is too long and overflow recovery is off"
                                if not self.overflow_recovery else
                                "the conversation is too long, with no turn left to shrink it")
                        comp.text = "%s: [%s] %s%s" % (
                            cls, note, ("HTTP %d " % comp.error_status) if comp.error_status else "",
                            comp.error_detail or comp.text or "provider error")
                    else:
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
                if canceled:
                    self._emit("canceled", at="model_boundary")
                    break
                if budget_hit:
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
                    # The native overnight OAuth profile carries Collie's system
                    # block only; the measured prefix therefore remains a harness
                    # measurement rather than a Claude Code prompt measurement.
                    res.prefix_measured = u.cache_creation + u.cache_read

                # --- cache-waste detection (point #3)
                skey = ",".join(sorted(s["name"] for s in schemas))
                system_key = hashlib.sha1(str(system).encode("utf-8", "replace")).hexdigest()
                cause = []
                if prev_skey is not None and skey != prev_skey:
                    cause.append("schema")           # tool set changed (load_tools / hard_at restriction)
                if prev_system is not None and system_key != prev_system:
                    # The system block leads the request, so any change in it (core memory the run
                    # wrote, Live context, a new day) re-reads the whole history after it. These
                    # were all "unexplained": 681 turns and 4.0M tokens in one machine's run log.
                    cause.append("system")
                compact_gen = int((meta.compaction or {}).get("generation") or 0)
                if compact_gen != prev_compact_gen:
                    # Replacing an old span with a summary rewrites the message prefix, so the
                    # first request after a compaction cannot cache-hit. It is a real, priced
                    # cost of the feature and belongs in the ledger by name, not as
                    # "unexplained" — the whole point of the cause column.
                    cause.append("compact")
                # Read the window from the list elide_from indexes into. Once compaction is
                # active that is the PROJECTION, not the raw transcript, and slicing the wrong
                # list would attribute the miss to the wrong cause (or miss it entirely).
                elide_src = meta.pre_elision or session["messages"]
                # A boundary below zero (a short history) stubs nothing, and the first move off
                # zero counts: stepped elision makes 0 -> ELIDE_STEP the first real one.
                elided_from = max(prev_elide_from, 0) if prev_elide_from is not None else None
                if elided_from is not None and meta.elide_from > elided_from and any(
                        m.get("role") == "tool" and isinstance(m.get("content"), str)
                        and len(m["content"]) > 240
                        for m in elide_src[elided_from:meta.elide_from]):
                    cause.append("elide")            # history elision newly stubbed a big tool output
                if prev_t and time.time() - prev_t > _CACHE_TTL:
                    cause.append("ttl?")             # NB completion-to-completion incl. generation time
                mt, mu = _cache_miss(prev_prompt, u, self.provider.model, reported_cache)
                c_str = "+".join(cause) or ("unexplained" if mt else "")
                if mt:
                    miss_n += 1; waste_tok += mt; waste_usd += mu
                    self._emit("cache_miss", tokens=mt, usd=mu, cause=c_str)
                prev_skey = skey
                prev_system = system_key
                prev_elide_from = meta.elide_from
                prev_compact_gen = compact_gen
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
                # nudge to continue. Bounded by trunc_rounds so it can't spin.
                # Counted per EPISODE, like the structured-response repair above: a completion that
                # did not stop at the output limit is real forward progress — its tool results or
                # text are already durable history — so it restores the allowance. A run-global
                # count ended a long, healthy run on its third SEPARATED truncation and threw away
                # every recovered turn in between, reporting a "loop" that had never happened.
                if comp.stop_reason != "length":
                    trunc_rounds = 0
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
                        session["messages"].append({"role": "user", "content": TRUNC_CONTINUE,
                                                    "source": "harness", "kind": "output_continuation"})
                    res.turns = turn + 1
                    # KEY: retrying at the SAME output ceiling truncates again -> the loop the user hit.
                    # Give the retry real room by escalating the cap (x2, bounded). A task that legit
                    # needs a big output finishes; a runaway is still stopped by the round bound below.
                    ceiling = 0
                    try:
                        cur = int(getattr(self.provider, "max_tokens", 0) or 0)
                        if 0 < cur < 32768:
                            self.provider.max_tokens = min(32768, cur * 2)
                        ceiling = int(getattr(self.provider, "max_tokens", 0) or 0)
                    except (TypeError, ValueError):
                        pass
                    if trunc_rounds >= 3 or not _has_next_turn(turn):
                        # give up retrying: surface a partial plain answer (with a marker), else error
                        if not comp.tool_calls and (comp.text or "").strip():
                            answer = comp.text
                        else:
                            # Name the ceiling that actually stopped this run. One string said
                            # "truncation loop" for both a genuine repeat and a single truncation
                            # that happened to land on the last available turn, and quoted no
                            # number a reader could raise.
                            res.error = res.error or (
                                "output-limit truncation: %s; its tool calls were not executed "
                                "because truncated arguments are unsafe to run%s" % (
                                    ("%d consecutive responses hit the output-token limit"
                                     % trunc_rounds) if trunc_rounds >= 3 else
                                    "the response hit the output-token limit on the last "
                                    "available turn",
                                    (" (output ceiling now %d tokens)" % ceiling) if ceiling else ""))
                        break
                    continue

                if comp.tool_calls:
                    session["messages"].append(
                        {"role": "assistant", "content": comp.text,
                         "tool_calls": comp.tool_calls,
                         # preserve signed thinking so the NEXT request can replay it (required by
                         # the API when extended thinking + tool use are both on). Empty when off.
                         "thinking_blocks": comp.thinking_blocks})
                    if self._cancel_requested():
                        canceled = True
                        res.error = "canceled by user"
                        for tc in comp.tool_calls:
                            session["messages"].append(
                                {"role": "tool", "tool_call_id": tc.id, "name": tc.name,
                                 "content": "CANCELED: run stopped before execution"})
                            res.tool_calls += 1
                            self._emit("tool", name=tc.name, args=tc.args, ok=False,
                                       canceled=True, result="run stopped before execution")
                        self._emit("canceled", at="tool_boundary",
                                   next_tool=comp.tool_calls[0].name)
                        res.turns = turn + 1
                        break
                    # ── pass 1: repair + AUTHORIZE every call in this turn, before running any ──
                    # Authorizing up front is the point: when the model proposes five calls, the
                    # human sees all five and decides, instead of discovering the third one only
                    # after the first two already happened irreversibly.
                    def _prepare_tool_call(raw_tc, still_active=None, forced_denial=None):
                        """Canonicalize and authorize one call without executing it.

                        Both provider-authored calls and execute_code RPC calls enter here.  Keeping
                        this as one closure preserves the load-bearing ordering: authorization sees
                        repaired arguments, but never secret-restored values.
                        """
                        tc = raw_tc
                        tool = self.registry.get(tc.name)
                        repairs = []
                        if isinstance(tc.args, dict) and "_malformed_args" in tc.args:
                            return tc, tool, repairs, None
                        if tool is not None:
                            rargs, repairs = repair_args(
                                tc.args, getattr(tool, "schema", {}) or {})
                            if repairs:
                                tc = ToolCall(tc.id, tc.name, rargs)
                                res.arg_repairs += 1
                                self._emit("repair", name=tc.name, kinds=repairs)
                        pre = self._hook("PreToolUse", {
                            "run_id": rid, "task_id": task_id, "turn": turn,
                            "tool_name": tc.name, "tool_input": tc.args,
                        }, subject=tc.name)
                        if pre is not None and pre.additional_context:
                            hook_contexts.extend(pre.additional_context)
                        if forced_denial and not self._cancel_requested():
                            # Host invariants (currently: memory must not outlive a timed-out RPC)
                            # are not user-overridable permission questions, but they are still
                            # auditable denials at the same boundary as Gate decisions.
                            from .gate import Decision
                            from .risk import classify, target_for
                            risk = classify(
                                tc.name, tool, getattr(self.gate, "risk_overrides", None)).value
                            target = target_for(
                                tc.name, tc.args,
                                getattr(self.gate, "origin_lookup", None))
                            policy = Decision(False, forced_denial, risk=risk, target=target)
                            self._audit(
                                tc, policy, stage="denied", outcome="refused",
                                reason=forced_denial)
                            self._emit("gate", name=tc.name, decision="denied",
                                       reason=forced_denial, risk=risk, target=target)
                        denied = ("run canceled" if self._cancel_requested() else
                                  (forced_denial if forced_denial else
                                   ("lifecycle hook denied this tool: %s" %
                                    (pre.reason or "policy rejection")
                                    if pre is not None and not pre.allowed else
                                    self._authorize(tc, tool, still_active=still_active))))
                        return tc, tool, repairs, denied

                    def _account_tool_outcome(tc, out, receipt=None, *, dispatched=False):
                        """Apply the normal edit/reproduction accounting to every dispatched call.

                        ``receipt`` is the host-minted execution record for this exact call (or
                        None), captured by the caller straight off ``Tool.run``'s return value.
                        It is passed by value, never stashed, so it cannot outlive its call.

                        ``dispatched`` records whether this call reached ``Tool.run``. A refused
                        call provides no workspace evidence and must preserve the last real
                        verification result. In particular, DENIED text is not a passing test.
                        """
                        nonlocal did_edit, last_edit_turn, last_repro_turn
                        nonlocal last_repro_failed, last_repro_asserted, edit_generation
                        nonlocal last_edit_path, last_edit_text, best_diff
                        try:            # edit-accounting + repro detection: best-effort bookkeeping
                            if os.environ.get("COLLIE_DEBUG"):
                                a = json.dumps(tc.args, ensure_ascii=False)
                                print("  T%d %s(%s)%s -> %s" % (
                                    turn, tc.name, a[:90], "" if dispatched else " [not run]",
                                    str(out)[:120].replace("\n", " ")),
                                    flush=True)
                            if not dispatched:
                                # Refusals and pre-dispatch failures are not fresh observations.
                                return
                            # count an edit ONLY if it actually landed. edit_file/write_file
                            # return "ERROR: old_string not found/appears N times" WITHOUT writing.
                            edit_ok = (tc.name in ("write_file", "edit_file")
                                       and isinstance(out, str)
                                       and not out.startswith(("ERROR", "DENIED")))
                            if edit_ok:
                                did_edit = True
                                last_edit_turn = turn
                                # The workspace just changed, so anything derived from its
                                # contents (the preflight's detected check command) is stale.
                                edit_generation += 1
                                # A landed edit invalidates earlier reproduction evidence, including
                                # an internal execute_code call that reproduced before a later write.
                                last_repro_turn, last_repro_failed, last_repro_asserted = (
                                    -100, False, False)
                                p = tc.args.get("path", "")
                                if p:
                                    p = p if isinstance(p, str) else str(p)
                                    rp = (os.path.relpath(p, self.cwd)
                                          if os.path.isabs(p) else p)
                                    edited_files.add(rp)
                                    last_edit_path = rp
                                last_edit_text = (tc.args.get("new_string")
                                                  or tc.args.get("content") or last_edit_text)
                                self._emit("edit", path=last_edit_path,
                                           old=tc.args.get("old_string", ""),
                                           new=tc.args.get("new_string")
                                           or tc.args.get("content", ""))
                                if self.force_edit:
                                    best_diff = _tree_diff(self.cwd) or best_diff
                            if did_edit and _is_repro_cmd(tc.name, tc.args):
                                last_repro_turn = turn
                                o = out if isinstance(out, str) else str(out)
                                last_repro_failed = _repro_failed(
                                    o, tc.name, tc.args.get("command") or "", receipt)
                                last_repro_asserted = _is_asserting_cmd(
                                    tc.args.get("command") or "")
                                self._emit("repro", passed=not last_repro_failed,
                                           asserted=last_repro_asserted,
                                           cmd=(tc.args.get("command") or "")[:200])
                            # Independent of the gate's freshness accounting: remember WHICH
                            # check command actually worked here, so a later reminder can name
                            # it instead of guessing from markers. Not evidence — an entry
                            # survives the edit that just invalidated its result.
                            observed = _host_observed_check(tc.name, tc.args, out)
                            if observed:
                                # Bound to the directory it ran in: a later reminder for some
                                # other workspace must not quote it.
                                entry = (self.cwd, observed)
                                if entry in host_checks:
                                    host_checks.remove(entry)
                                host_checks.append(entry)
                                del host_checks[:-_HOST_CHECK_MEMORY]
                        except Exception as _acc_e:
                            if os.environ.get("COLLIE_DEBUG"):
                                print("  [accounting error, continuing] %s" % _acc_e, flush=True)

                    def _execute_prepared_tool(tc, tool, repairs, denied, *, record_result=True,
                                               journal_parent=None, still_active=None,
                                               begin_effect=None, end_effect=None):
                        """The single Harness execution boundary used by normal and RPC calls.

                        Internal calls deliberately do not append a standalone tool-result message:
                        the provider emitted only the parent execute_code tool_use, so such a message
                        would be protocol-invalid.  They still pass through every host-owned fence.
                        """
                        nonlocal journal_state, journal_detail, durability_fault
                        if still_active is not None and not still_active():
                            # A late HTTP handler must not mutate a completed RunResult, fire hooks,
                            # or overwrite the parent's terminal session checkpoint.
                            return "DENIED: parent execute_code invocation is no longer active"
                        uncertain_boundary = False
                        # Execution evidence for THIS invocation only. A local, so every dispatch —
                        # including one that is denied, malformed, or fails before running — starts
                        # with none, and no later call or concurrent inner RPC call can inherit it.
                        receipt = None
                        # Per-call state, including inner RPCs; a Tool.run exception still counts
                        # as execution and its ERROR remains failed verification evidence.
                        dispatched = False
                        if isinstance(tc.args, dict) and "_malformed_args" in tc.args:
                            out = ("ERROR: tool call arguments were not valid JSON (truncated or "
                                   "malformed). Raw prefix: %s. Re-emit the call with valid JSON "
                                   "arguments." % str(tc.args.get("_malformed_args"))[:500])
                        elif denied is not None:
                            out = "DENIED: %s" % denied
                            res.denied_calls += 1
                        elif still_active is not None and not still_active():
                            out = "DENIED: parent execute_code invocation is no longer active"
                            res.denied_calls += 1
                        else:
                            try:
                                # Restore placeholders only at the execution boundary.  Remember is
                                # the sole exception because it persists its input as plaintext.
                                skip_restore = tc.name == "remember"
                                run_args = (_redact.restore(tc.args, self._secret_vault)
                                            if (_redact_on and not skip_restore) else tc.args)
                                journal_state = "executing_tool"
                                parent = journal_parent or {}
                                detail = {
                                    "tool_name": parent.get("tool_name") or tc.name,
                                    "tool_call_id": parent.get("tool_call_id") or tc.id,
                                }
                                # Attest the implementation, not a plugin's name or
                                # read-only hint. A killed built-in read can resume
                                # without asking the user to inspect external effects.
                                if record_result and not parent:
                                    from .tools import (ReadFileTool, GlobTool, GrepTool,
                                                        MemorySearchTool)
                                    from .delegate import DelegateTool
                                    if type(tool) in (ReadFileTool, GlobTool, GrepTool,
                                                      MemorySearchTool, DelegateTool):
                                        detail["replay_safe"] = True
                                if not record_result:
                                    detail.update(internal=True, inner_tool_name=tc.name,
                                                  inner_tool_call_id=tc.id)
                                journal_detail = dict(detail)
                                if still_active is not None and not still_active():
                                    return ("DENIED: parent execute_code invocation ended before "
                                            "the inner tool could execute")
                                checkpointed = self._session_checkpoint(
                                    session["messages"], rid, turn, journal_state, detail)
                                if checkpointed is False:
                                    out = ("ERROR: durability checkpoint failed; tool was not "
                                           "executed because crash recovery could not be fenced")
                                    # Further model turns cannot repair a failed host journal.
                                    durability_fault = durability_fault or (
                                        "the host could not persist a crash-recovery checkpoint "
                                        "before running %s; this run stopped without executing it. "
                                        "Check the session store, then resume the thread."
                                        % (tc.name,))
                                elif still_active is not None and not still_active():
                                    return ("DENIED: parent execute_code invocation ended before "
                                            "the inner tool could execute")
                                elif tool is None:
                                    out = "ERROR: no such tool %s" % tc.name
                                elif begin_effect is not None and not begin_effect():
                                    return ("DENIED: parent execute_code invocation ended before "
                                            "the inner consequential tool could execute")
                                elif tc.name == "execute_code":
                                    # The broker exists only for the duration of this parent call.
                                    # Restoring/deleting it in finally prevents a stale closure from
                                    # leaking the completed run's authority through a reused ToolCtx.
                                    had_broker = hasattr(ctx, "tool_broker")
                                    previous_broker = getattr(ctx, "tool_broker", None)
                                    inner_broker = _make_inner_broker(tc.id)
                                    ctx.tool_broker = inner_broker
                                    previous_call_id = ctx.tool_call_id
                                    ctx.tool_call_id = tc.id
                                    try:
                                        dispatched = True
                                        out = tool.run(run_args, ctx)
                                    finally:
                                        ctx.tool_call_id = previous_call_id
                                        if had_broker:
                                            ctx.tool_broker = previous_broker
                                        else:
                                            delattr(ctx, "tool_broker")
                                    if inner_broker.uncertain:
                                        uncertain_boundary = True
                                        res.error = ("execute_code ended while an inner tool "
                                                     "was still running; "
                                                     "recovery inspection is required")
                                        if not str(out).startswith("ERROR"):
                                            out = "ERROR: %s\n%s" % (res.error, out)
                                else:
                                    previous_call_id = ctx.tool_call_id
                                    ctx.tool_call_id = tc.id
                                    try:
                                        dispatched = True
                                        out = tool.run(run_args, ctx)
                                        # Capture the host's own record of what ran BEFORE
                                        # redaction (which returns a plain str and would drop it)
                                        # and before any text-based reading of the result.
                                        receipt = _bound_receipt(out, tc, run_args)
                                    finally:
                                        ctx.tool_call_id = previous_call_id
                                        if end_effect is not None:
                                            end_effect()
                            except Exception as e:
                                out = "ERROR: tool %s failed: %s" % (tc.name, e)
                        if (not record_result and still_active is not None and
                                not still_active()):
                            # The parent execute_code call has already returned and
                            # persisted an external_action recovery fence.  A late
                            # handler may finish the real effect, but it must never
                            # rewrite the closed RunResult, transcript, hooks, counters,
                            # or checkpoint state from its daemon thread.
                            if _redact_on:
                                out = _redact.redact_obj(out, self._secret_vault)
                            return out
                        if getattr(ctx, "tool_effect_uncertain", False):
                            uncertain_boundary = True
                            res.error = ("a tool returned without confirming its process lifetime; "
                                         "recovery inspection is required")
                            if not str(out).startswith("ERROR"):
                                out = "ERROR: %s\n%s" % (res.error, out)
                        # A custom or deferred tool may return structured data.  Redact it before
                        # lifecycle hooks, event sinks, transcripts, or the RPC response can see it;
                        # limiting this boundary to strings would leak nested secret values.
                        if _redact_on:
                            out = _redact.redact_obj(out, self._secret_vault)
                        failed_tool = isinstance(out, str) and (
                            out.startswith("ERROR") or out.startswith("DENIED"))
                        post = self._hook(
                            "PostToolUseFailure" if failed_tool else "PostToolUse", {
                                "run_id": rid, "task_id": task_id, "turn": turn,
                                "tool_name": tc.name, "tool_input": tc.args,
                                "tool_response": out,
                            }, subject=tc.name)
                        if post is not None and post.additional_context:
                            hook_contexts.extend(post.additional_context)
                        res.tool_calls += 1
                        if record_result:
                            # Only provider-authored calls get protocol messages.  RPC calls are
                            # summarized by the parent execute_code result instead.
                            tmsg = {"role": "tool", "tool_call_id": tc.id,
                                    "name": tc.name, "content": out}
                            if repairs:
                                tmsg["repairs"] = repairs
                            session["messages"].append(tmsg)
                        # An internal RPC result does not finish the provider-authored execute_code
                        # tool_use. Keep recovery fenced on that real parent id until its one paired
                        # result is appended below; otherwise a crash fabricates an orphan rpc id and
                        # falsely reports the still-running parent as auto-resumable.
                        journal_state = ("external_action" if uncertain_boundary else
                                         "tool_complete" if record_result else "executing_tool")
                        parent = journal_parent or {}
                        detail = {
                            "tool_name": parent.get("tool_name") or tc.name,
                            "tool_call_id": parent.get("tool_call_id") or tc.id,
                            "ok": not failed_tool,
                        }
                        if not record_result:
                            detail.update(internal=True, inner_complete=True,
                                          inner_tool_name=tc.name, inner_tool_call_id=tc.id)
                        journal_detail = dict(detail)
                        if self._session_checkpoint(
                                session["messages"], rid, turn, journal_state,
                                detail) is False:
                            # The call is over and only its receipt was lost.  Stop rather
                            # than buy turns this host still cannot journal, and report the
                            # call exactly as far as it got: dispatch alone does not prove an
                            # effect, and a completed one must not be replayed or rolled back
                            # on a guess about the outside world.
                            if not dispatched:
                                fault = ("the host could not persist the outcome of %s, "
                                         "which was not executed; check the session store, "
                                         "then resume the thread")
                            elif failed_tool:
                                fault = ("%s was started and then reported a failure the "
                                         "host could not save; it may have taken effect "
                                         "before failing, so do not blindly replay it or "
                                         "roll it back. Check the session store and what "
                                         "the tool actually did")
                            else:
                                fault = ("%s completed and its result could not be saved; "
                                         "the call did run, so do not replay it or roll its "
                                         "effect back. Check the session store and what the "
                                         "tool actually did")
                            durability_fault = durability_fault or fault % (tc.name,)
                        # Internal screenshots stay queued until the parent result is paired; adding
                        # a user image before that result would break provider tool-use ordering.
                        if record_result and getattr(ctx, "images", None):
                            for img in ctx.images:
                                label = img.get("label") or "screen"
                                source = img.get("source") or "screenshot"
                                session["messages"].append({"role": "user", "source": "harness",
                                    "kind": "tool_attachment", "content": [
                                    {"type": "text", "text": "[%s: %s]" % (source, label)},
                                    {"type": "image",
                                     "media_type": img.get("media_type", "image/png"),
                                     "data": img["data"]}]})
                            ctx.images.clear()
                        rprev = ""
                        if isinstance(out, str) and out.strip():
                            first = next((ln for ln in out.splitlines() if ln.strip()), "")
                            rprev = first[:160] + (" …" if len(out) > len(first) + 2 else "")
                        emit_data = {
                            "name": tc.name, "args": tc.args,
                            "ok": not failed_tool,
                            "result": rprev,
                        }
                        if not record_result:
                            emit_data["internal"] = True
                        self._emit("tool", **emit_data)
                        _account_tool_outcome(tc, out, receipt, dispatched=dispatched)
                        return out

                    def _make_inner_broker(parent_call_id):
                        # ThreadingHTTPServer may receive concurrent calls from a user script.  The
                        # Harness transcript, counters and checkpoint journal are ordered state, so
                        # serialize those calls while leaving the child process itself unconstrained.
                        secret_vault = self._secret_vault
                        class InnerBroker:
                            def __init__(self):
                                self.lock = threading.RLock()
                                self.sequence = 0
                                self.revoked = threading.Event()
                                self.state_lock = threading.Lock()
                                self.effects_in_flight = 0
                                self.uncertain = False
                                self.effects_idle = threading.Event()
                                self.effects_idle.set()
                                self.partial_results = []

                            def revoke(self):
                                # Non-blocking: an inbox approver can be waiting on another thread.
                                # Its eventual answer is re-checked below and cannot fire late.
                                self.revoked.set()
                                with self.state_lock:
                                    if self.effects_in_flight:
                                        self.uncertain = True

                            def active(self):
                                return not self.revoked.is_set()

                            def quiesce(self, timeout=.25):
                                # Cancellation reaches owned subprocess tools cooperatively.
                                # Give their already-running handlers a short chance to close
                                # before deciding that the parent's effect is still unknown.
                                self.effects_idle.wait(timeout)
                                with self.state_lock:
                                    if not self.effects_in_flight:
                                        self.uncertain = bool(getattr(ctx, "tool_effect_uncertain", False))

                            def captured_results(self):
                                with self.state_lock:
                                    return list(self.partial_results)

                            def begin_effect(self):
                                with self.state_lock:
                                    if self.revoked.is_set():
                                        return False
                                    self.effects_in_flight += 1
                                    self.effects_idle.clear()
                                    return True

                            def end_effect(self):
                                with self.state_lock:
                                    self.effects_in_flight = max(0, self.effects_in_flight - 1)
                                    if not self.effects_in_flight:
                                        self.effects_idle.set()

                            def __call__(self, name, args):
                              with self.lock:
                                if getattr(ctx, "tool_effect_uncertain", False):
                                    return "DENIED: an earlier tool requires recovery inspection"
                                if self.revoked.is_set():
                                    return "DENIED: parent execute_code invocation is no longer active"
                                if durability_fault:
                                    # Check under the dispatch lock before approval or execution.
                                    # A healed store does not restart a run already stopped here.
                                    # Stays generic: the failed checkpoint may have been this
                                    # run's pre-action fence or a completed call's receipt.
                                    return ("DENIED: a durability checkpoint failed earlier in "
                                            "this run; the session journal is unreliable, so no "
                                            "further tool will be started")
                                self.sequence += 1
                                # The parent execute_code source is restored immediately before
                                # execution. A secret used by that script therefore returns over
                                # RPC as plaintext; put it back behind a placeholder before Gate,
                                # hooks, audit and checkpoints observe the dynamic call. The normal
                                # execution boundary restores it again only for tool.run().
                                safe_args = (_redact.redact_obj(args, secret_vault)
                                             if _redact_on else args)
                                inner = ToolCall("%s:rpc:%d" % (parent_call_id, self.sequence),
                                                 str(name or ""), safe_args)
                                # Memory shares a connection with end-of-run settlement.  A timed-
                                # out daemon RPC must not keep reading/writing that connection after
                                # the Harness returns and its caller consolidates or closes it.
                                forced_denial = None
                                if inner.name in ("execute_code", "delegate"):
                                    forced_denial = (
                                        "%s cannot be called from inside execute_code; nested "
                                        "subprocess or sub-agent amplification is disabled" %
                                        inner.name)
                                elif inner.name in ("remember", "memory_search"):
                                    forced_denial = (
                                        "memory tools cannot run inside execute_code; call this "
                                        "tool directly so its lifecycle is joined to the parent run")
                                prepared = _prepare_tool_call(
                                    inner, still_active=self.active,
                                    forced_denial=forced_denial)
                                if self.revoked.is_set():
                                    return "DENIED: parent execute_code invocation is no longer active"
                                if not self.begin_effect():
                                    return ("DENIED: parent execute_code invocation ended before "
                                            "the inner tool could execute")
                                try:
                                    result = _execute_prepared_tool(
                                        *prepared, record_result=False,
                                        journal_parent={"tool_name": "execute_code",
                                                        "tool_call_id": parent_call_id},
                                        still_active=self.active)
                                    # The script may be killed before receiving its HTTP
                                    # response. Keep a bounded, already-redacted record so
                                    # the parent's interrupted result retains this evidence.
                                    with self.state_lock:
                                        self.partial_results.append({"tool": inner.name,
                                                                     "result": str(result)[:2000]})
                                        del self.partial_results[:-16]
                                    return result
                                finally:
                                    self.end_effect()
                        return InnerBroker()

                    _prepared = []
                    hook_contexts = []
                    for tc in comp.tool_calls:
                        _prepared.append(_prepare_tool_call(tc))

                    # ── pass 2: execute what cleared ──
                    for tool_idx, (tc, tool, repairs, _denied) in enumerate(_prepared):
                        if self._cancel_requested():
                            canceled = True
                            res.error = "canceled by user"
                            self._emit("canceled", at="tool_boundary", next_tool=tc.name)
                            # Preserve provider protocol: every tool_use in the assistant message
                            # still receives a result, even though it was deliberately not executed.
                            for pending_tc, _tool, _repairs, _deny in _prepared[tool_idx:]:
                                session["messages"].append(
                                    {"role": "tool", "tool_call_id": pending_tc.id,
                                     "name": pending_tc.name,
                                     "content": "CANCELED: run stopped before execution"})
                                res.tool_calls += 1
                                self._emit("tool", name=pending_tc.name, args=pending_tc.args,
                                           ok=False, canceled=True,
                                           result="run stopped before execution")
                            break
                        _execute_prepared_tool(tc, tool, repairs, _denied)
                        if journal_state == "external_action":
                            if self._cancel_requested():
                                canceled = True
                            break
                        if durability_fault:
                            # Every later call in this batch would meet the same refused
                            # fence. Stop dispatching; finalization closes their tool_use
                            # blocks as never-started, which is exactly what happened.
                            break
                    if hook_contexts and not canceled:
                        session["messages"].append({
                            "role": "user",
                            "source": "harness", "kind": "lifecycle_context",
                            "content": "[Trusted lifecycle context]\n" + "\n".join(hook_contexts),
                        })
                    if durability_fault:
                        # Preserve earlier errors and classify this as a host failure.
                        # Finalization still gives cancellation its existing precedence.
                        from .recorder import note_host_error as _note_host_error
                        _note_host_error(res, durability_fault)
                        self._emit("durability_stop", reason=durability_fault[:300],
                                   turn=turn)
                        res.turns = turn + 1
                        break
                    if canceled:
                        res.turns = turn + 1
                        break
                    if journal_state == "external_action":
                        res.turns = turn + 1
                        break
                    res.turns = turn + 1
                    # converge: still exploring past the deadline with no edit -> nudge ONCE
                    # (then hard tool-restriction at hard_at does the structural forcing;
                    # re-injecting every turn just accumulated duplicate identical messages).
                    if (self.force_edit and not did_edit and not edit_forced
                            and turn + 1 >= force_at and _has_next_turn(turn)):
                        session["messages"].append(
                            {"role": "user", "content": EDIT_FORCE_NUDGE,
                             "source": "harness", "kind": "edit_reminder"})
                        edit_forced = True
                    # embedding-driven multi-file coverage: right after the first edit,
                    # surface sibling locations (by similarity to the edit) that likely
                    # need the same change — proactive, not "please go grep".
                    elif (self.force_edit and did_edit and not multifile_hinted
                          and last_edit_text and self.registry.get("code_search")
                          and _has_next_turn(turn)):
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
                            session["messages"].append({"role": "user", "source": "harness",
                                "kind": "coverage_reminder", "content":
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
                                    {"role": "user", "content": self.repair_nudge or REPAIR_NUDGE,
                                     "source": "harness", "kind": "verification_reminder"})
                                verify_rounds += 1
                                res.turns = turn + 1
                                continue
                        # white-flag guard: don't spin-break out holding an EMPTY tree when a
                        # non-empty edit state existed — rescue turn(s) first (the spin window
                        # re-arms, so the model gets a bounded second chance to land something)
                        if (rollback_rounds < 1 and best_diff
                                and _has_next_turn(turn) and _tree_empty(self.cwd)):
                            session["messages"].append(
                                {"role": "user", "content": ROLLBACK_NUDGE,
                                 "source": "harness", "kind": "rollback_reminder"})
                            rollback_rounds += 1
                            res.turns = turn + 1
                            continue
                        break
                    continue

                # reproduce -> verify -> repair, EVIDENCE-gated (not a single advisory nudge).
                # Don't accept "done" after an edit until a reproduction actually ran on the
                # FIXED code (turn >= last edit) and its last run didn't error. Bounded so a
                # stubborn model can't spin; falls back to the old one-shot nudge when gate off.
                if self.self_verify and did_edit and _has_next_turn(turn):
                    if self.verify_gate:
                        # assert-mode: a print-only repro (no `assert`) is NOT verification —
                        # the wrong-output-doesn't-raise hole. Decision lives in verifier.py.
                        repro_ok = self._repro_verified(
                            did_edit, last_edit_turn, last_repro_turn,
                            last_repro_failed, last_repro_asserted)
                        if not repro_ok and verify_rounds < self.verify_max:
                            # Gate semantics are untouched: Required still refuses this finish
                            # until a reproduction has actually run. Only which check the text
                            # names is workspace-selected.
                            nudge = ((self.verify_nudge
                                      or verify_nudge_for(self.cwd, edited_files, host_checks))
                                     if last_repro_turn < last_edit_turn
                                     else (self.repair_nudge or REPAIR_NUDGE))
                            session["messages"].append({"role": "assistant", "content": comp.text})
                            session["messages"].append({"role": "user", "content": nudge,
                                "source": "harness", "kind": "verification_reminder"})
                            verify_rounds += 1
                            res.turns = turn + 1
                            continue
                    elif not verified and not self._repro_verified(
                            did_edit, last_edit_turn, last_repro_turn,
                            last_repro_failed, last_repro_asserted):
                        session["messages"].append({"role": "assistant", "content": comp.text})
                        session["messages"].append(
                            {"role": "user",
                             "content": (self.verify_nudge
                                         or verify_nudge_for(self.cwd, edited_files,
                                                             host_checks)),
                             "source": "harness", "kind": "verification_reminder"})
                        verified = True
                        res.turns = turn + 1
                        continue

                # the model wants to finish. If it never edited on a fix task, don't
                # accept the empty result — push it to make the change.
                if (self.force_edit and not did_edit and _has_next_turn(turn)):
                    session["messages"].append({"role": "assistant", "content": comp.text})
                    session["messages"].append({"role": "user", "content": EDIT_FORCE_NUDGE,
                        "source": "harness", "kind": "edit_reminder"})
                    res.turns = turn + 1
                    continue

                # edited and finishing: coverage pass for multi-file fixes.
                if self.force_edit and did_edit and _has_next_turn(turn):
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
                            session["messages"].append({"role": "user", "source": "harness",
                                "kind": "coverage_reminder", "content":
                                COVERAGE_NUDGE + "\nSame-package files closest to your change "
                                "(edit the ones that need the SAME fix; ignore those that "
                                "don't, then finish):\n" + locs})
                            coverage_rounds += 1
                            res.turns = turn + 1
                            continue
                    elif not covered:
                        session["messages"].append({"role": "assistant", "content": comp.text})
                        session["messages"].append({"role": "user", "content": COVERAGE_NUDGE,
                            "source": "harness", "kind": "coverage_reminder"})
                        covered = True
                        res.turns = turn + 1
                        continue

                # adversarial critic: an INDEPENDENT fresh read attacks the fix before we accept it.
                # Self-attack shares the model's blind spot (a misread attacks from the same misread);
                # a separate read that sees ONLY issue+diff catches under-coverage and misreads a
                # self-nudge cannot. Bounded critic->repair rounds.
                shared_exhausted = bool(self.shared_budget is not None
                                        and self.shared_budget.exceeded())
                local_exhausted = self._over_budget(total)
                if (self.critic and did_edit and _has_next_turn(turn)
                        and critic_rounds < self.critic_max
                        and (not getattr(self, "max_model_calls", 0) or
                             model_calls < int(self.max_model_calls))
                        and not shared_exhausted and not local_exhausted):
                    # For reading, not re-applying: bytes that are not UTF-8 become U+FFFD here
                    # rather than lone surrogates, which a request body cannot encode.
                    _cdiff = _tree_diff(self.cwd, binary=False).encode(
                        "utf-8", "surrogateescape").decode(
                        "utf-8", "replace")
                    if _cdiff:
                        _ok, _obj = (self.critic_fn(self.critic_issue, _cdiff, self.cwd)
                                     if self.critic_fn else
                                     self._run_critic(self.critic_issue, _cdiff))
                        if getattr(self, "_critic_usage", None):   # count the critic's own tokens/$
                            self._account_usage(total, self._critic_usage,
                                                getattr(self, "_critic_model", None))
                            self._critic_usage = None; self._critic_model = None
                            # None: an embedder's own critic_fn reported tokens but no count —
                            # unknown issuance keeps the conservative one request.
                            model_calls += request_count_of(
                                getattr(self, "_critic_request_count", None))
                            self._critic_request_count = None
                        if not _ok:
                            session["messages"].append({"role": "assistant", "content": comp.text})
                            session["messages"].append({"role": "user", "source": "harness",
                                "kind": "review_feedback", "content":
                                "An INDEPENDENT reviewer (fresh read of the issue — did NOT see your "
                                "reasoning or your test) examined your diff and raised this concern:\n\n"
                                + _obj + "\n\nIf it is valid, fix it and re-verify in run_in_env. If you "
                                "are confident it is unfounded, prove it with a run_in_env check of "
                                "exactly that case, then finish."})
                            critic_rounds += 1
                            res.turns = turn + 1
                            continue

                if self._cancel_requested():
                    canceled = True
                    answer = comp.text or ""
                    res.turns = turn + 1
                    break

                # steering finish-interception (point 13, point B): if the user typed something while
                # the model was deciding to finish, honor it instead of stopping — same gate pattern
                # as verify/coverage. Guard BEFORE draining so a steer typed on the LAST turn stays
                # queued for the next REPL prompt rather than vanishing.
                if _has_next_turn(turn):
                    steers = self._drain_steering()
                    if steers:
                        txt = "\n".join(steers)
                        if self.gate is not None and hasattr(self.gate, "extend_request"):
                            self.gate.extend_request(txt)
                        session["messages"].append({"role": "assistant", "content": comp.text})
                        session["messages"].append({"role": "user", "content": txt})
                        res.steer_count += 1
                        self._emit("steer", text=txt[:200])
                        self.recorder.log_turn(rid, turn, "steer", txt[:500], 0, 0, 0, 0)
                        res.turns = turn + 1
                        continue
                    # A durable steer accepted while the model was deciding to
                    # finish is answered, not lost: the finishing text goes into
                    # the thread first, then the instruction it must now honour.
                    drained, inbox_error = self._consume_durable_steering(
                        session, res, rid, turn,
                        prelude={"role": "assistant", "content": comp.text})
                    if inbox_error:
                        res.error = inbox_error
                        res.turns = turn + 1
                        break
                    if drained:
                        res.turns = turn + 1
                        continue

                # white-flag guard (voluntary finish): the model says done but the tree holds
                # ZERO net changes after edits happened — it reverted itself (sphinx-10435).
                if (self.force_edit and did_edit and rollback_rounds < 1 and best_diff
                        and _has_next_turn(turn) and _tree_empty(self.cwd)):
                    session["messages"].append({"role": "assistant", "content": comp.text})
                    session["messages"].append({"role": "user", "content": ROLLBACK_NUDGE,
                        "source": "harness", "kind": "rollback_reminder"})
                    rollback_rounds += 1
                    res.turns = turn + 1
                    continue

                stop_hook = self._hook("Stop", {
                    "run_id": rid, "task_id": task_id, "turn": turn,
                    "answer": comp.text or "", "did_edit": did_edit,
                    "edited_files": sorted(edited_files),
                    "verification_passed": self._repro_verified(
                        did_edit, last_edit_turn, last_repro_turn,
                        last_repro_failed, last_repro_asserted),
                }, subject=self.project)
                if stop_hook is not None and not stop_hook.allowed:
                    reason = stop_hook.reason or "completion policy says work remains"
                    if _has_next_turn(turn) and hook_stop_rounds < 3:
                        session["messages"].append({"role": "assistant", "content": comp.text})
                        session["messages"].append({"role": "user", "source": "harness",
                            "kind": "completion_reminder", "content":
                            "A trusted completion hook blocked stopping: %s\n"
                            "Address it with evidence, then try to finish again." % reason})
                        hook_stop_rounds += 1
                        res.turns = turn + 1
                        continue
                    res.error = "completion blocked by lifecycle hook: %s" % reason

                answer = comp.text
                res.turns = turn + 1
                break
            else:
                turns_exhausted = bool(turn_cap)

            # mechanical white-flag restore (the belt to ROLLBACK_NUDGE's braces): every rescue
            # is spent and the tree is STILL empty — put the last non-empty edit state back.
            # A wrong patch can score at eval; an empty one is a guaranteed zero.
            if (not canceled and self.force_edit and did_edit and best_diff
                    and _tree_empty(self.cwd)):
                ok = _apply_diff(self.cwd, best_diff)
                self.recorder.log_turn(rid, res.turns, "rollback",
                                       "empty tree at finish — restored last non-empty diff "
                                       "(%d B): %s" % (len(best_diff), "ok" if ok else "FAILED"),
                                       0, 0, 0, 0)
                self._emit("rollback", ok=ok, size=len(best_diff))
                if ok:
                    # Restoring a prior patch is itself a new mutation. Evidence collected before
                    # the restore cannot certify the bytes we just put back.
                    last_edit_turn = max(last_edit_turn, res.turns)
                    last_repro_turn, last_repro_failed, last_repro_asserted = -100, False, False

            if canceled:
                answer = ((answer.rstrip() + "\n\n") if answer else "") + "_[stopped by user]_"
            elif not answer:
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
                elif (budget_hit or self._over_budget(total)
                      or (self.shared_budget is not None and self.shared_budget.exceeded())):
                    # Don't spend MORE past either the local ceiling or Pack's aggregate ceiling on
                    # a cosmetic synthesis call after useful work has already happened.
                    budget_hit = True
                    answer = "(stopped at budget — see the edits/tools above)"
                elif (not getattr(self, "max_model_calls", 0) or
                      model_calls < int(self.max_model_calls)):
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
                            session, safe_user_msg, self.cwd, self.project, self.mode)
                        # ...and tell the model that this is the summary turn and why the run
                        # stopped. Without it the model reads an ordinary working thread and
                        # answers with its next tool call (see final_summary_instruction).
                        # Appended to the projection only — session["messages"] keeps the real
                        # transcript, and the extra message costs no additional request.
                        msgs2 = list(msgs2) + [{
                            "role": "user", "source": "harness", "kind": "final_summary",
                            "content": final_summary_instruction(turns_exhausted, turn_cap)}]
                        from .cancellation import complete as complete_cancelable
                        def _synthesis_text(piece):
                            interrupt_partial.append(piece)
                            if self.stream_cb:
                                self.stream_cb(piece)
                        fin = complete_cancelable(
                            self.provider, _sys2, msgs2, [], on_text=_synthesis_text,
                            cancelled=self._cancel_requested)
                        self._account_usage(total, fin.usage)
                        # A synthesis the provider refused to issue (denied reservation) is the
                        # one that inflated a 48-request run's receipt to 49. Count what the
                        # request gate actually let through; the error below still stands.
                        model_calls += issued_requests(fin)
                        if self._cancel_requested():
                            canceled = True
                            answer = (fin.text or "".join(interrupt_partial)).strip()
                            answer = ((answer + "\n\n") if answer else "") + "_[stopped by user]_"
                        elif fin.stop_reason == "error":   # don't let a failed synthesis become the answer
                            res.error = res.error or (fin.text or "provider error")[:300]
                            answer = _placeholder
                        elif getattr(fin, "tool_calls", None):
                            # An action request is not a final report, including prose promising
                            # to perform it. No tools dispatch here and no extra retry is bought.
                            answer = (_unfinished if turns_exhausted else
                                      "(stopped without a final summary — see the tools above)")
                        else:
                            answer = (fin.text or "").strip() or _placeholder
                        del interrupt_partial[:]
                    except Exception:
                        if self._cancel_requested():
                            canceled = True
                            answer = "".join(interrupt_partial).strip()
                            answer = ((answer + "\n\n") if answer else "") + "_[stopped by user]_"
                        else:
                            answer = _placeholder
                else:
                    budget_hit = True
                    answer = "(stopped at model-call budget — see the edits/tools above)"
            if budget_hit and answer:
                # Name the ceiling that was actually crossed, and only when it was: budget_hit
                # also covers the model-call cap and Pack's aggregate, and a receipt that
                # invented a number for those would be worse than one that stayed general.
                ceiling = (self._active_limits.ceiling_text()
                           if (self._active_limits is not None
                               and self._over_budget(total)) else "")
                answer += "\n\n_[stopped: budget ceiling reached%s]_" % (
                    (" — " + ceiling) if ceiling else "")
            if turns_exhausted and answer and "ran out of turns" not in answer:
                # The cost ceiling has always said so; the turn ceiling never did, so a summary
                # written mid-task read as a finished report — including when no check had run.
                answer += ("\n\n_[stopped: ran out of turns (%d) — this task was NOT finished, and "
                           "nothing above was necessarily verified]_" % turn_cap)
            # A turn ceiling is a normal, predeclared product outcome, not a transport/provider
            # fault. Surface it structurally so an evaluator can classify the attempt as a valid
            # unresolved result even if a partial patch was left behind.
            res.turns_exhausted = turns_exhausted
            if last_stop == "length" and answer and "truncated" not in answer:
                answer += "\n\n_[answer truncated at output-token limit]_"   # visible half of point 1

            # Compute the verdict before persistence. Required gets a bounded number of repair
            # turns, but exhausting that retry allowance must be a hard FAILED result rather than
            # silently accepting the model's next "done". Keep the partial answer for diagnosis and
            # mark it explicitly so a resumed/saved thread cannot remember it as a success.
            res.verified = self._repro_verified(
                did_edit, last_edit_turn, last_repro_turn,
                last_repro_failed, last_repro_asserted)
            if (self.verify_gate and did_edit and not res.verified
                    and not canceled and not res.error):
                evidence = "executed post-edit assertion" if self.require_assert else \
                           "executed post-edit check"
                res.error = "verification required but no %s passed" % evidence
                marker = "_[run failed: %s]_" % res.error
                if marker not in answer:
                    answer = (answer.rstrip() + "\n\n" + marker).lstrip()

            res.answer = answer
            # Never consolidate MOCK runs — their canned "Based on the tool output: …" answers are
            # test plumbing, not durable facts, and were polluting memory.db on every selftest.
            # Also skip a length-stopped answer: an incomplete "fact" shouldn't enter durable memory.
            # A model-authored summary is a CLAIM, not ground truth.  It stays outside recall until
            # the host verification boundary promotes it.  This is deliberately fail-closed for
            # older/custom memory adapters: lacking a proposal lifecycle means no durable write.
            if (not canceled and not res.error and consolidate and answer
                    and getattr(self.provider, "name", "") != "mock"
                    and last_stop != "length"):
                propose = getattr(self.memory, "propose", None)
                if callable(propose):
                    claim_id = propose(
                        text="Task '%s' -> %s" % (task_id, answer[:200]),
                        keys=task_id, project=self.project,
                        source="run_consolidation",
                        provenance={"run_id": rid, "task_id": task_id,
                                    "provider": getattr(self.provider, "name", ""),
                                    "model": getattr(self.provider, "model", "")},
                        scope=self.project)
                    if claim_id is not None and int(claim_id) >= 0:
                        res.memory_claim_ids.append(int(claim_id))
                        # The in-loop gate is evidence too: edits followed by a fresh passing repro
                        # can be learned immediately. External checks settle remaining proposals.
                        if res.verified and not self.defer_memory_promotion:
                            self.memory.promote(
                                claim_id, status="verified",
                                evidence={"kind": "post_edit_repro", "run_id": rid},
                                source="verification_gate",
                                provenance={"run_id": rid, "task_id": task_id})
        except KeyboardInterrupt:
            # Ctrl-C is a STOP, not a crash.  Unwinding out of run() used to leave
            # each surface holding its pre-turn history, so completed edits vanished
            # from the conversation while their effects stayed on disk.  Take the
            # ordinary canceled ending instead: the finalization below closes the
            # transcript, keeps the recovery fence, and returns a usable result.
            canceled = True
            res.error = res.error or "interrupted by user"
            self._emit("canceled", at="interrupt")
            # A delegated child finishes its own books below, then lets the
            # interrupt continue to the run the person was actually watching.
            interrupted_child = bool(getattr(self, "delegation_depth", 0))
            partial = "".join(interrupt_partial).strip()
            if partial and not answer:
                answer = partial
            answer = ((answer.rstrip() + "\n\n") if answer else "") + "_[stopped by user]_"
            # The normal ending assigns this inside the try we just left. A stop still
            # owes the caller whatever answer text the run had actually produced.
            res.answer = answer
        except Exception as e:
            res.error = "%s: %s" % (type(e).__name__, e)

        res.input_tokens = total.input_tokens
        res.model_calls = model_calls
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
        res.canceled = canceled
        res.budget_exhausted = budget_hit
        # The ceilings this run was actually held to, so a receipt can say what "budget" meant
        # here rather than making a reader guess from whatever the panel says afterwards.
        if self._active_limits is not None:
            res.budget_limits = dict(self._active_limits.values(),
                                     source=self._active_limits.source)
        res.edited = did_edit
        # Keep this assignment before finish_run: recorder implementations/adapters are allowed to
        # inspect the complete result synchronously, and previously always observed the dataclass's
        # default False even on a verified run.
        res.verified = self._repro_verified(
            did_edit, last_edit_turn, last_repro_turn,
            last_repro_failed, last_repro_asserted)
        if res.error:
            # Errors can originate below the ordinary tool-output redaction
            # boundary (provider bodies, hooks, persistence, custom tools).
            # They are receipts/status, never executable input, so always apply
            # structural credential redaction even when model-input redaction
            # was explicitly disabled.
            res.error = _redact.redact(str(res.error), self._secret_vault)[:4_000]
            res.success = False
        from .recorder import note_host_error, run_stop_reason
        res.stop_reason = "output_limit" if last_stop == "length" else "completed"
        res.stop_reason = run_stop_reason(res)
        res.success = res.stop_reason == "completed"
        # ensure the thread ENDS with the final answer (the no-tool-call path breaks without
        # appending it) so a --continue'd next turn sees what this turn concluded.
        m = session["messages"]
        # A stopped run can end holding tool_use blocks that never got a result.
        # Close them honestly BEFORE the answer, so this thread stays valid for
        # the next turn (in this process or after --resume) without any call
        # being described as more finished, or more untouched, than it was.
        self._close_unanswered_calls(m, journal_state, journal_detail)
        if answer and not (m and m[-1].get("role") == "assistant" and m[-1].get("content") == answer):
            m.append({"role": "assistant", "content": answer})
        res.messages = m                      # expose the thread so a session can be saved/continued
        # Persist first so the recorded outcome includes any final journal failure.
        if journal_state in ("executing_tool", "external_action"):
            # The tool may have committed its effect before the process/host code
            # failed. Preserve the fence for explicit reconciliation.
            recovery_detail = dict(journal_detail)
            recovery_detail["error"] = res.error
            from . import sessions as _sessions
            # An interrupted host-attested built-in read cannot have changed
            # anything, and demanding inspection for it would strand the thread.
            # Keep that boundary auto-resumable; everything else stays fenced.
            fence_state = ("executing_tool"
                           if _sessions.replay_safe_boundary(journal_state, journal_detail)
                           else "external_action")
            final_saved = self._session_checkpoint(m, rid, res.turns, fence_state,
                                                   recovery_detail, terminal=False)
            unsaved = ("the crash-recovery fence for this run could not be persisted; "
                       "the interrupted call must be reconciled by hand")
        else:
            final_saved = self._session_checkpoint(m, rid, res.turns, "terminal",
                                                   {"error": res.error,
                                                    "verified": res.verified},
                                                   terminal=True)
            # Earlier checkpoints may survive, and the last turn may already have effects.
            # Report the missing final save without recommending a blind retry.
            unsaved = ("the final transcript save failed, so the latest progress may be "
                       "missing from the saved thread; check the saved session and what "
                       "the last turn actually did before retrying it")
        if final_saved is False:
            # Preserve the real answer but do not report an unsaved run as completed.
            note_host_error(res, unsaved)
            res.error = _redact.redact(str(res.error), self._secret_vault)[:4_000]
            res.stop_reason = run_stop_reason(res)
            res.success = False
        elif durability_fault and journal_state == "tool_complete":
            # The store healed in time for the terminal save, which pops the fence: the
            # whole thread, including the receipt lost mid-turn, is on disk. Say so rather
            # than send a person to reconcile a healthy store. The run still stopped here,
            # so the host-error classification and this error's precedence are unchanged.
            note_host_error(res, "the final save then succeeded, so the saved session does "
                                 "hold this run's latest results and no recovery fence is "
                                 "left open")
        self.recorder.finish_run(res)
        # final receipt — the honest token/time/$ tally + the verification verdict, for the
        # streaming UX / editor / ACP surfaces (the "$" the brand promises, now on the wire).
        # verified = edited + a repro ran on the FIXED code + it didn't fail + (in assert-mode) it
        # actually executed an assertion — matching the gate's own definition, so the receipt can't
        # claim "verified" for a print-only repro under require_assert. Same verifier.py decision
        # as the finish gate, so the receipt can never disagree with why the run was allowed to stop.
        self._emit("receipt", verified=res.verified, stop_reason=res.stop_reason,
                   completed=res.stop_reason == "completed", parent_run_id=res.parent_run_id,
                   prefix_tokens=res.prefix_tokens, prefix_measured=res.prefix_measured,
                   input_tokens=res.input_tokens,
                   output_tokens=res.output_tokens, total_tokens=res.total_tokens,
                   turns=res.turns, tool_calls=res.tool_calls,
                   wall_ms=res.wall_ms, cost_usd=res.cost_usd,
                   cache_waste_usd=res.cache_waste_usd, cache_misses=miss_n, error=res.error,
                   canceled=canceled)
        self._hook("SessionEnd", {
            "run_id": rid, "task_id": task_id, "success": bool(res.success and not res.error),
            "verified": bool(res.verified), "error": res.error,
            "turns": res.turns, "wall_ms": res.wall_ms, "cost_usd": res.cost_usd,
        }, subject=self.project)
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
        if interrupted_child:
            # Books closed: usage accounted, receipt emitted, journal written. Now
            # the stop reaches the parent, which owns the surface the user stopped.
            from .delegate import DelegatedInterrupt
            raise DelegatedInterrupt(res)
        return res

    def settle_run_memory(self, res: RunResult, passed: bool, evidence=None,
                          source: str = "external_verification") -> dict:
        """Accept/reject pending run claims after a host-side verification command.

        CLI/web checks run outside ``run()``, so proposal ids travel on RunResult. Repeated
        settlement is safe because memory lifecycle methods only transition pending proposals.
        The ids themselves are untrusted transport data: only a run-consolidation proposal whose
        immutable producer identity exactly matches this run may cross the verification boundary.
        """
        promoted = rejected = 0
        evidence = self._safe_memory_evidence(evidence)
        raw_run_id = getattr(res, "run_id", 0)
        if (not isinstance(raw_run_id, int) or isinstance(raw_run_id, bool)
                or raw_run_id <= 0 or raw_run_id > 9_223_372_036_854_775_807):
            return {"promoted": 0, "rejected": 0}
        run_id = raw_run_id
        task_id = getattr(res, "task_id", "")
        provider = getattr(res, "provider", "")
        model = getattr(res, "model", "")
        if (not isinstance(task_id, str) or not task_id
                or not isinstance(provider, str)
                or not isinstance(model, str)):
            return {"promoted": 0, "rejected": 0}
        project = str(getattr(self, "project", "") or "")
        boundary = {"project": project, "scope": project}
        claim_boundary = getattr(self.memory, "claim_boundary", None)
        if callable(claim_boundary):
            try:
                candidate = claim_boundary(project)
            except (TypeError, ValueError):
                return {"promoted": 0, "rejected": 0}
            if (not isinstance(candidate, dict)
                    or not isinstance(candidate.get("project"), str)
                    or not isinstance(candidate.get("scope"), str)
                    or not candidate["project"] or not candidate["scope"]):
                return {"promoted": 0, "rejected": 0}
            boundary = candidate
        producer = {
            "run_id": run_id,
            "task_id": task_id,
            "provider": provider,
            "model": model,
        }
        producer_text = json.dumps(
            producer, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if not task_id or not project:
            return {"promoted": 0, "rejected": 0}
        raw_claim_ids = getattr(res, "memory_claim_ids", None) or []
        if isinstance(raw_claim_ids, (str, bytes)):
            return {"promoted": 0, "rejected": 0}
        try:
            raw_claim_ids = list(raw_claim_ids)
        except TypeError:
            return {"promoted": 0, "rejected": 0}
        get_claim = getattr(self.memory, "get_claim", None)
        if not callable(get_claim):
            return {"promoted": 0, "rejected": 0}
        review_provenance = dict(producer, project=project)
        seen = set()
        for raw_claim_id in raw_claim_ids:
            # Do not coerce floats, booleans, or arbitrary objects into another
            # claim's integer primary key.
            if isinstance(raw_claim_id, bool):
                continue
            if isinstance(raw_claim_id, int):
                claim_id = raw_claim_id
            elif isinstance(raw_claim_id, str):
                digits = raw_claim_id.strip()
                if not digits.isascii() or not digits.isdigit():
                    continue
                digits = digits.lstrip("0") or "0"
                if len(digits) > 19:
                    continue
                try:
                    claim_id = int(digits)
                except (ValueError, OverflowError):
                    continue
            else:
                continue
            if claim_id <= 0 or claim_id > 9_223_372_036_854_775_807 \
                    or claim_id in seen:
                continue
            seen.add(claim_id)
            try:
                claim = get_claim(claim_id)
            except (TypeError, ValueError, OverflowError):
                continue
            if not claim or claim.get("status") != "proposed":
                continue
            if (claim.get("project") != boundary["project"]
                    or claim.get("scope") != boundary["scope"]
                    or claim.get("source") != "run_consolidation"
                    # Run consolidation writes deterministic JSON.  Exact text
                    # comparison also rejects duplicate JSON keys or alternate
                    # scalar types that a permissive parser could normalize.
                    or claim.get("provenance") != producer_text):
                continue
            if passed:
                promoted += int(bool(self.memory.promote(
                    claim_id, status="verified", evidence=evidence, source=source,
                    provenance=review_provenance)))
            else:
                # A failed verifier rejects only this run's still-pending
                # proposal.  It must never invalidate an already accepted fact
                # merely because an id was replayed or forged into RunResult.
                rejected += int(bool(self.memory.reject(
                    claim_id, evidence=evidence, source=source,
                    provenance=review_provenance)))
        return {"promoted": promoted, "rejected": rejected}

    @staticmethod
    def _safe_memory_evidence(evidence):
        """Keep verification receipts useful without persisting stdout, paths, or secrets."""
        if not isinstance(evidence, dict):
            return str(evidence or "")[:500]
        allowed = (
            "kind", "passed", "command_passed", "exit_code", "timestamp", "duration_ms",
            "ran_after_last_edit", "freshness", "source", "snapshot_kind", "executed",
            "working_tree_changed_during_check", "run_id",
        )
        safe = {key: evidence.get(key) for key in allowed if key in evidence}
        command = str(evidence.get("command") or "")
        if command:
            safe["command_sha256"] = hashlib.sha256(command.encode(
                "utf-8", "replace")).hexdigest()
        return safe
