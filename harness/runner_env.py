"""Environment policy for the agent CLIs Collie hands work to.

Collie's own process environment holds three things a child ``codex`` /
``claude`` process must not simply inherit, and each one fails differently:

* a **credential** (``ANTHROPIC_API_KEY``) turns a subscription run into a
  metered one — the receipt would say "subscription" while the invoice says
  otherwise, and nobody finds out until the bill arrives;
* an **endpoint** (``ANTHROPIC_BASE_URL``, ``HTTPS_PROXY``,
  ``NODE_EXTRA_CA_CERTS``) sends the prompt and the answer somewhere Collie
  cannot attest to, while the receipt still names the vendor;
* an **injection** variable (``NODE_OPTIONS``, ``LD_PRELOAD``) runs somebody
  else's code inside a process the user was told is a stock CLI.

So the child environment is *built*, never filtered.  Only names on an
allowlist — each carrying a written reason for being there — reach the child;
everything else is absent because it was never copied.  That is what makes
``env_receipt["allowed"]`` a complete description of what the child could see,
rather than a hopeful subset.

Two independent defences, on purpose — the same pair ``harness/swe.py`` already
uses:

* :func:`assert_no_billing_override` refuses to start when the *parent*
  environment would re-route this runner's billing or endpoint.  It raises
  instead of degrading, because a wrong charge cannot be undone after the fact
  and "we quietly ran it on your API key" is not a recoverable outcome.
* :func:`child_env` strips the same inventory again on the way down, because a
  caller can reach a runner without going through the preflight.

Credential *values* are never read here.  Sensitive names are compared and
reported; values are only ever passed through for allowlisted, non-sensitive
names.  ``env_receipt`` therefore contains key names and nothing else, and is
safe to persist in a run receipt or print in a log.

The inventories below are the merge of the five places that had each grown
their own copy: ``bench/normalized_prime_pi.py`` (``_SAFE_INHERITED_ENV``,
``FORBIDDEN_AUTH_ENV``), ``bench/normalized_hermes.py`` (``_SAFE_ENV_KEYS``),
``harness/swe.py`` (``_NON_CLAUDE_KEYS``, ``_NON_CODEX_KEYS``) and
``harness/subscription_guard.py`` (the forbidden names/prefixes and
``_STATUS_CHILD_ENV_NAMES``).  ``tests/test_runner_env.py`` imports those five
sources and asserts the merge still holds, so a name added there cannot silently
stop applying here.
"""
from __future__ import annotations

from collections.abc import Mapping
import os


class BillingOverrideError(RuntimeError):
    """The parent environment would change who pays or where requests go.

    Raised by :func:`assert_no_billing_override` *before* a runner subprocess is
    created, and by :func:`child_env` when a caller tries to inject such a name
    through ``extra``.  It is deliberately not a degrade-and-continue signal:
    the whole promise of an external worker is that the receipt names the route
    that was actually used, and an ambient ``ANTHROPIC_API_KEY`` breaks exactly
    that promise while the run still looks successful.

    Defined here rather than imported from :mod:`harness.runner_specs` so this
    module keeps working on its own (it is imported by the runners themselves,
    the conformance matrix, and by tests that have no reason to pull in the
    whole contract module).  It belongs to the same ``RunnerError`` family
    conceptually: a start-time refusal, safe to show to the user, carrying no
    credential material.  The message names variables, never values.
    """


# Policy names.  ``HarnessSpec.env_policy`` is one of these.  ``sidecar-harness``
# is a placeholder for the phase-3 sidecar runners (pi / prime / hermes), which
# additionally launch against a freshly rendered config tree; today it behaves
# exactly like ``native`` so that a spec can already declare it.
POLICIES: tuple[str, ...] = ("native", "codex", "claude", "sidecar-harness")


