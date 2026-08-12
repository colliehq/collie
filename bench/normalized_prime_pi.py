"""Generate isolated Prime/Pi launch plans for the normalized sidecar track.

This module deliberately does not install a harness, authenticate, start a
process, or contact the model sidecar.  It only creates a fresh configuration
tree and returns the exact cwd/argv/environment a benchmark runner may launch.

The sidecar is required to speak OpenAI Chat Completions while routing every
request to Collie's reviewed, subscription-native Claude Agent SDK transport.
Prime and Pi therefore retain their own prompts, loops, and native coding tools;
only their model transport is normalized.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlsplit


PROVIDER = "collie-sdk-subscription"
MODEL = "claude-opus-4-8"
THINKING = "high"
API = "openai-completions"

# Public, non-secret capability sentinel required by the isolated sidecar.  It
# authenticates only this container-local hop; it is not a model/API credential.
AUTH_SENTINEL = "subscription-sidecar-internal-only-v1"

PRIME_VERSION = "0.7.2"
PRIME_COMMIT = "0987c1ba7637cbcb99afe9efe1180b838a0aa958"
PI_VERSION = "0.84.1"
PI_COMMIT = "53fa77ccd8a279eb87e92294ef3687b03ff80112"

_SAFE_INHERITED_ENV = frozenset(
    {
        "COMSPEC", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "PATHEXT",
        "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "WINDIR",
    }
)

# These variables could select a native provider, leak another login into the
# test, or turn a sidecar failure into a billable API fallback.  The returned
# environment is allowlisted rather than inherited, but keeping this inventory
# explicit makes the fail-closed contract reviewable and testable.
FORBIDDEN_AUTH_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY", "OPENROUTER_API_KEY", "PRIME_API_KEY",
        "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        "AZURE_OPENAI_API_KEY", "COPILOT_GITHUB_TOKEN", "GH_TOKEN",
        "GITHUB_TOKEN",
    }
)


@dataclass(frozen=True)
class HarnessPin:
    name: str
    version: str
    commit: str


@dataclass(frozen=True)
class NormalizedLaunch:
    """A process launch description; callers must not merge ambient env into it."""

    harness: str
    pin: HarnessPin
    root: Path
    config_dir: Path
    session_dir: Path
    home_dir: Path
    models_path: Path
    settings_path: Path
    cwd: Path
    argv: tuple[str, ...]
    env: dict[str, str]
    provider: str = PROVIDER
    model: str = MODEL
    thinking: str = THINKING
    prompt_transport: str = "argv"


@dataclass(frozen=True)
class _ProcessCapture:
    returncode: int
    stdout: str
    stderr: str


PINS = {
    "prime": HarnessPin("prime", PRIME_VERSION, PRIME_COMMIT),
    "pi": HarnessPin("pi", PI_VERSION, PI_COMMIT),
}


def _validated_endpoint(endpoint: str) -> str:
    """Accept only the container sidecar DNS name or loopback with explicit port."""
    value = str(endpoint or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("sidecar endpoint must use http or https")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("sidecar endpoint must not contain credentials, query, or fragment")
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1", "inference"}:
        raise ValueError("subscription sidecar must use loopback or the inference service")
    if not parsed.port:
        raise ValueError("subscription sidecar endpoint must use an explicit port")
    return value


def _model_config(endpoint: str) -> dict:
    return {
        "providers": {
            PROVIDER: {
                "name": "Collie Claude Agent SDK subscription sidecar",
                "baseUrl": endpoint,
                "api": API,
                "apiKey": AUTH_SENTINEL,
                "authHeader": True,
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": True,
                    "supportsUsageInStreaming": True,
                    "maxTokensField": "max_tokens",
                },
                "models": [
                    {
                        "id": MODEL,
                        "name": "Claude Opus 4.8 (normalized subscription transport)",
                        "reasoning": True,
                        "thinkingLevelMap": {"high": "high"},
                        "input": ["text"],
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cacheRead": 0,
                            "cacheWrite": 0,
                        },
                        "contextWindow": 200000,
                        "maxTokens": 16384,
                    }
                ],
            }
        }
    }


def _settings(harness: str) -> dict:
    common = {
        "defaultProvider": PROVIDER,
        "defaultModel": MODEL,
        "defaultThinkingLevel": THINKING,
        "quietStartup": True,
        # The evaluator owns the one physical-request budget for every arm.
        # Prime/Pi otherwise retry one failed logical turn at both the agent
        # and provider layers, multiplying the same infrastructure incident
        # while Collie is configured with max_retries=0.
        "retry": {
            "enabled": False,
            "maxRetries": 0,
            "baseDelayMs": 0,
            "provider": {"maxRetries": 0},
        },
    }
    if harness == "prime":
        common["telemetry"] = {"enabled": False}
    else:
        common.update(
            {
                "defaultProjectTrust": "never",
                "enableInstallTelemetry": False,
                "enableAnalytics": False,
                "packages": [],
            }
        )
    return common


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _fresh_tree(
    root: Path, harness: str, endpoint: str
) -> tuple[Path, Path, Path, Path, Path, Path]:
    root = root.resolve()
    if root.exists():
        if not root.is_dir() or any(root.iterdir()):
            raise FileExistsError("normalized harness root must be absent or empty")
    else:
        root.mkdir(parents=True)
    config = root / "config"
    sessions = root / "sessions"
    home = root / "home"
    for path in (config, sessions, home):
        path.mkdir()
    models = config / "models.json"
    settings = config / "settings.json"
    _write_json(models, _model_config(endpoint))
    _write_json(settings, _settings(harness))
    return root, config, sessions, home, models, settings


def _environment(
    harness: str,
    *,
    config: Path,
    sessions: Path,
    home: Path,
    inherited_env: Mapping[str, str] | None,
) -> dict[str, str]:
    source = os.environ if inherited_env is None else inherited_env
    env = {
        key: str(value)
        for key, value in source.items()
        if key.upper() in _SAFE_INHERITED_ENV and isinstance(value, str)
    }
    env.update(
        {
            "HOME": str(home),
            "NO_COLOR": "1",
            "DO_NOT_TRACK": "1",
            "PI_OFFLINE": "1",
            "PI_SKIP_VERSION_CHECK": "1",
        }
    )
    if harness == "prime":
        env.update(
            {
                "PRIME_AGENT_CODING_AGENT_DIR": str(config),
                "PRIME_AGENT_SESSION_DIR": str(sessions),
                "PRIME_AGENT_TELEMETRY": "0",
                # Prebuilt in the benchmark image. A fresh HOME plus --offline
                # must never trigger Prime's first-use kernel download.
                "PRIME_AGENT_KERNEL_PYTHON": "/opt/prime-kernel/bin/python",
            }
        )
    else:
        env.update(
            {
                "PI_CODING_AGENT_DIR": str(config),
                "PI_CODING_AGENT_SESSION_DIR": str(sessions),
                "PI_TELEMETRY": "0",
            }
        )
    if FORBIDDEN_AUTH_ENV.intersection(env):
        raise AssertionError("auth-bearing environment escaped the allowlist")
    return env


def prepare_normalized_harness(
    harness: str,
    root: str | os.PathLike[str],
    *,
    endpoint: str,
    workspace: str | os.PathLike[str],
    prompt: str,
    executable: str | os.PathLike[str] | None = None,
    inherited_env: Mapping[str, str] | None = None,
) -> NormalizedLaunch:
    """Create one fresh, isolated Prime or Pi launch plan.

    ``env`` is a complete allowlisted process environment, not an overlay.
    The caller must pass it directly to the process launcher and use ``cwd``.
    """
    name = str(harness).strip().lower()
    if name not in PINS:
        raise ValueError("harness must be 'prime' or 'pi'")
    normalized_endpoint = _validated_endpoint(endpoint)
    workspace_path = Path(workspace).resolve()
    if not workspace_path.is_dir():
        raise FileNotFoundError("workspace must be an existing directory")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a non-empty string")
    if prompt.startswith("-"):
        raise ValueError("prompt must not begin with '-' in argv transport")

    (root_path, config, sessions, home, models, settings) = _fresh_tree(
        Path(root), name, normalized_endpoint
    )
    env = _environment(
        name, config=config, sessions=sessions, home=home,
        inherited_env=inherited_env,
    )

    if name == "prime":
        program = str(executable or "/opt/prime-agent/prime-agent.sh")
        argv: Sequence[str] = (
            program,
            "--dist",
            "--mode", "json",
            "--offline",
            "--no-session",
            "--no-extensions",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--provider", PROVIDER,
            "--model", MODEL,
            "--models", f"{PROVIDER}/{MODEL}",
            "--thinking", THINKING,
            "--cwd", str(workspace_path),
            "--", prompt,
        )
    else:
        program = str(executable or "pi")
        argv = (
            program,
            "--mode", "json",
            "--offline",
            "--no-session",
            "--no-extensions",
            "--no-context-files",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--no-approve",
            "--provider", PROVIDER,
            "--model", MODEL,
            "--models", f"{PROVIDER}/{MODEL}",
            "--thinking", THINKING,
            prompt,
        )

    return NormalizedLaunch(
        harness=name,
        pin=PINS[name],
        root=root_path,
        config_dir=config,
        session_dir=sessions,
        home_dir=home,
        models_path=models,
        settings_path=settings,
        cwd=workspace_path,
        argv=tuple(argv),
        env=env,
    )


def prepare_prime(*args, **kwargs) -> NormalizedLaunch:
    return prepare_normalized_harness("prime", *args, **kwargs)


def prepare_pi(*args, **kwargs) -> NormalizedLaunch:
    return prepare_normalized_harness("pi", *args, **kwargs)


def _default_executor(
    argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], timeout_s: float
) -> _ProcessCapture:
    completed = subprocess.run(
        list(argv), cwd=str(cwd), env=dict(env), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout_s, check=False,
    )
    return _ProcessCapture(
        returncode=int(completed.returncode),
        stdout=str(completed.stdout or ""),
        stderr=str(completed.stderr or ""),
    )


def _safe_process_error(returncode: int, stderr: str) -> str:
    text = str(stderr or "").lower()
    if returncode == 0:
        return ""
    if "no api key" in text or "authentication" in text or "unauthorized" in text:
        return "auth_rejected"
    if "model" in text and ("not found" in text or "unavailable" in text):
        return "model_unavailable"
    if "connect" in text or "econnrefused" in text or "fetch failed" in text:
        return "sidecar_unavailable"
    if "permission" in text or "access denied" in text:
        return "tool_permission_denied"
    return "harness_exit_nonzero"


_IPYTHON_EDIT_RE = re.compile(
    r"\b(?:apply_patch|write_text|write_bytes|touch|rename|replace|unlink|mkdir|makedirs)\b"
    r"|\bopen\s*\([^\n]{0,500},\s*['\"](?:w|a|x|r\+|w\+|a\+|x\+)"
    r"|\b(?:shutil\.)?(?:copy|copy2|copyfile|move)\s*\(",
    re.IGNORECASE,
)


def _ipython_edit_intent(args: object) -> bool:
    """Recognize file-mutating Python without retaining its potentially secret text."""
    try:
        material = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        material = str(type(args).__name__)
    return bool(_IPYTHON_EDIT_RE.search(material[:256_000]))


def _trace_evidence(
    harness: str, stdout: str
) -> tuple[dict[str, int], int, int, bool, bool, int]:
    allowed = {"ipython"} if harness == "prime" else {"read", "bash", "edit", "write"}
    counts = {name: 0 for name in sorted(allowed)}
    starts: dict[tuple[str, str], list[bool]] = {}
    native_calls = 0
    native_edits = 0
    agent_end = False
    error_seen = False
    invalid_lines = 0
    for raw in str(stdout or "").splitlines():
        try:
            event = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            invalid_lines += 1
            continue
        if not isinstance(event, dict):
            invalid_lines += 1
            continue
        kind = str(event.get("type") or "")
        if kind == "agent_end":
            agent_end = True
        if kind in {"error", "auto_retry_end"} and (
            kind == "error" or event.get("success") is False
        ):
            error_seen = True
        if kind == "message_end":
            message = event.get("message")
            if isinstance(message, dict) and message.get("stopReason") in {"error", "aborted"}:
                error_seen = True
        name = str(event.get("toolName") or "")
        call_id = str(event.get("toolCallId") or "")
        key = (name, call_id)
        if kind == "tool_execution_start" and name in allowed and call_id:
            edit_intent = name in {"edit", "write"} or (
                name == "ipython" and _ipython_edit_intent(event.get("args"))
            )
            starts.setdefault(key, []).append(edit_intent)
        elif kind == "tool_execution_end" and name in allowed and call_id:
            pending = starts.get(key) or []
            if pending:
                edit_intent = pending.pop(0)
                native_calls += 1
                if event.get("isError") is not True:
                    counts[name] += 1
                    if edit_intent:
                        native_edits += 1
    return counts, native_calls, native_edits, agent_end, error_seen, invalid_lines


def run_normalized_harness(
    launch: NormalizedLaunch,
    *,
    timeout_s: float = 900,
    executor: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Run an already-prepared plan and return bounded, prompt-free evidence.

    The result intentionally excludes stdout, stderr, messages, arguments, tool
    payloads, and the prompt.  A benchmark worker may persist this dictionary.
    Tests inject ``executor``; the default executes exactly ``launch.argv``.
    """
    if launch.harness not in PINS or launch.pin != PINS[launch.harness]:
        raise ValueError("launch does not carry an admitted Prime/Pi pin")
    if timeout_s <= 0:
        raise ValueError("timeout_s must be positive")
    execute = executor or _default_executor
    started = time.monotonic()
    try:
        capture = execute(
            launch.argv, cwd=launch.cwd, env=launch.env, timeout_s=float(timeout_s)
        )
        returncode = int(capture.returncode)
        stdout = str(capture.stdout or "")
        stderr = str(capture.stderr or "")
        timed_out = False
    except (subprocess.TimeoutExpired, TimeoutError):
        returncode = -1
        stdout = ""
        stderr = ""
        timed_out = True

    (tool_counts, native_calls, native_edits, agent_end, error_seen,
     invalid_lines) = _trace_evidence(launch.harness, stdout)
    category = "timeout" if timed_out else _safe_process_error(returncode, stderr)
    if not category and error_seen:
        category = "model_or_tool_error"
    if not category and not agent_end:
        category = "incomplete_json_trace"
    outcome = "completed" if not category else "invalid"
    return {
        "schema_version": 1,
        "harness": launch.harness,
        "outcome": outcome,
        "returncode": returncode,
        "safe_error_category": category,
        "timed_out": timed_out,
        "native_tool_success_counts": tool_counts,
        "native_tool_success_total": sum(tool_counts.values()),
        "tool_evidence": {
            "native_tool_calls": native_calls,
            "native_edit_calls": native_edits,
            "terminal_observed": agent_end,
        },
        "agent_end_observed": agent_end,
        "invalid_jsonl_lines": invalid_lines,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "runtime": {
            "product": launch.harness,
            "expected_version": launch.pin.version,
            "source_commit": launch.pin.commit,
            "provider": launch.provider,
            "model": launch.model,
            "thinking": launch.thinking,
            "surface": "openai_compatible_subscription_sidecar",
        },
        "raw_output_persisted": False,
        "prompt_persisted": False,
    }


def run_prime(launch: NormalizedLaunch, **kwargs) -> dict[str, Any]:
    if launch.harness != "prime":
        raise ValueError("run_prime requires a Prime launch")
    return run_normalized_harness(launch, **kwargs)


def run_pi(launch: NormalizedLaunch, **kwargs) -> dict[str, Any]:
    if launch.harness != "pi":
        raise ValueError("run_pi requires a Pi launch")
    return run_normalized_harness(launch, **kwargs)


__all__ = [
    "API", "AUTH_SENTINEL", "FORBIDDEN_AUTH_ENV", "MODEL", "PINS",
    "PI_COMMIT", "PI_VERSION", "PRIME_COMMIT", "PRIME_VERSION", "PROVIDER",
    "THINKING", "HarnessPin", "NormalizedLaunch", "prepare_normalized_harness",
    "prepare_pi", "prepare_prime", "run_normalized_harness", "run_pi", "run_prime",
]
