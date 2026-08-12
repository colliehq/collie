"""SWE-bench runner — the credible eval. Two phases:

  PREDICTION (no Docker): clone repo @ base_commit, run an agent on the
    problem_statement, `git add -A && git diff --cached` -> model_patch.
  EVALUATION (Docker): the official `swebench.harness.run_evaluation` builds a
    per-instance image, applies the patch + gold test_patch, runs
    FAIL_TO_PASS/PASS_TO_PASS, emits resolved/unresolved.

Same official evaluation for collie and for Claude Code — the only difference is
who produces the patch. The agent is given ONLY the problem_statement + the clean
repo (never the gold `patch`/`test_patch`).

Docker: use the native WSL engine (`docker-ce` as a systemd service) — a stable,
docker-group-owned `/var/run/docker.sock`, no Docker Desktop dependency. If the login
shell pre-dates the group add, wrap calls in `sg docker -c "..."`. See docs/SWEBENCH.md.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import tempfile
import time

from . import plat


def _git(args, cwd=None, capture=False, check=True, timeout=600):
    return subprocess.run(["git"] + args, cwd=cwd, text=True, check=check,
                          capture_output=capture, timeout=timeout)


def _git_net(args, cwd=None, timeout=1800, tries=5, base=6):
    """A git op that touches the network — retry with exponential backoff so a TRANSIENT DNS/
    connectivity blip (WSL's NAT resolver drops intermittently; an overnight run hits several)
    doesn't nuke the instance. 6/12/24/48s between tries; raises the last error after `tries`."""
    last = None
    for i in range(tries):
        try:
            return _git(args, cwd=cwd, timeout=timeout)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            last = e
            if i < tries - 1:
                time.sleep(base * (2 ** i))
    raise last


# One bare mirror per unique repo, built once (network) then reused (file://) — a 100-instance /
# 7-repo run re-cloned each repo ~14x over the network, slow AND fragile to DNS blips. The mirror
# concentrates ALL network into a one-time warmup; every instance then prepares from local disk.
_MIRROR_DIR = os.path.expanduser("~/.collie/swe_mirrors")


def ensure_mirror(repo: str) -> str:
    """Local bare mirror path for `repo`; clone it (network, retried) if absent. Atomic via a
    .tmp+rename so a killed clone never leaves a half-mirror that looks complete."""
    path = os.path.join(_MIRROR_DIR, repo.replace("/", "__") + ".git")
    if os.path.isdir(os.path.join(path, "objects")):
        return path
    os.makedirs(_MIRROR_DIR, exist_ok=True)
    tmp = path + ".tmp"
    plat.rmtree(tmp)
    _git_net(["clone", "--mirror", "--quiet", f"https://github.com/{repo}.git", tmp])
    os.replace(tmp, path)
    return path


def prepare_repo(repo: str, base_commit: str, workdir: str):
    """Working tree at base_commit with FULL past history but ZERO future history and no remote.

    A plain `git clone` keeps every ref + the whole DAG — the gold fix is usually already in
    origin/main, so an agent can literally `git log --all` / `git diff HEAD..origin/main` its way
    to the answer (SWE-rebench caught Claude Code doing exactly this, and it re-fetched once they
    stripped refs — hence also removing the remote). We fetch the base commit's ANCESTRY ONLY
    (`git fetch origin <sha>`): past context for legitimate archaeology, future objects absent.
    Source is a LOCAL bare mirror (file://) so the run is network/DNS-independent after warmup;
    the mirror holds every ref but the instance fetches one SHA and drops the remote, so the
    anti-cheat guarantee (no future history, no remote to re-fetch) is fully preserved."""
    if not base_commit or len(base_commit) < 7:
        raise ValueError("prepare_repo: bad base_commit %r" % base_commit)
    src = ensure_mirror(repo)                     # local path; network only on first build
    os.makedirs(workdir, exist_ok=True)
    _git(["init", "--quiet", workdir])
    _git(["remote", "add", "origin", src], cwd=workdir)
    try:
        # ancestry-only fetch of the exact SHA (works against a local mirror for any object)
        _git(["fetch", "--quiet", "origin", base_commit], cwd=workdir, timeout=300)
        _git(["checkout", "--force", "--quiet", "FETCH_HEAD"], cwd=workdir)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # local full fetch (fast — no network) + ref strip, for the rare SHA the direct want misses
        _git(["fetch", "--quiet", "origin"], cwd=workdir)
        _git(["checkout", "--force", "--quiet", base_commit], cwd=workdir)
        refs = _git(["for-each-ref", "--format=%(refname)"], cwd=workdir, capture=True).stdout
        for ref in refs.split():
            _git(["update-ref", "-d", ref], cwd=workdir, check=False)
        _git(["reflog", "expire", "--expire=all", "--all"], cwd=workdir, check=False)
    _git(["remote", "remove", "origin"], cwd=workdir, check=False)
    _git(["config", "core.fileMode", "false"], cwd=workdir)
    # tail guard: HEAD must BE the base commit — a silent drift here (bad sha, failed checkout)
    # would hand the agent a future tree and poison the whole run's credibility
    head = _git(["rev-parse", "HEAD"], cwd=workdir, capture=True).stdout.strip()
    if not head.startswith(base_commit) and not base_commit.startswith(head):
        raise RuntimeError("prepare_repo: HEAD %s != base_commit %s" % (head[:12], base_commit[:12]))


_JUNK_PATHSPEC = [":!venv", ":!.venv", ":!env", ":!**/__pycache__", ":!**/*.egg-info",
                  ":!**/*.dist-info", ":!**/site-packages", ":!*.pyc",
                  ":!.aider*", ":!.goose*", ":!*.orig", ":!.opencode*", ":!node_modules"]


def make_patch(workdir: str, max_len: int = 200_000) -> str:
    """Diff the agent's edits, INCLUDING new source files, but excluding junk.

    `git add -A` alone once captured a whole `venv/` from a stray `pip install` (a 12MB,
    1164-file "patch"). Plain `git add -u` fixed that but silently DROPS new source files
    a correct multi-file fix may create. So: `git add -A` with pathspec-excludes for
    venv/__pycache__/*.egg-info/etc., plus a size backstop that falls back to tracked
    *.py hunks only if something still bloated the diff.
    """
    _git(["add", "-A", "--", "."] + _JUNK_PATHSPEC, cwd=workdir, check=False)
    patch = _git(["diff", "--cached"], cwd=workdir, capture=True).stdout
    if len(patch) > max_len:
        print("  WARN make_patch: %d bytes (>%d) — restricting to *.py hunks"
              % (len(patch), max_len), flush=True)
        _git(["reset", "-q"], cwd=workdir, check=False)
        _git(["add", "-u", "--", "*.py"], cwd=workdir, check=False)
        patch = _git(["diff", "--cached"], cwd=workdir, capture=True).stdout
    return patch


# ---- language detection for the verify gate ----------------------------------------------
# Every nudge below used to hardcode `python3 -c`. On a Go or JS repo that instruction is not
# merely useless, it is misleading: the agent runs some Python, nothing raises, and the gate lets
# it finish. SWE-bench Pro is 280 go / 266 python / 165 js / 20 ts, so the gate was INERT on 445
# of 731 instances — and it showed. On flipt the patch applied and the test package then failed
# to COMPILE (undefined: NewClient), zero tests ran, and collie had declared itself done.
#
# So the gate is now built per language, and for anything that compiles or typechecks the FIRST
# obligation is that it still builds. A fix that does not build cannot be right, and unlike
# behavioural correctness that is cheap and unambiguous to check.
_LANG_MARKERS = [
    ("go", ("go.mod",)),
    ("rust", ("Cargo.toml",)),
    ("java", ("pom.xml", "build.gradle", "build.gradle.kts")),
    ("js", ("package.json",)),                       # incl. ts; refined below
    ("python", ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini")),
]


def detect_language(workdir: str) -> str:
    """Best-effort repo language. Unknown is an ANSWER, not a default to python — a wrong guess
    reinstates exactly the bug this exists to fix."""
    for lang, markers in _LANG_MARKERS:
        for m in markers:
            if os.path.exists(os.path.join(workdir, m)):
                if lang == "js" and (os.path.exists(os.path.join(workdir, "tsconfig.json"))):
                    return "ts"
                return lang
    return ""


# Per language: (build/typecheck command, how to run a throwaway assertion).
_LANG_VERIFY = {
    "go": ("go build ./... && go vet ./...",
           "write a temporary `xx_verify_test.go` in the package you changed, run it with "
           "`go test -run '^TestXxVerify$' ./path/to/pkg`, and DELETE the file before you finish"),
    "rust": ("cargo check",
             "add a `#[test]` in the module you changed, run `cargo test <name>`, then remove it"),
    "java": ("mvn -q -o compile || ./gradlew compileJava",
             "run the single most relevant existing test class for the code you changed"),
    "ts": ("npx tsc --noEmit",
           "run the single most relevant existing test file with the project's runner "
           "(check package.json scripts — usually `npx jest <file>` or `npx vitest run <file>`)"),
    "js": ("node --check the files you edited",
           "run the single most relevant existing test file with the project's runner "
           "(check package.json scripts — usually `npx jest <file>` or `npx mocha <file>`)"),
    "python": ("python3 -c 'import <the package you changed>'",
               "run a SHORT `python3 -c \"...\"` that constructs the minimal scenario from the "
               "issue and asserts the correct result"),
}


def _verify_instructions(lang: str) -> str:
    """The language-specific half of the verify nudge. Empty when the language is unknown —
    saying nothing beats telling a Go agent to run python3."""
    spec = _LANG_VERIFY.get(lang)
    if not spec:
        return ("Use whatever the repo's own toolchain is to check your change: first that it "
                "still BUILDS/typechecks, then that it behaves correctly on the issue's scenario.")
    build, exercise = spec
    return ("This repo is %s. FIRST make sure your change still builds: run `%s` and fix every "
            "error it reports — a patch that does not compile cannot be correct, and this is the "
            "single most common way a plausible-looking fix is worthless. THEN exercise the fix: "
            "%s." % (lang, build, exercise))


# ---- prediction agents (each: edit files in workdir, given problem_statement) ----
# Ported from Hermes' loop: after editing, TEST the fix by running it (not the suite).
_SWE_VERIFY_NUDGE = (
    "Before you finish: SANITY-CHECK your fix. Use the bash tool to run a SHORT "
    "`python3 -c \"...\"` that reproduces the issue's scenario and prints the result "
    "(construct the minimal object/call from the issue). Do NOT run pytest or the full "
    "test suite — the env isn't set up for it and a separate grader runs the tests. "
    "If it raises or the output is still wrong, read the traceback, FIX the code with "
    "edit_file, and re-check. ITERATE — repair and re-run — until your reproduction prints "
    "the CORRECT result; only then finish.")

# ASSERT-based verify (diagnosis 2026-07-10): collie hits the RIGHT file but makes a
# plausible WRONG edit and finishes — its wrong edits don't raise, they emit wrong OUTPUT, so
# an "eyeball the printout / no-traceback" check passes them. Force the model to encode the
# expected result as an EXECUTABLE ASSERTION so a wrong fix fails loudly (AssertionError) and
# the gate drives another repair round. `expected` must be derived from the ISSUE (it usually
# states or shows the correct behavior), not guessed — a wrong `expected` would iterate toward
# a wrong fix (the failure mode of the old blind repair loop).
# A single fix-confirming assertion (reverted from a bidirectional edge+neighbor version). The
# bidirectional push was measured NET-HARMFUL on rebench 2026_03: pairing the assert-neighbor /
# assert-edge instructions with a hardened finish-gate drove SCOPE CREEP — on a fix that was
# already correct the model kept editing (extra files, bigger diffs) to satisfy the broader
# verification and broke it. Control (old-harness resample of the v2 regressions) confirmed 4/8
# regressions were genuine v2 damage. Keep verification focused on the ONE thing it reliably
# helps: does the fix produce the correct result. (See docs/SWE_AUDIT.md verify-loop section.)
_SWE_ASSERT_VERIFY_NUDGE = (
    "Before finishing you MUST verify with an EXECUTABLE ASSERTION, not by eyeballing output "
    "(a wrong fix often prints plausible-but-wrong text without raising). Use the bash tool: "
    "`python3 -c \"...\"` that (1) constructs the minimal scenario from the issue, (2) computes "
    "the actual result, and (3) runs `assert actual == expected, (actual, expected)` where "
    "`expected` is the CORRECT result the issue states or implies. Do NOT run pytest/the suite. "
    "If it raises AssertionError, your fix produces the wrong result — read the (actual, expected), "
    "FIX the code with edit_file, and RE-RUN the SAME assertion. Only finish once the assertion "
    "passes with no error.")

_SWE_ASSERT_REPAIR_NUDGE = (
    "Your assertion FAILED after the edit — the fix produces the wrong result (see the "
    "AssertionError's (actual, expected) above). Do not merely re-run: reconsider WHAT the "
    "correct change is (the current edit's logic is wrong or incomplete), FIX it with edit_file, "
    "then RE-RUN the SAME assertion. If instead the assertion itself is wrong (bad `expected`), "
    "correct the `expected` to match the issue and re-run. Finish only when it passes cleanly.")


# The three nudges above are tuned for Python and stay VERBATIM on Python repos — they were
# measured there (assert-verify promoted on a 12-instance holdout) and rewording them would
# silently re-run that experiment. For every other language the Python-specific mechanics are
# replaced by the repo's own toolchain, build-first.
def _swe_verify_nudge(lang: str) -> str:
    if lang == "python":
        return _SWE_VERIFY_NUDGE
    return ("Before you finish: SANITY-CHECK your fix. %s Do NOT run the full test suite — a "
            "separate grader runs the tests. If the build fails or the behaviour is still wrong, "
            "read the error, FIX the code with edit_file, and re-check. ITERATE until it builds "
            "clean AND your check shows the CORRECT result; only then finish."
            % _verify_instructions(lang))


def _swe_assert_verify_nudge(lang: str) -> str:
    if lang == "python":
        return _SWE_ASSERT_VERIFY_NUDGE
    return ("Before finishing you MUST verify by EXECUTING something, not by eyeballing your diff "
            "or the code (a plausible-looking edit is exactly what a wrong fix looks like). %s "
            "State the expected result from what the ISSUE says, and make the check FAIL LOUDLY "
            "if the actual result differs. Do NOT run the whole test suite. If the build fails or "
            "the check does not match, reconsider WHAT the correct change is — do not merely "
            "re-run — fix it and re-check. Finish only when the build is clean and the check "
            "passes." % _verify_instructions(lang))


_CRITIC_SYS = (
    "You are an INDEPENDENT senior reviewer doing DUE DILIGENCE on a candidate bug-fix. The repo in "
    "your working dir ALREADY HAS the candidate diff applied. You did NOT see the author's reasoning "
    "— form your OWN view from the issue + the code. You have READ-ONLY tools (read_file, grep, "
    "code_search, bash for read-only inspection). Determine whether the fix COMPLETELY and CORRECTLY "
    "does what the issue requires. Investigate ACTIVELY with grep/read/code_search (prefer these — "
    "they are fast and reliable) — especially: grep the changed function/symbol/pattern and check "
    "whether it appears in OTHER files or call-sites that were NOT changed but SHOULD have been "
    "(under-coverage is the #1 miss); check the edge/default cases the issue implies. Do NOT modify "
    "any files. You have a limited turn budget — investigate efficiently and CONCLUDE. Your FINAL "
    "message MUST be your verdict in EXACTLY one of these forms: `CORRECT` (the fix genuinely and "
    "completely satisfies the issue) OR `CONCERN: <one concrete concern in 1-2 sentences naming the "
    "exact file/case/behavior that is wrong or missing>`. Do not end mid-investigation.")


def critic_backend(provider, model):
    """(provider, model) the critic should run on — a SECOND model when asked for.

    The critic is justified by "a separate read does not share the model's blind spot", but it has
    been the same model reading twice, which shares a good deal of it: a misread of the issue tends
    to repeat. COLLIE_CRITIC_PROVIDER / COLLIE_CRITIC_MODEL make the reviewer genuinely independent.

    A model name belongs to ITS provider, so naming a provider without a model falls back to that
    provider's default rather than carrying e.g. `deepseek-chat` into an Anthropic request.
    """
    want_provider = (os.environ.get("COLLIE_CRITIC_PROVIDER") or "").strip()
    want_model = (os.environ.get("COLLIE_CRITIC_MODEL") or "").strip()
    if not want_provider and not want_model:
        return provider, model
    switched = bool(want_provider) and want_provider != provider
    return (want_provider or provider,
            want_model or (None if switched else model))


def _critic_provider(provider, model):
    """Build the critic's provider, or None to reuse the author's.

    Deliberately raises rather than falling back when an explicitly requested critic backend cannot
    be built. Silently reviewing with the author's own model would look exactly like a working
    cross-model critic and would quietly turn a measurement into a tautology.
    """
    from .providers import make_provider
    critic_p, critic_m = critic_backend(provider, model)
    if (critic_p, critic_m) == (provider, model):
        return None
    try:
        return make_provider(critic_p, critic_m)
    except Exception as exc:
        raise RuntimeError(
            "critic backend %s/%s could not be built: %s. Fix it or unset "
            "COLLIE_CRITIC_PROVIDER/COLLIE_CRITIC_MODEL — falling back to the author's own model "
            "would report a cross-model critic that never happened." %
            (critic_p, critic_m or "(default)", exc)) from exc


def _spawn_investigative_critic(provider, model):
    """Return critic(issue, diff, cwd) -> (ok, objection): a FRESH read-only agent that investigates
    the codebase itself (grep/read/code_search/run_in_env) — catches under-coverage and call-site
    misses a diff-only glance cannot, while staying independent of the author's (possibly wrong) read."""
    def critic(issue, diff, cwd):
        try:
            ch = make_harness(cwd, provider=provider, model=model, project="critic", code_search=True)
            ch.max_turns = int(os.environ.get("COLLIE_CRITIC_TURNS", "14"))
            ch.self_verify = False
            ch.force_edit = False
            ch.critic = False                                   # no recursion
            for tname in ("edit_file", "write_file", "undo"):   # READ-ONLY: can't corrupt the fix
                ch.registry._tools.pop(tname, None)
            prompt = (_CRITIC_SYS + "\n\nISSUE:\n" + str(issue)[:6000] +
                      "\n\nCANDIDATE DIFF (already applied — inspect the live tree):\n" + str(diff)[:9000])
            res = ch.run("critic", prompt, consolidate=False)
            try:
                ch.memory.close(); ch.recorder.close()
            except Exception:
                pass
            text = (getattr(res, "answer", None) or getattr(res, "text", None) or "").strip()
        except Exception:
            return True, ""            # a critic failure must never block a finish
        up = text.upper().lstrip("*# `>-\n")
        # require an explicit verdict token: CONCERN: -> objection; CORRECT (or no clean verdict,
        # i.e. it ran out of budget mid-thought) -> don't block. Only a stated CONCERN gates.
        i = up.find("CONCERN:")
        if i != -1:
            return False, text[text.upper().find("CONCERN:") + 8:].strip()[:1000]
        return True, ""
    return critic


def predict_collie(workdir: str, problem_statement: str, provider="deepseek",
                   model=None, max_turns=50, benchmark_safe=False,
                   request_gate=None, request_complete=None, request_scope="",
                   complete_prompt=None, benchmark_effort="default"):
                   # 50 (was 35): the verify loop (reproduce ->
                   # edit -> re-check) needs headroom; Hermes runs to 90. Still well under.
    from .cli import make_harness
    # The verify gate is only as good as the toolchain it names, so decide the language BEFORE
    # building any nudge. COLLIE_SWE_LANG overrides it for repos the markers get wrong.
    _lang = (detect_language(workdir) if benchmark_safe else
             (os.environ.get("COLLIE_SWE_LANG") or detect_language(workdir)))
    # Env override to swap the backend model without touching call sites — e.g. to break the
    # DeepSeek resolve ceiling with the latest Opus via the subscription/CLI path:
    #   COLLIE_PROVIDER=claude-cli COLLIE_MODEL=opus  (auth via CLAUDE_CODE_OAUTH_TOKEN or
    #   ANTHROPIC_API_KEY — see ClaudeCliProvider). Legitimate first-party CLI, no proxy.
    if benchmark_safe:
        if provider not in ("claude-cli", "claude-agent-sdk"):
            raise ValueError(
                "benchmark_safe Collie requires an approved official Claude provider")
    else:
        provider = os.environ.get("COLLIE_PROVIDER", provider)
        model = os.environ.get("COLLIE_MODEL", model)
    # COLLIE_CODE_SEARCH=0 drops the semantic index entirely (agent navigates with grep/read only)
    # — the Report-B lever to measure whether embedding navigation actually changes task resolve,
    # and the lightweight config for weak machines (no ONNX embedding at all).
    _cs = (not benchmark_safe and
           os.environ.get("COLLIE_CODE_SEARCH", "1") not in ("0", "false", "off"))
    # LEAN-PROMPT (opt-in, COLLIE_LEAN_PROMPT=1): principled-lean isolation. Removes the
    # PROCEDURAL commands ("keep focused / do not expand to unrelated files", the "focused"
    # scope-shrink adjective) that over-corrected v2's scope-creep into v3's scope-SHRINK
    # (multi-file under-coverage — related_locations surfaces the sibling, but the command
    # made the model skip it). Keeps every INFORMATIONAL rule (env facts, exact-API contract,
    # verify-assert). Thesis: for a capable model, SUPPLY missing context + let it JUDGE scope;
    # multi-file coverage comes from related_locations SURFACING siblings (info), not a command.
    _lean = (not benchmark_safe and
             os.environ.get("COLLIE_LEAN_PROMPT") in ("1", "true", "on"))
    if benchmark_safe and benchmark_effort not in ("default", "high"):
        raise ValueError("benchmark_effort must be default or high")
    h = make_harness(workdir, provider=provider, model=model, project="swe",
                     effort=benchmark_effort if benchmark_safe else None,
                     code_search=_cs,           # semantic repo navigation (bge-small); env-gated
                     embed="hash")              # one-shot fix: skip loading jina-v3 for
                                                # memory (unused here) -> ~2GB less peak
    # Keep the advertised workflow and the retained tools derived from the same switches.  The
    # trusted-fixture benchmark profile deliberately has no shell/network authority and ignores
    # ambient repo rules/skills, matching the native arms' safe/ignore-rules profiles.  General SWE
    # runs retain their existing shell plus optional local search / container verifier.
    allowed = ["read_file", "write_file", "edit_file", "grep", "glob"]
    if not benchmark_safe:
        allowed.append("bash")
        if _cs:
            allowed.append("code_search")
        if os.environ.get("COLLIE_E2E_IMAGE"):
            allowed.append("run_in_env")
    h.registry.retain(allowed)
    if benchmark_safe:
        h.provider.subscription_only = True
        h.composer.auto_prefetch = False
        h.composer.include_project_rules = False
        h.composer.include_skills = False
        h.composer.identity = "You are Collie, a coding agent in a frozen evaluation."
        # Freeze every loop-level behavior that normal product settings or environment variables
        # can customize.  The ranking runner owns one total attempt budget; an ambient retry,
        # hook, overflow retry, or forced-answer threshold would make the two product arms spend
        # different numbers of model calls on the same cell.
        h.max_retries = 0
        h.retry_base = 0.0
        h.overflow_recovery = False
        h.hooks = None
        h.force_ratio = 0.55
        h.hard_ratio = 0.76
    h.max_turns = (int(max_turns) if benchmark_safe else
                   int(os.environ.get("COLLIE_MAX_TURNS", str(max_turns))))
    # VERIFY step ported from Hermes' loop (trace diff: Hermes edits once after thorough
    # exploration, then runs `python -c` to TEST the fix and iterates — collie edited then
    # finished blind, causing the 3/9 "right file, wrong edit" failures). We enable a BOUNDED
    # verify: a quick python -c reproduction, NOT pytest/the suite (env isn't set up; grader
    # runs tests separately). Opt-out with COLLIE_SWE_VERIFY=0.
    if (not benchmark_safe and
            os.environ.get("COLLIE_SWE_VERIFY", "1") not in ("0", "false", "off")):
        h.self_verify = True
        h.verify_nudge = _swe_verify_nudge(_lang)
        # Evidence-gate (default on): gate finish on an actually-run post-edit reproduction
        # that didn't error, with bounded reproduce->repair rounds. COLLIE_VERIFY_GATE=0
        # reverts to the old one-shot advisory nudge for A/B.
        # Default OFF: the evidence-gated repair loop is NET-NEUTRAL on Opus (measured full-15:
        # fixed seaborn-3069 but broke pytest-10081 via a repair on an already-correct edit;
        # requests-1724's "loss" was eval flakiness — identical patch). Forcing repair is
        # double-edged: it rescues a wrong edit but can wreck a right one when the model
        # misjudges its own reproduction. Keep it opt-in (COLLIE_VERIFY_GATE=1) for study.
        h.verify_gate = os.environ.get("COLLIE_VERIFY_GATE", "0") not in ("0", "false", "off")
        h.verify_max = int(os.environ.get("COLLIE_VERIFY_ROUNDS", "2"))
        # Multi-file coverage re-surfacing (default OFF pending A/B: a score threshold can't
        # tell a needed sibling from an incidental same-package file, so it's advisory-only).
        h.coverage_gate = os.environ.get("COLLIE_COVERAGE_GATE", "0") not in ("0", "false", "off")
        h.coverage_max = int(os.environ.get("COLLIE_COV_ROUNDS", "2"))
        h.cov_thresh = float(os.environ.get("COLLIE_COV_THRESH", "1.9"))
        # ASSERT-VERIFY (DEFAULT-ON since 2026-07-12; set COLLIE_ASSERT_VERIFY=0 to disable): the
        # redirected executed-oracle lever. Diagnosis (docs/SWE_AUDIT.md, Opus): collie localizes correctly but
        # makes plausible WRONG edits and finishes; its wrong edits emit wrong OUTPUT without
        # raising, so the old no-traceback gate passed them. This mode gates finish on an executed
        # `assert actual==expected` and drives edit-iteration until it passes — turning the model's
        # correctness judgment into a checkable signal. Differs from the net-neutral
        # COLLIE_VERIFY_GATE: (a) require_assert closes the print-only hole, (b) the repair nudge
        # says reconsider the FIX, not re-run. PROMOTED to default after the same-holdout control:
        # baseline pass@1 9/12 vs assert 10/12, direction consistent across 3 disjoint holdouts.
        if os.environ.get("COLLIE_ASSERT_VERIFY", "1") not in ("0", "false", "off"):
            h.verify_gate = True
            h.require_assert = True
            h.verify_nudge = _swe_assert_verify_nudge(_lang)
            h.repair_nudge = _SWE_ASSERT_REPAIR_NUDGE
            h.verify_max = int(os.environ.get("COLLIE_VERIFY_ROUNDS", "3"))
    else:
        h.self_verify = False
    # Adversarial critic gate (COLLIE_CRITIC=1): before finishing, an INDEPENDENT fresh read of the
    # issue attacks the diff. Ranked #1 comprehension lever (workflow) — catches under-coverage and
    # misreads that a self-nudge cannot, because a separate read does not share the model's blind spot.
    if (not benchmark_safe and
            os.environ.get("COLLIE_CRITIC") in ("1", "true", "on")):
        h.critic = True
        h.critic_issue = problem_statement
        h.critic_max = int(os.environ.get("COLLIE_CRITIC_ROUNDS", "2"))
        # Independence is only as real as the reader. Applies to the DEFAULT shallow critic too,
        # which is the one that empirically wins — not just the opt-in investigative path below.
        h.critic_provider = _critic_provider(provider, model)
        # Default = SHALLOW one-shot critic (issue+diff, forced to state ONE concrete concern). It
        # EMPIRICALLY beat the investigative version: the tool-equipped critic wanders and often ends
        # mid-investigation without a verdict, MISSING logic holes the forced-concise critic catches
        # (opensandbox: shallow flagged + flipped it; investigative said CORRECT). More context/tools
        # hurt sharpness here. Investigative is opt-in (COLLIE_CRITIC_DEEP=1) — neither catches
        # under-coverage (pygraphistry), which stays the ceiling.
        if os.environ.get("COLLIE_CRITIC_DEEP") in ("1", "true", "on"):
            h.critic_fn = _spawn_investigative_critic(*critic_backend(provider, model))
    h.force_edit = True                          # converge to an edit (no empty patches)
    # PLAN-FIRST (opt-in, COLLIE_PLAN_FIRST=1): commit the full multi-file SCOPE before
    # editing. Hypothesis (audit found DeepSeek fixes the primary file then stops even when
    # siblings are surfaced+read): enumerating every file up front, then editing against that
    # list, beats edit-then-nudge.
    # FALSIFIED — measured no coverage gain on any multi-file miss (4551 1/4, 4604 1/2,
    # seaborn-3187 1/2, same as the default hint). Together with 3 other tried strategies
    # (strong "MUST edit" wording, wider churn window, the k=8 recall bump) this confirms a
    # DeepSeek-V3 CEILING: it will not do coordinated multi-file edits regardless of harness
    # strategy. Kept OFF-by-default and gated (like the distiller) so a stronger agentic model
    # can flip it on; it does nothing for DeepSeek. See docs/SWE_AUDIT.md.
    if (not benchmark_safe and
            os.environ.get("COLLIE_PLAN_FIRST") in ("1", "true", "on")):
        workflow = (
            "Workflow: (1) use `code_search` (repeatedly, different queries) to find ALL code "
            "involved. (2) BEFORE editing anything, decide the COMPLETE set of files a correct "
            "fix must change and state it as a short list `PLAN: file1, file2, ...` — issues "
            "like this often span SEVERAL files in the same package (e.g. a value produced in "
            "one file and consumed/formatted in another). (3) Then `edit_file` EACH file in your "
            "PLAN. (4) Do not finish until every file in the PLAN is edited, or you have "
            "explicitly stated why a planned file turned out not to need the change.\n")
    elif _cs:
        # LEAN drops the "focused" adjective (a scope-shrink signal); the model judges scope.
        _fix = ("make a COMPLETE fix" if _lean else "make a focused, COMPLETE fix")
        workflow = (
            "Workflow: (1) use `code_search` to LOCATE the relevant code semantically; "
            "(2) `read_file` the file(s) around it; (3) `edit_file` to %s "
            "— handle the edge cases the issue implies (e.g. whitespace, None).\n" % _fix)
    else:
        # code_search disabled (Report B / lightweight mode): navigate with grep, not embeddings.
        _fix = ("make a COMPLETE fix" if _lean else "make a focused, COMPLETE fix")
        workflow = (
            "Workflow: (1) use `grep` (the identifiers/errors/paths the issue names) and "
            "`read_file` to LOCATE the relevant code; (2) `edit_file` to %s "
            "— handle the edge cases the issue implies (e.g. whitespace, None).\n" % _fix)
    # Real-env verification (COLLIE_E2E_IMAGE set): the local checkout has NO installed deps, so a
    # `python3 -c "import <pkg>"` check fails silently and the fix ships UNVERIFIED. run_in_env runs
    # against the real installed environment — the fix for collie's "verify is theater on SWE".
    if os.environ.get("COLLIE_E2E_IMAGE") and not benchmark_safe:
        workflow += (
            "CRITICAL — VERIFY IN THE REAL ENVIRONMENT: your local working dir has NO installed "
            "dependencies (so `import <thepackage>` fails locally and you CANNOT trust a local "
            "check). You have `run_in_env`, which runs commands against the REAL installed code with "
            "your edits applied. Before finishing you MUST: (a) reproduce the issue in run_in_env to "
            "confirm you understand it, (b) after editing, re-run there and also test the EDGE cases "
            "(None, empty, boundaries — not just the one case in the issue), (c) run the FULL existing "
            "test MODULE for the file you changed (the whole test_<module>.py, not one test) — a fix "
            "that breaks an import or any other test in that file is WRONG. ITERATE until it genuinely "
            "passes; never finish on an unverified or partial fix.\n"
            "A local `python3 -c` or `bash` assertion proves NOTHING here — the local dir has no "
            "installed dependencies, so a local check that 'passes' is meaningless. ONLY `run_in_env` "
            "counts as verification. Do NOT claim your fix is verified, and do NOT finish, without a "
            "`run_in_env` RED→GREEN plus the repo's existing test module passing.\n"
            "Reproduce at the UNIT level — call the changed function/class DIRECTLY with constructed "
            "inputs; do NOT try to boot the whole app or a live server/DB. If the code talks to an "
            "external service (DB, network, filesystem), MOCK it (a fake object / patched connection "
            "returning the values the issue describes) or reuse the repo's own test fixtures — that "
            "is faster and more reliable than standing up real infrastructure.\n"
            "REPRODUCE-FIRST: run_in_env runs your assertion/test BOTH on the original code and with "
            "your edits. A valid check must FAIL on the original code (proving it captures the bug) "
            "and PASS with your fix — RED→GREEN. If it reports 'PASSES WITHOUT YOUR FIX', your check "
            "does not reproduce the bug (you likely tested your own assumption, not the issue's actual "
            "behavior) — rewrite it to fail on the original first. Only finish after a RED→GREEN.\n"
            "ANCHOR to the issue's OWN words: build your reproduction from the EXACT inputs, signatures, "
            "names and expected outputs the issue STATES or SHOWS — verbatim (e.g. if it shows "
            "`f(protocol='foo', value_type=str)` with `{2: 'value1'}` -> `'value1'`, assert THAT exact "
            "case). Do NOT substitute your own paraphrased inputs or a data shape you find convenient — "
            "that is how a fix passes your test but fails the real one.\n"
            # NOTE: a prompt-level "DIALECTICAL GATE" (self-attack: find an uncovered case + question
            # your own test) was tried and did NOT help (pygraphistry stayed 3/4 files, opensandbox
            # stayed 1-failing). Root cause: a model told to attack ITSELF shares its own blind spot —
            # if it misread the issue, the self-attack misreads too. True adversarial needs a SEPARATE
            # agent with an independent read (the workflow's adversarial-critic, which ranked #1). That
            # is an architectural change (in-loop second agent), not a nudge. Left out on purpose.
            "ESCAPE HATCH — never burn your whole turn budget failing to reproduce: if after ~3 "
            "attempts you genuinely CANNOT build a reproduction (the bug needs a data file / fixture / "
            "service you don't have), STOP trying. Make your best-effort fix from reading the code, run "
            "the repo's EXISTING relevant test MODULE in run_in_env as your regression oracle (it's the "
            "real suite — trust it over a repro you can't build), then FINISH. A committed best-effort "
            "fix ALWAYS beats an empty patch — you MUST leave an edit; never end with no change.\n")
    # NOTE: a "COMPLETENESS" nudge (exhaustively test every default/option/propagation) was tried
    # and BACKFIRED — it drove over-engineering (wtforms 4->6 fails, pyinfra 1->2 fails on the same
    # instances). Same lesson as the v2/v3 anti-scope-creep saga: aggressive procedural nudges
    # over-correct. Left out on purpose.
    # Verify clause. Non-lean keeps the anti-scope-creep command (belt-and-suspenders after the
    # gate-hardening revert). LEAN drops it: it over-corrected into multi-file under-coverage
    # (the model was SHOWN the sibling via related_locations but the command made it skip).
    _verify = (
        "Before finishing, re-read the edited source and check the requested cases carefully. "
        "This restricted benchmark profile has no command tool; an external hidden grader will "
        "execute the contract.\n"
        if benchmark_safe else
        "Before finishing, VERIFY the fix with a `python3 -c` assertion (call your code by the "
        "names the ISSUE uses); iterate the SOURCE until it passes. Don't run the suite.\n"
        if _lean else
        "Before finishing, VERIFY the fix with a `python3 -c` assertion (call your code by the "
        "names the ISSUE uses); iterate the SOURCE until it passes. Keep the change focused — do "
        "not expand it to unrelated files or cases the issue didn't ask for. Don't run the suite.\n")
    # Full-path coverage (COLLIE_TRACE_PATH=1). The rebench gap autopsy: collie's misses are
    # UNDER-COVERAGE of the reported behavior's data path — it defines a new symbol but never wires
    # it into the existing call-site (pygraphistry), fixes the read half but not the write half of a
    # flow (pdm), or patches the loudest traceback frame in the wrong subsystem (astropy). The SAME
    # model (Opus) solves all three under pi/hermes — so this is a PROMPT gap (the lean prompt doesn't
    # push tracing the whole path), not a capability limit. Scoped to the reported path only, to
    # avoid the completeness-nudge backfire (which over-tested easy instances).
    _trace = (
        "Before finishing, trace the issue's reported behavior end-to-end. If your fix ADDS a symbol "
        "(function/class/hook/option), grep for where it must be CALLED and wire it in — a symbol "
        "defined but never invoked fixes nothing. If the behavior flows through several steps "
        "(e.g. arg-parsing -> config-load, or produce -> consume), fix EVERY step on that path, not "
        "just the first you find. Put the fix in the subsystem the reported symptom actually "
        "originates from — don't just patch the loudest frame in a traceback. Still do NOT expand "
        "beyond the reported behavior's own path (no unrelated files or cases).\n"
        if (not benchmark_safe and
            os.environ.get("COLLIE_TRACE_PATH") in ("1", "true", "on")) else "")
    # COLLIE_V1_PROMPT=1: exact original v1 prompt (HEAD, pre-regression-saga) — base + workflow +
    # no-pip guard with "reproduce encouraged", NO exact-API / env-guard / anti-scope-creep. Used to
    # test extended thinking on the PROVEN-GOOD baseline (v1 ~= hermes) without the v2/v3 confound.
    if (not benchmark_safe and
            os.environ.get("COLLIE_V1_PROMPT") in ("1", "true", "on")):
        prompt = (
            "Fix this GitHub issue by editing the repository's SOURCE code (never tests).\n"
            + workflow +
            "NEVER run `pip install`, `python -m venv`, `pip`, or the test suite (`pytest`) — "
            "the environment isn't set up for it, it wastes turns, and a separate grader runs "
            "the tests. Do not create a venv/ or download packages. A few quick "
            "`python3 -c \"...\"` checks to REPRODUCE the issue and verify your fix are "
            "encouraged; just don't run the suite.\n\nISSUE:\n" + problem_statement)
        if callable(request_gate):
            with h.provider.request_authority(
                    request_gate, request_complete, request_scope=request_scope):
                res = h.run("swe", prompt, consolidate=False)
        else:
            res = h.run("swe", prompt, consolidate=False)
        h.memory.close(); h.recorder.close()
        return res
    prompt = complete_prompt if complete_prompt is not None else (
        "Fix this GitHub issue by editing the repository's SOURCE code (never tests).\n"
        + workflow +
        "NEVER run `pip install`, `python -m venv`, `pip`, or the test suite (`pytest`) — "
        "the environment isn't set up for it, it wastes turns, and a separate grader runs "
        "the tests. Do not create a venv/ or download packages.\n"
        # Exact-API rule (rebench forensics: hats-648 renamed the issue's declared skymap_coverage
        # -> compute_skymap_coverage; reframe-3660 got a parameter name wrong; the hidden test
        # imports the DECLARED name, so any rename/typo = 0 tests even with correct logic). The
        # issue's pseudocode/signature is a CONTRACT, not a suggestion — even when it says "e.g.".
        "If the issue declares a name or signature (function, class, parameter, attribute), "
        "implement THAT exact name verbatim — never rename it or 'improve' it, even if the issue "
        "labels it pseudocode or an example.\n"
        + _trace + _verify +
        # Environment-mismatch guard (pylint-4661 audit, two findings): (a) local import
        # success is a FALSE signal — the grading container's packages differ from this
        # machine's; (b) when the fix genuinely needs a library, match the ERA/style of the
        # repo (the hidden acceptance test may hardcode the maintainer's library choice —
        # 4661's test does `import appdirs`, so a functionally-equal platformdirs fix fails).
        "IMPORTANT: your local Python is NOT the grading environment — a library that imports "
        "fine here may be missing there, so never justify a dependency by testing it locally. "
        "Strongly prefer the standard library or libraries the repo ALREADY imports. If the fix "
        "truly requires a new dependency, pick what this repo's maintainers would have used AT "
        "THIS COMMIT'S TIME (mirror the era and conventions of the codebase, not today's best "
        "practice), and add it to the packaging metadata like a maintainer would.\n\nISSUE:\n"
        + problem_statement)
    if callable(request_gate):
        with h.provider.request_authority(
                request_gate, request_complete, request_scope=request_scope):
            res = h.run("swe", prompt, consolidate=False)
    else:
        res = h.run("swe", prompt, consolidate=False)
    h.memory.close(); h.recorder.close()
    return res


# Shared instruction for the CLI agents (claude/hermes/openclaw) so the ONLY variable
# is the harness, not the wording. Same "no pip/venv/test-suite" guard collie gets.
CLI_SWE_PROMPT = (
    "Resolve this GitHub issue by editing the repository's SOURCE code in the current "
    "directory. Do not stop at a plan or merely describe changes: continue using the editing "
    "tools until the source files have actually been changed. NEVER edit test files. Make a "
    "focused, COMPLETE fix — handle the edge "
    "cases the issue implies. Do NOT run `pip install`, create a virtualenv, or run the "
    "test suite: a separate grader runs the tests. Your local Python is NOT the grading "
    "environment — never justify a dependency by testing it locally. Strongly prefer stdlib "
    "or libraries the repo already imports; if a new dependency is truly required, pick what "
    "this repo's maintainers would have used at this commit's time and update the packaging "
    "metadata like a maintainer would.\n\nISSUE:\n")


def _run_cli(cmd, workdir, extra_env=None, timeout=1800, stdin_text=None):
    """Run a headless coding-agent CLI in workdir; it edits files in place.

    `stdin_text` feeds the prompt on stdin instead of as an argv element. On Windows these CLIs
    are npm .CMD shims, so they launch through cmd.exe — and cmd.exe TREATS A NEWLINE AS END OF
    COMMAND. A multi-line prompt passed as an argument is silently cut at the first newline: the
    agent receives a fragment, says it lacks the issue body, edits nothing, and the comparison
    records a clean 0 with no error. Verified empirically: a 1007-char prompt arrived as its
    first line via argv, and complete via stdin.
    """
    env = dict(os.environ)
    for k, v in (extra_env or {}).items():
        if v is None:
            env.pop(k, None)                 # unset a shadowing key
        else:
            env[k] = v
    # Resolve argv[0] on PATH ourselves. On Windows CreateProcess does NOT consult PATHEXT, so
    # bare "claude" misses claude.cmd and raises FileNotFoundError in ~0.2s — which a comparison
    # harness happily records as "the other agent produced no patch". That exact broken arm has
    # now produced a bogus 2:0 and a bogus 10:0 in this repo. A competitor that cannot start must
    # be an ERROR, never a loss, so say which executable was not found.
    exe = shutil.which(cmd[0], path=env.get("PATH"))
    if not exe:
        raise FileNotFoundError("%r is not on PATH — cannot run this arm (this is a harness "
                                "failure, not a result for %s)" % (cmd[0], cmd[0]))
    # Refuse to silently truncate. If a multi-line prompt is still sitting in argv on Windows,
    # cmd.exe would cut it at the first newline and the run would LOOK fine — so fail loudly.
    if os.name == "nt" and stdin_text is None:
        for i, a in enumerate(cmd[1:], 1):
            if isinstance(a, str) and "\n" in a:
                raise ValueError(
                    "argv[%d] of %r contains a newline; on Windows cmd.exe would truncate it "
                    "there and the agent would receive only a fragment. Pass it via stdin_text."
                    % (i, cmd[0]))
    return subprocess.run([exe] + list(cmd[1:]), cwd=workdir, env=env, text=True, check=False,
                          timeout=timeout, capture_output=True, input=stdin_text)


# Third-party provider keys the `claude` CLI never needs — dropped from the child env so that a
# prompt-injection in the (untrusted) issue text / repo can't exfiltrate them (see SECURITY note).
_NON_CLAUDE_KEYS = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "ANTHROPIC_BEDROCK_BASE_URL", "ANTHROPIC_VERTEX_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_VERTEX",
    "DEEPSEEK_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "GROQ_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
)

# A product-comparison Codex arm must use the ChatGPT login already held by the first-party CLI.
# Drop every common API-key/custom-endpoint escape hatch from the child even after the benchmark's
# launch guard checked the parent environment.  The duplicate defence matters because a caller can
# invoke this predictor directly, and a shadowing OPENAI_API_KEY would silently turn a subscription
# comparison into a metered API run.
_NON_CODEX_KEYS = (
    "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN", "OPENAI_ACCESS_TOKEN", "OPENAI_BASE_URL",
    "OPENAI_API_BASE", "OPENAI_ORG_ID", "OPENAI_ORGANIZATION", "OPENAI_PROJECT",
    "OPENAI_PROJECT_ID", "CODEX_API_KEY", "CODEX_AUTH_TOKEN", "CODEX_BASE_URL",
    "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_AD_TOKEN", "AZURE_OPENAI_ENDPOINT",
    # These identify the *parent* Codex session.  Inheriting them made a nested standalone CLI
    # silently reuse the parent's read-only tool profile even with `--sandbox workspace-write`.
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_PERMISSION_PROFILE", "CODEX_THREAD_ID",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
    "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
    "XAI_API_KEY", "OPENROUTER_API_KEY", "MISTRAL_API_KEY",
)


def predict_claude_code(workdir: str, problem_statement: str, model="", timeout=1800,
                        max_turns=24):
    # SECURITY: benchmark repositories and issue text are untrusted.  Safe mode disables project
    # hooks/instructions/plugins, no-session-persistence avoids leaving benchmark conversations in
    # the user's history, and the explicit tool set omits Bash and all network tools.  acceptEdits
    # keeps headless source changes working without granting arbitrary command execution.  This is
    # intentionally a tool-restricted product run, and the report labels it that way.
    # --output-format json so the run reports its own usage and cost. Plain text mode returns only
    # the answer, which left the Claude arm with NO token or cost data at all while Collie's was
    # measured — a comparison can't discuss efficiency when one side is unmeasured. The JSON also
    # separates cache reads from fresh input, which is the difference between a real cost figure
    # and one several times too high. Editing behaviour is unaffected; only stdout changes.
    safe_tools = "Read,Edit,Write,Grep,Glob"
    cmd = ["claude", "-p", "--output-format", "json", "--permission-mode", "acceptEdits",
           "--safe-mode", "--no-session-persistence", "--tools", safe_tools,
           "--allowedTools", safe_tools, "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    # RETURN the CompletedProcess. Discarding it made a non-zero exit / a refusal on stdout
    # indistinguishable from "the agent chose to change nothing", which a comparison harness then
    # scores as a loss. Callers must look at returncode and stderr before recording an empty patch.
    return _run_cli(cmd, workdir, extra_env={k: None for k in _NON_CLAUDE_KEYS}, timeout=timeout,
                    stdin_text=CLI_SWE_PROMPT + problem_statement)


def predict_codex(workdir: str, problem_statement: str, model="", timeout=1800,
                  complete_prompt=None):
    """Drive the first-party Codex CLI through its existing ChatGPT subscription login.

    This is a *native product* arm, not a same-model harness arm.  It deliberately ignores user
    config and repository rules so a custom provider or project instruction cannot change the
    billed route or the task prompt.  The CLI still reads its normal auth store.  Workspace-write
    contains generated commands to the disposable checkout; callers must still use an isolated
    benchmark directory because the evaluated repository is untrusted.
    """
    disabled_features = (
        "apps", "auth_elicitation", "browser_use", "browser_use_external",
        "browser_use_full_cdp_access", "computer_use",
        "fast_mode", "goals", "hooks", "image_generation", "in_app_browser",
        "memories", "multi_agent", "plugin_sharing", "plugins", "remote_compaction_v2",
        "remote_plugin", "skill_mcp_dependency_install", "skill_search",
        "tool_call_mcp_elicitation", "tool_suggest", "workspace_dependencies",
    )
    # These are Codex's native local-repository execution surfaces, not foreign
    # tools.  Keep them on so the product arm remains capable of inspecting and
    # editing its isolated checkout.
    local_features = ("code_mode_host", "shell_snapshot", "shell_tool", "unified_exec")
    cmd = ["codex", "--sandbox", "workspace-write", "--ask-for-approval", "never"]
    for feature in disabled_features:
        cmd += ["--disable", feature]
    for feature in local_features:
        cmd += ["--enable", feature]
    for override in ('web_search="disabled"', "tools.web_search=false",
                     'cli_auth_credentials_store="file"',
                     'model_reasoning_effort="high"'):
        cmd += ["-c", override]
    if model:
        cmd += ["--model", model]
    cmd += ["exec", "--json", "--ephemeral", "--ignore-user-config",
           "--ignore-rules", "--strict-config", "--color", "never",
           "--cd", workdir]
    # `-` is Codex's documented stdin sentinel.  Besides preserving multiline issue bodies on
    # Windows, stdin keeps untrusted issue text out of process listings and shell diagnostics.
    cmd.append("-")
    prompt = (CLI_SWE_PROMPT + problem_statement
              if complete_prompt is None else complete_prompt)
    return _run_cli(cmd, workdir, extra_env={k: None for k in _NON_CODEX_KEYS}, timeout=timeout,
                    stdin_text=prompt)


def predict_hermes(workdir: str, problem_statement: str, model="", timeout=1800):
    # DeepSeek via ~/.hermes/config.yaml; unset a shadowing OPENAI_API_KEY.
    # To match collie on the same latest model, set:
    #   HERMES_PROVIDER=anthropic HERMES_MODEL=claude-opus-4-8  (+ ANTHROPIC_API_KEY in env)
    # Billing reality (verified): Anthropic reinstated third-party agent use of Claude
    # subscriptions, but meters it to a SEPARATE pay-as-you-go "extra usage" pool, NOT your
    # flat Max session quota (which only the first-party `claude` CLI draws — that's why
    # collie-via-`claude -p` runs on the flat subscription and Hermes hit "out of extra
    # usage"). So for Hermes, either fund extra-usage on claude.ai, or (cleaner + fully
    # per-token) set ANTHROPIC_API_KEY here for its native anthropic provider.
    cmd = ["hermes", "-z", CLI_SWE_PROMPT + problem_statement]
    # Select provider/model via the HERMES_PROVIDER/HERMES_MODEL ENV, NOT --provider/-m flags: the
    # flag path makes hermes consult its OWN auth store ("No Codex credentials stored") while the env
    # path reads ~/.codex/auth.json (Codex sub) / the native creds directly. _run_cli inherits
    # os.environ so the vars already reach hermes; passing them again as flags broke Codex.
    # Token capture is limited: hermes runs via CLI (no runs.db) and its -z stdout is only the
    # answer; HERMES_DUMP_REQUEST_STDOUT dumps REQUEST payloads (no usage — usage is in the response),
    # and the per-session token store is not cleanly queryable. So for hermes we record wall_s only
    # (timed in swe_predict_one); tokens/turns stay 0. Wall is the reliable cross-agent metric.
    p = _run_cli(cmd, workdir, extra_env={"OPENAI_API_KEY": None}, timeout=timeout)
    return _parse_cli_usage((p.stdout or "") + "\n" + (p.stderr or ""))


def _parse_cli_usage(text):
    """Best-effort tokens/turns from a CLI agent's dumped output. Sums usage blocks; counts
    assistant/tool round-trips as turns. Returns {} if nothing parseable (wall_s still recorded)."""
    import re
    toks = 0
    for m in re.finditer(r'"(?:total_tokens|input_tokens|output_tokens|prompt_tokens|completion_tokens)"\s*:\s*(\d+)', text):
        toks += int(m.group(1))
    # avoid double-counting: prefer explicit total_tokens if present
    tot = [int(m.group(1)) for m in re.finditer(r'"total_tokens"\s*:\s*(\d+)', text)]
    if tot:
        toks = sum(tot)
    turns = len(re.findall(r'"role"\s*:\s*"assistant"', text)) or text.count("[request]") or 0
    out = {}
    if toks:
        out["tokens"] = toks
    if turns:
        out["turns"] = turns
    return out


def predict_aider(workdir: str, problem_statement: str, model="", timeout=1800):
    # Aider on DeepSeek (native, reads DEEPSEEK_API_KEY). --no-auto-commits keeps edits in
    # the working tree for make_patch; keep .git (don't --no-git) so the diff works.
    _run_cli(["aider", "--model", "deepseek/deepseek-chat",
              "--message", CLI_SWE_PROMPT + problem_statement, "--yes",
              "--no-auto-commits", "--no-gitignore", "--no-analytics", "--no-check-update"],
             workdir, timeout=timeout)


def predict_opencode(workdir: str, problem_statement: str, model="", timeout=1800):
    # opencode headless `run` on DeepSeek; --auto approves tool use (else it stalls).
    # SUBSCRIPTION mode (Claude Pro/Max): run `opencode auth login` once, then set
    #   SWE_OPENCODE_MODEL=anthropic/claude-sonnet-5  (whether it draws the FLAT free pool vs the
    #   metered extra-usage pool depends on opencode's own system-prompt fingerprint — verify).
    mdl = model or os.environ.get("SWE_OPENCODE_MODEL", "deepseek/deepseek-chat")
    _run_cli(["opencode", "run", "--auto", "--model", mdl,
              CLI_SWE_PROMPT + problem_statement], workdir, timeout=timeout)


def predict_pi(workdir: str, problem_statement: str, model="", timeout=1800):
    # pi (a minimal reference harness) headless. `-p` = print/non-interactive
    # (no tool-approval prompt); `-a` trusts project-local files. Install: npm i -g
    # @earendil-works/pi-coding-agent.
    #   DeepSeek (default):   reads DEEPSEEK_API_KEY from env.
    #   Other providers: run Pi's documented `/login`, then set
    #     SWE_PI_PROVIDER/SWE_PI_MODEL. Pi remains the owner of its credentials.
    # NB pi -p (print) mode runs built-in tools without an approval prompt, so no --approve flag is
    # needed (and pi 0.74.2 rejects the old `-a`). IMPORTANT: on the Claude subscription pi draws the
    # METERED extra-usage pool, NOT the flat free plan (empirically verified: 400 "out of extra
    # usage"). Fund extra-usage at claude.ai/settings/usage for a subscription Pi run, or use
    # a documented provider with an approved budget.
    prov = os.environ.get("SWE_PI_PROVIDER", "deepseek").strip()
    if prov.lower() == "claudesub":
        raise ValueError(
            "SWE_PI_PROVIDER=claudesub was removed; use a documented Pi provider/login")
    mdl = model or os.environ.get("SWE_PI_MODEL", "deepseek-chat")
    normalized_model = mdl.strip().lower()
    if normalized_model == "claudesub" or normalized_model.startswith(
            ("claudesub/", "claudesub:")):
        raise ValueError(
            "SWE_PI_MODEL cannot select removed provider 'claudesub'; use a documented Pi provider")
    cmd = ["pi", "-p", CLI_SWE_PROMPT + problem_statement, "--provider", prov, "--model", mdl]
    p = _run_cli(cmd, workdir, timeout=timeout)
    return _parse_cli_usage(((p.stdout or "") + "\n" + (p.stderr or "")) if p else "")


# ---- ULTRA tier: best-of-k sampling + ORACLE-FREE selection --------------------------------
# The pass@k >> pass@1 gap (multi-run eval) means the right patch is usually AMONG k samples;
# ultra spends k x compute to try to SELECT it without seeing the tests. Selection = consensus
# first (if >=2 samples produce the same normalized patch, self-consistency is a strong signal),
# else an LLM judge ranks the distinct candidates. This is collie's "ultra" toggle, à la CC.
def _norm_patch(p):
    # normalize for consensus: drop volatile hunk-line-numbers + blob hashes + blank lines, but KEEP
    # the +++/--- file paths — dropping them let two patches that add the same line to DIFFERENT
    # files collapse to one consensus bucket (a wrong winner picked without the judge).
    out = []
    for ln in p.splitlines():
        if ln.startswith(("@@", "index ", "diff ")):
            continue
        s = ln.rstrip()
        if s:
            out.append(s)
    return "\n".join(out)


def _judge_patch(problem_statement, patches):
    """Pick the candidate most likely to correctly+completely fix the issue (no test access)."""
    body = "\n\n".join("### CANDIDATE %d\n%s" % (i + 1, p[:2500]) for i, p in enumerate(patches))
    sysmsg = ("You are selecting the best patch for a GitHub issue. Pick the ONE most likely to "
              "COMPLETELY and CORRECTLY fix it: right file(s), handles the edge cases the issue "
              "implies, no syntax errors, minimal collateral change. Reply with ONLY the number.")
    base, env, model = _ENDPOINTS_ULTRA
    key = os.environ.get(env, "")
    out = _chat_ultra(base, key, model, sysmsg,
                      "ISSUE:\n%s\n\n%s\n\nBest candidate number:" % (problem_statement[:2500], body))
    for tok in (out or "").split():
        if tok.strip(".:#").isdigit():
            i = int(tok.strip(".:#")) - 1
            if 0 <= i < len(patches):
                return i
    return 0


_ENDPOINTS_ULTRA = ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat")


def _chat_ultra(base, key, model, system, user, max_tokens=16):
    import urllib.request
    body = json.dumps({"model": model, "temperature": 0.0, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"content-type": "application/json",
                                          "authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read())["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""


def predict_collie_ultra(workdir: str, problem_statement: str, provider="deepseek",
                         model=None, k=3):
    """Generate k candidate patches (reset repo between each), then apply the selected one."""
    patches = []
    for _ in range(k):
        # reset --hard HEAD (NOT `checkout -- .`, which restores from the INDEX — and make_patch
        # `git add -A`'s the prior candidate's edits into the index, so checkout left them in place,
        # contaminating candidates 2..k and making the FINAL apply fail on already-applied context.
        _git(["reset", "-q", "--hard", "HEAD"], cwd=workdir, check=False)
        _git(["clean", "-fd"], cwd=workdir, check=False)
        predict_collie(workdir, problem_statement, provider=provider, model=model)
        patches.append(make_patch(workdir))
    _git(["reset", "-q", "--hard", "HEAD"], cwd=workdir, check=False)
    _git(["clean", "-fd"], cwd=workdir, check=False)
    nonempty = [p for p in patches if p.strip()]
    if not nonempty:
        return
    # consensus: if a normalized patch appears >=2x, trust self-consistency
    from collections import Counter
    norm = [_norm_patch(p) for p in nonempty]
    common, cnt = Counter(norm).most_common(1)[0]
    if cnt >= 2:
        winner = nonempty[norm.index(common)]
    else:
        winner = nonempty[_judge_patch(problem_statement, nonempty)]
    # apply the winning patch back onto the clean repo. Try the winner first, then the OTHER
    # candidates in order — if the winner doesn't apply (context drift after checkout/clean) we
    # must NOT silently leave an empty patch (scored unresolved despite a valid candidate existing).
    ordered = [winner] + [p for p in nonempty if p is not winner]
    for cand in ordered:
        proc = subprocess.run(["git", "-C", workdir, "apply", "--whitespace=nowarn"],
                              input=cand.encode(), capture_output=True)
        if proc.returncode == 0:
            return
        proc3 = subprocess.run(["git", "-C", workdir, "apply", "--3way", "--whitespace=nowarn"],
                               input=cand.encode(), capture_output=True)
        if proc3.returncode == 0:
            return
    import sys as _sys
    print("WARN(swe): no candidate patch applied cleanly for this instance — emitting empty patch",
          file=_sys.stderr)


AGENTS = {"collie": predict_collie, "claude": predict_claude_code,
          "hermes": predict_hermes,
          "collie_ultra": predict_collie_ultra,
          "aider": predict_aider, "opencode": predict_opencode, "pi": predict_pi}

_CAP_OK = None


def _capped(cmd, mem="12G", swap="2G"):
    """Wrap a command in a systemd cgroup memory cap so one pathological instance
    (e.g. an agent that runs `pip install` and balloons) is killed inside its own
    cgroup instead of OOM-ing the whole box. Falls back to unwrapped if unavailable.
    `--scope` inherits the caller's environment (so DEEPSEEK_API_KEY passes through)."""
    global _CAP_OK
    if _CAP_OK is None:
        _CAP_OK = subprocess.run(["systemd-run", "--user", "--scope", "-q",
                                  "-p", "MemoryMax=64M", "true"],
                                 capture_output=True).returncode == 0
    if not _CAP_OK:
        return cmd
    return ["systemd-run", "--user", "--scope", "-q",
            "-p", "MemoryMax=%s" % mem, "-p", "MemorySwapMax=%s" % swap] + cmd


def _harness_sha():
    """Short git SHA of the harness that produced a prediction — pinned into each shard so a
    cross-device resume can refuse to MIX predictions from different harness versions in one A/B."""
    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return _git(["rev-parse", "--short", "HEAD"], cwd=root, capture=True).stdout.strip()
    except Exception:
        return "unknown"


def _shard_dir(out_path):
    """preds/<run_id>.jsonl -> preds/<run_id>/ : one JSON per instance. Per-instance files make a
    private-repo sync conflict-free — two devices predicting DIFFERENT instances never collide, the
    SAME instance is idempotent. The .jsonl is a rebuildable VIEW (assemble_jsonl), not the store."""
    return out_path[:-6] if out_path.endswith(".jsonl") else out_path + ".d"


def _shard_done(shard_dir, legacy_jsonl=None):
    """Resume set = instances with a shard file, UNION any legacy single-file jsonl (back-compat so
    an in-flight run started before sharding isn't re-predicted)."""
    done = set()
    if os.path.isdir(shard_dir):
        for fn in os.listdir(shard_dir):
            if fn.endswith(".json"):
                done.add(fn[:-5])
    if legacy_jsonl and os.path.exists(legacy_jsonl):
        for line in open(legacy_jsonl, encoding="utf-8"):
            try:
                done.add(json.loads(line)["instance_id"])
            except Exception:
                pass
    return done


def assemble_jsonl(out_path):
    """Rebuild the swebench-format preds/<run_id>.jsonl from the shard dir (+ any legacy lines).
    Called before evaluate() so the eval path is unchanged. Idempotent; newest shard wins."""
    shard_dir = _shard_dir(out_path)
    rows = {}
    if os.path.exists(out_path):                     # seed with legacy lines (pre-sharding runs)
        for line in open(out_path, encoding="utf-8"):
            try:
                r = json.loads(line); rows[r["instance_id"]] = r
            except Exception:
                pass
    if os.path.isdir(shard_dir):
        for fn in sorted(os.listdir(shard_dir)):
            if fn.endswith(".json"):
                try:
                    r = json.load(open(os.path.join(shard_dir, fn), encoding="utf-8"))
                    rows[r["instance_id"]] = {k: r[k] for k in
                                              ("instance_id", "model_name_or_path", "model_patch")}
                except Exception:
                    pass
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for iid in sorted(rows):
            f.write(json.dumps(rows[iid], ensure_ascii=False) + "\n")
    os.replace(tmp, out_path)
    return len(rows)


def build_predictions(instance_ids, agent="collie", out_path=None,
                      provider="deepseek", model=None, timeout=1800):
    """Run `agent` on each instance, sharding predictions to preds/<run_id>/<instance>.json (one
    file each) and keeping the swebench-format preds/<run_id>.jsonl as a rebuilt view.

    Each instance is predicted in a FRESH subprocess (harness/swe_predict_one) so ONNX-embedder
    memory can't accumulate across instances. Each shard is fsynced immediately, so a killed run —
    or a run continued on ANOTHER device after `bench pull` — resumes cleanly (done = shard files).
    """
    import socket
    import sys
    from datasets import load_dataset
    ds = load_dataset("princeton-nlp/SWE-bench_Verified", split="test")
    by_id = {r["instance_id"]: r for r in ds}
    out_path = out_path or "preds/%s.jsonl" % agent
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    shard_dir = _shard_dir(out_path)
    os.makedirs(shard_dir, exist_ok=True)
    done = _shard_done(shard_dir, legacy_jsonl=out_path)   # resume across kills AND devices
    host, sha = socket.gethostname(), _harness_sha()
    for iid in instance_ids:
        if iid in done:
            print("  [%s] %s (cached, skip)" % (agent, iid), flush=True)
            continue
        inst = by_id[iid]
        spec = {"instance_id": iid, "repo": inst["repo"],
                "base_commit": inst["base_commit"],
                "problem_statement": inst["problem_statement"],
                "agent": agent, "provider": provider, "model": model}
        sf = tempfile.NamedTemporaryFile("w", suffix=".spec.json", delete=False)
        json.dump(spec, sf); sf.close()
        ofd, of = tempfile.mkstemp(suffix=".out.json")   # mkstemp (not mktemp): no predictable-name race
        os.close(ofd)
        patch = ""
        completed = False                       # did the child finish cleanly?
        try:
            subprocess.run(_capped([sys.executable, "-m",
                            "harness.swe_predict_one", sf.name, of]),
                           check=False, timeout=timeout)
            if os.path.exists(of):
                patch = json.load(open(of, encoding="utf-8")).get("model_patch", "")
                completed = True
        except subprocess.TimeoutExpired:
            print("  [%s] %s TIMEOUT" % (agent, iid), flush=True)
        finally:
            for p in (sf.name, of):
                try: os.remove(p)
                except OSError: pass
        if not completed:
            # timeout / crash / OOM-kill: do NOT record — leaving it out keeps it out of the
            # resume `done` set so it's retried, instead of frozen as a permanent score-0.
            print("  [%s] %s not completed — will retry next run" % (agent, iid), flush=True)
            continue
        rec = {"instance_id": iid, "model_name_or_path": agent, "model_patch": patch,
               "_meta": {"host": host, "harness_sha": sha, "provider": provider, "model": model}}
        tmp = os.path.join(shard_dir, iid + ".json.tmp")
        with open(tmp, "w", encoding="utf-8") as sfh:
            json.dump(rec, sfh, ensure_ascii=False)
            sfh.flush(); os.fsync(sfh.fileno())
        os.replace(tmp, os.path.join(shard_dir, iid + ".json"))   # atomic -> resumable
        print("  [%s] %s  patch_len=%d" % (agent, iid, len(patch)), flush=True)
    assemble_jsonl(out_path)                          # keep the swebench-format view current
    return out_path


def evaluate(predictions_path, run_id, instance_ids, max_workers=2):
    """Invoke the official harness. Requires Docker (see module docstring).

    max_workers=2 by default: each worker runs a test container (~1-2GB); this box
    also hosts other services, so keep the eval's parallel container count modest to
    stay well clear of OOM. Raise it only when RAM headroom is confirmed.
    """
    import sys
    env = dict(os.environ)
    env.setdefault("DOCKER_HOST", "unix:///var/run/docker.sock")
    cmd = [sys.executable, "-m", "swebench.harness.run_evaluation",
           "--dataset_name", "princeton-nlp/SWE-bench_Verified", "--split", "test",
           "--predictions_path", predictions_path, "--run_id", run_id,
           "--max_workers", str(max_workers), "--cache_level", "env",
           "--instance_ids"] + list(instance_ids)
    return subprocess.run(cmd, env=env).returncode