# Every name here has to earn its place: a child that cannot start is a worse
# outcome than a child with a small environment, and most "works on my machine"
# reports about spawned CLIs are one of these going missing.  Windows needs far
# more of them than POSIX does, and they are listed unconditionally — a name the
# platform does not define simply never appears in the parent environment, so no
# platform branch is needed (and none can drift).
_INHERITED: dict[str, str] = {
    # -- finding and starting the executable --------------------------------
    "PATH": "resolve the CLI and everything it shells out to (git, node)",
    "PATHEXT": "Windows finds the `.cmd` shim of an npm-installed CLI only through this list",
    "COMSPEC": "that `.cmd` shim is executed by the cmd.exe named here; CreateProcess fails without it",
    "SYSTEMROOT": "the Windows loader and Winsock read it; a child without it dies at startup "
                  "(0xc0000139) before it can print a reason",
    "SYSTEMDRIVE": "tools compose absolute paths from it",
    "WINDIR": "older tooling reads this instead of SYSTEMROOT",
    "PROGRAMFILES": "locating an installed program (git, node) that is not itself on PATH",
    "PROGRAMFILES(X86)": "same, for a 32-bit install on a 64-bit machine",
    "PROGRAMW6432": "same, as seen from a 32-bit process",
    "PROGRAMDATA": "machine-wide configuration (git's system config) lives under it",
    "APPDATA": "roaming application data: npm's prefix and the CLI's own settings",
    "LOCALAPPDATA": "per-machine application data and download caches",
    # -- where `~` is, i.e. where the CLI finds its own login ---------------
    "USERPROFILE": "the Windows home; `~/.codex/auth.json` and `~/.claude` are resolved from it",
    "HOMEDRIVE": "half of the legacy Windows home pair, still honoured by git and node",
    "HOMEPATH": "the other half — passing only one of the two silently relocates `~`",
    "HOME": "the POSIX home, and node/git honour it on Windows as well",
    # -- scratch space ------------------------------------------------------
    "TEMP": "the CLI writes scratch files; npm-installed tools fail outright without one",
    "TMP": "the other Windows spelling of the same directory",
    "TMPDIR": "the POSIX spelling",
    # -- text decoding ------------------------------------------------------
    "LANG": "decides the child's output encoding; a wrong one mangles the JSON we parse back",
    "LC_ALL": "same, and it overrides LANG",
    "LC_CTYPE": "same, narrower",
}

# Per-policy additions.  Keep these tiny: every entry is a name the child gets
# that the reviewer of a receipt has to reason about.
_POLICY_INHERITED: dict[str, dict[str, str]] = {
    "native": {},
    "codex": {
        # CODEX_HOME selects *which* login file the CLI reads, not who is
        # billed for reading it.  It is inherited because Collie's own probe
        # honours the same variable (`harness/codex_oauth.py`,
        # `harness/catalog.py`): stripping it here would make the receipt's
        # billing evidence describe a different account than the one that ran.
        "CODEX_HOME": "the CLI and Collie's own login probe must read the same auth.json",
    },
    # Deliberately NOT CLAUDE_CONFIG_DIR: Collie reads `~/.claude` directly
    # (`harness/providers.claude_credentials`), so honouring the variable in the
    # child would let the run use an account the probe never looked at.
    "claude": {},
    "sidecar-harness": {},
}

# Injected rather than inherited.  Colour escapes end up inside the error text
# we surface and inside `_clean_error` output; a child that never emits them is
# easier to attribute than one whose message we have to strip.
_INJECTED: dict[str, str] = {"NO_COLOR": "1"}


# ---------------------------------------------------------------------------
# The sensitive inventory: names that must never reach a worker, grouped by the
# failure each group causes.  Membership is by exact upper-cased name or by
# prefix; the prefixes exist because a variable a future CLI release invents
# ("ANTHROPIC_SOMETHING_NEW") has to fail closed until somebody reviews it —
# the same argument `subscription_guard._FORBIDDEN_ENV_PREFIXES` makes.
# ---------------------------------------------------------------------------

# Provider credentials.  Union of `FORBIDDEN_AUTH_ENV`
# (bench/normalized_prime_pi.py), `_NON_CLAUDE_KEYS` and `_NON_CODEX_KEYS`
# (harness/swe.py).  A worker that inherits one of these can bill an account
# Collie never checked, or exfiltrate it: the repository and the task text are
# untrusted input, and the worker executes both.
_CREDENTIAL_NAMES = frozenset({
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
    "AZURE_OPENAI_AD_TOKEN", "AZURE_OPENAI_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY", "CODEX_AUTH_TOKEN",
    "COPILOT_GITHUB_TOKEN", "DEEPSEEK_API_KEY", "GEMINI_API_KEY", "GH_TOKEN",
    "GITHUB_TOKEN", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENAI_ACCESS_TOKEN",
    "OPENAI_API_KEY", "OPENAI_AUTH_TOKEN", "OPENROUTER_API_KEY",
    "PRIME_API_KEY", "XAI_API_KEY",
    # Not secrets, but they name the account/project the charge lands on, which
    # is the same failure with a different mechanism.
    "OPENAI_ORGANIZATION", "OPENAI_ORG_ID", "OPENAI_PROJECT", "OPENAI_PROJECT_ID",
})

# Endpoint / routing overrides.  Union of the base-URL names in
# `harness/swe.py` and the proxy and TLS-trust names in
# `subscription_guard._FORBIDDEN_ENV_NAMES`.  These do not need a credential to
# do damage: they redirect a subscription-authenticated request to a host of
# somebody else's choosing, or disable the check that would have noticed.
#
# Stripping the proxy names has a visible cost: behind a corporate proxy the
# worker will fail to connect instead of silently tunnelling through it.  That
# is the intended trade — a run whose traffic was intercepted must not be
# attributed to the user's subscription without anybody noticing — and the
# stripped names appear in the receipt so the failure is diagnosable.
_ROUTING_NAMES = frozenset({
    "ALL_PROXY", "ANTHROPIC_BASE_URL", "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL", "AZURE_OPENAI_ENDPOINT", "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_USE_VERTEX", "CODEX_BASE_URL",
    "CURL_CA_BUNDLE", "GLOBAL_AGENT_HTTP_PROXY", "GLOBAL_AGENT_HTTPS_PROXY",
    "GRPC_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NODE_EXTRA_CA_CERTS",
    "NODE_TLS_REJECT_UNAUTHORIZED", "NO_PROXY", "NPM_CONFIG_HTTPS_PROXY",
    "NPM_CONFIG_PROXY", "OPENAI_API_BASE", "OPENAI_BASE_URL",
    "REQUESTS_CA_BUNDLE", "SSL_CERT_DIR", "SSL_CERT_FILE",
})

# Session identity of the *parent* agent CLI.  `harness/swe.py` found these the
# hard way: a nested Codex CLI inherited the parent's read-only tool profile and
# quietly ignored `--sandbox workspace-write`.  Collie is very often started
# from inside one of these CLIs, so the child must be told nothing about the
# session that spawned it — but see `_BILLING_ROUTE_EXEMPT`: none of them names
# a payer, so stripping is the whole fix and refusing to start would be wrong.
_PARENT_SESSION_NAMES = frozenset({
    # Current Codex desktop/IDE launches (0.149+) use these two shorter
    # spellings.  They identify the parent session, not an account or endpoint;
    # inheriting them can nest the child into the parent's permissions, while
    # refusing them would make every worker unusable from the primary UI.
    "CODEX_CI", "CODEX_SESSION_ID",
    # Codex 0.155.1 inject_session_env exports its harness version to tools.
    # It is parent metadata, not a billing override; discard it in the child.
    "CODEX_VERSION",
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT",
    "CODEX_INTERNAL_ORIGINATOR_OVERRIDE", "CODEX_PERMISSION_PROFILE",
    "CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED", "CODEX_THREAD_ID",
})

# The rest of what Claude Code 2.1.221 exports into a terminal it owns: an
# entrypoint marker, a version string, feature switches, a PID, an IPC socket
# and its one-shot token, and the effort the *parent* session was set to.  None
# of them names an account, an endpoint or a key, so like `_PARENT_SESSION_NAMES`
# they are stripped from the child but must not refuse the launch.  Enumerated
# from a live session rather than guessed; the prefix rule alone refused every
# `--runner claude-code` run started from inside Claude Code, which is exactly
# the terminal this feature exists to be used from.
_PARENT_SESSION_PREFIXES = ("CLAUDE_CODE_ENABLE_", "CLAUDE_CODE_MESSAGING_")
_PARENT_SESSION_NAMES = _PARENT_SESSION_NAMES | frozenset({
    "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_EFFORT", "CLAUDE_PID",
})

# Code injection into the interpreter that hosts the worker.  `claude` and
# `codex` are node programs and pi/prime are Python ones, so both loaders are in
# scope; the dynamic-linker variables apply to every one of them.
_INJECTION_NAMES = frozenset({
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "LD_LIBRARY_PATH",
    "LD_PRELOAD", "NODE_OPTIONS", "NODE_PATH", "PYTHONHOME", "PYTHONPATH",
    "PYTHONSTARTUP",
})

_SENSITIVE_NAMES = (_CREDENTIAL_NAMES | _ROUTING_NAMES
                    | _PARENT_SESSION_NAMES | _INJECTION_NAMES)

# Prefix families, from `subscription_guard._FORBIDDEN_ENV_PREFIXES` plus the
# other vendors that appear in the two swe.py lists.  Anything starting with one
# of these is treated as sensitive even if it is not named above.
_SENSITIVE_PREFIXES: tuple[str, ...] = (
    "ANTHROPIC_", "AWS_", "AZURE_OPENAI_", "CLAUDE_", "CODEX_", "COPILOT_",
    "DEEPSEEK_", "GEMINI_", "GH_", "GITHUB_", "GOOGLE_", "GROQ_", "HERMES_",
    "MISTRAL_", "NPM_CONFIG_", "OPENAI_", "OPENROUTER_", "PI_", "PRIME_",
    "XAI_",
)

# Names that match a sensitive prefix but are explicitly kept for one policy
# (see `_POLICY_INHERITED` for the reasoning).  Everything not listed here loses
# to the prefix rule.
_POLICY_PASSTHROUGH: dict[str, frozenset[str]] = {"codex": frozenset({"CODEX_HOME"})}


# ---------------------------------------------------------------------------
# Billing route: the subset of the inventory that decides *who pays* and *which
# endpoint answers*, keyed by `HarnessSpec.credential_family`.  This is what
# `assert_no_billing_override` refuses to start against; the wider inventory
# above is merely stripped, because e.g. a stray GH_TOKEN cannot misbill a Codex
# run — dropping it is a sufficient answer.
# ---------------------------------------------------------------------------
_BILLING_ROUTE_PREFIXES: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_", "CLAUDE_"),
    "codex": ("OPENAI_", "CODEX_", "AZURE_OPENAI_"),
    # A local model has no billing route to hijack, and nothing on the machine
    # would honour these names anyway.
    "local": (),
}
# An unrecognised family ("" = follow PROVIDER, "collie-sidecar", anything new)
# gets the union: we cannot say which vendor's variables that worker honours, so
# the safe answer is "any of them is a refusal".
_BILLING_ROUTE_ANY: tuple[str, ...] = tuple(sorted(
    {p for prefixes in _BILLING_ROUTE_PREFIXES.values() for p in prefixes}))
# Names that match a billing-route prefix but do not decide who pays:
# CODEX_HOME picks the login file (see `_POLICY_INHERITED`), and the parent
# session markers merely say which agent CLI Collie was started from.  They are
# still stripped from the child; refusing over them would make
# `collie run --runner codex-exec` impossible from inside a Codex or Claude Code
# terminal, which is one of the places people will most want to run it.
_BILLING_ROUTE_EXEMPT = frozenset({"CODEX_HOME"}) | _PARENT_SESSION_NAMES


def _upper_name(name: object) -> str:
    if not isinstance(name, str) or not name:
        raise ValueError("environment name must be a non-empty string")
    return name.upper()


def _is_sensitive(upper: str, policy: str) -> bool:
    if upper in _POLICY_PASSTHROUGH.get(policy, frozenset()):
        return False
    return upper in _SENSITIVE_NAMES or upper.startswith(_SENSITIVE_PREFIXES)


def allowlist(policy: str) -> dict[str, str]:
    """Return ``{NAME: why it is inherited}`` for ``policy``.

    Exposed so the conformance matrix (`env_hygiene`) and the docs can show the
    contract instead of restating it, and so a test can assert the allowlist and
    the sensitive inventory never overlap.
    """
    if policy not in POLICIES:
        raise ValueError("unknown env policy %r (known: %s)" % (policy, ", ".join(POLICIES)))
    merged = dict(_INHERITED)
    merged.update(_POLICY_INHERITED.get(policy, {}))
    return merged


def child_env(policy: str, *, extra: Mapping[str, str] | None = None,
              home: str | None = None,
              environ: Mapping[str, str] | None = None,
              ) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Build the environment for a worker subprocess under ``policy``.

    Returns ``(env, env_receipt)`` where ``env_receipt`` is
    ``{"allowed": [names], "stripped": [names]}`` — **names only**, so the
    receipt can be persisted and printed as-is.

    ``allowed`` is the complete set of names the child receives.  ``stripped``
    lists the names from the parent that are on the sensitive inventory and were
    deliberately dropped; ordinary unrelated variables are not listed, because
    they are not *removed* so much as never copied, and a receipt full of
    ``CHOCOLATEYINSTALL`` would bury the two lines that matter.

    ``extra`` injects non-sensitive variables the caller owns (``COLLIE_RUN_ID``,
    ``COLLIE_PROCESS_OWNER``…).  A sensitive name there raises
    :class:`BillingOverrideError`: passing a credential through the side door is
    exactly the thing this module exists to prevent, and a caller doing it by
    accident should hear about it at the call site.

    ``home`` re-points the child's ``~`` at a specific directory (a fresh config
    tree for conformance, a sidecar's rendered home in phase 3).  Both the POSIX
    and the Windows spellings are rewritten together — setting only one of
    ``HOMEDRIVE``/``HOMEPATH`` leaves ``~`` resolving somewhere neither the
    caller nor the CLI expects.

    ``environ`` defaults to :data:`os.environ`; it exists so callers and tests
    can pass an explicit parent mapping instead of mutating the real one.  The
    mapping passed in is never modified.
    """
    allowed_names = allowlist(policy)  # also validates the policy name
    parent = os.environ if environ is None else environ

    env: dict[str, str] = {}
    stripped: set[str] = set()
    for name in parent:
        upper = _upper_name(name)
        sensitive = _is_sensitive(upper, policy)
        if sensitive:
            # Recorded even when it was never on the allowlist: "we saw
            # ANTHROPIC_API_KEY and did not pass it on" is the claim a receipt
            # reader needs, and it is the second half of the double defence
            # described in the module docstring.
            stripped.add(upper)
            continue
        if upper not in allowed_names:
            continue
        value = parent[name]
        if not isinstance(value, str):
            raise ValueError("environment value for %s must be a string" % upper)
        # Canonical upper case, matching `subscription_guard._status_child_environment`:
        # Windows hands us upper-cased names already, and every POSIX name in
        # the allowlist is upper case by convention.
        env[upper] = value

    env.update(_INJECTED)

    if home is not None:
        if not isinstance(home, str) or not home:
            raise ValueError("home must be a non-empty path")
        resolved = os.path.abspath(home)
        env["HOME"] = resolved
        env["USERPROFILE"] = resolved
        drive, rest = os.path.splitdrive(resolved)
        if drive:
            env["HOMEDRIVE"] = drive
            env["HOMEPATH"] = rest
        else:
            # No drive letter to describe this home with, so remove a pair
            # inherited from the parent rather than let it point elsewhere.
            env.pop("HOMEDRIVE", None)
            env.pop("HOMEPATH", None)

    for name in (extra or {}):
        upper = _upper_name(name)
        if _is_sensitive(upper, policy):
            raise BillingOverrideError(
                "refusing to inject %s into a worker environment: it is a credential, "
                "endpoint or loader override" % upper)
        value = (extra or {})[name]
        if not isinstance(value, str):
            raise ValueError("environment value for %s must be a string" % upper)
        env[upper] = value

    return env, {"allowed": sorted(env), "stripped": sorted(stripped)}


def assert_no_billing_override(env: Mapping[str, str] | None, family: str) -> None:
    """Refuse to start when the parent environment would re-route ``family``.

    ``env`` is the *parent* environment (``os.environ`` when ``None``), not the
    child environment :func:`child_env` produces — the point is to catch the
    override before a process exists, so the user gets a sentence they can act
    on instead of a run that succeeded on the wrong account.

    ``family`` is ``HarnessSpec.credential_family`` (``"claude"``, ``"codex"``,
    ``"local"``, ``""``…).  An unknown family is checked against every vendor's
    prefixes, because we cannot claim to know which variables such a worker
    reads.

    Presence is enough to refuse, even for an empty value: an empty
    ``ANTHROPIC_BASE_URL`` is still an ambiguous shell override, and the operator
    unsetting it is cheaper than any of us guessing what the CLI does with it
    (same rule as `subscription_guard._check_environment`).

    Raises :class:`BillingOverrideError` naming the offending variables.  Names
    only — the values are never read, here or anywhere else in this module.
    """
    parent = os.environ if env is None else env
    prefixes = _BILLING_ROUTE_PREFIXES.get(family, _BILLING_ROUTE_ANY)

    found: set[str] = set()
    for name in parent:
        upper = _upper_name(name)
        if upper in _BILLING_ROUTE_EXEMPT or upper.startswith(_PARENT_SESSION_PREFIXES):
            continue
        if prefixes and upper.startswith(prefixes):
            found.add(upper)
    if not found:
        return
    listed = ", ".join(sorted(found))
    raise BillingOverrideError(
        "refusing to start a %s worker: %s set in this environment would change which account "
        "is billed or where requests are sent. Unset %s and run again, or use `--runner collie`."
        % (family or "external", listed,
           "it" if len(found) == 1 else "them"))
