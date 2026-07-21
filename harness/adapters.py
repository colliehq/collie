"""Multi-harness comparison adapters.

Wrap each mainstream coding-agent CLI behind one uniform interface so the same
task can be run through any of them and recorded to runs.db with the SAME schema
as `collie`. Auto-discovery skips CLIs that aren't installed.

    HarnessAdapter                     key / label / cli / usage_supported
      .available()                     is the CLI on PATH?
      .run(task, cwd, recorder, ...)   -> RunResult (uniform metrics)

Not every CLI exposes token usage in a machine-readable way; adapters set
`usage_supported` honestly and record whatever is available (duration, turns,
answer, success). Where a CLI gives no tokens, prefix/input/output stay 0 and the
dashboard shows the run without fabricated numbers.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import time

from .recorder import Recorder, RunResult

# Only Claude Code has a measured steady prefix (2026-07-05 report §01). Others
# have no credible measured baseline here — left None rather than guessed.
MEASURED_PREFIX = {"claude": 17000}


class HarnessAdapter:
    key = ""            # short id used as runs.harness value
    label = ""          # human label
    cli = ""            # executable name on PATH
    usage_supported = False
    extra_env: dict = {}   # merged into subprocess env (e.g. ANTHROPIC_BASE_URL)

    def available(self) -> bool:
        return bool(shutil.which(self.cli))

    def build_cmd(self, prompt: str, model: str) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, stderr: str) -> dict:
        """Return dict: input_tokens, output_tokens, cache_read, cache_creation,
        num_turns, result (text)."""
        return {"result": stdout.strip()}

    def run(self, task: dict, cwd: str, recorder: Recorder, model: str = "",
            max_turns: int = 8, timeout: int = 300) -> RunResult:
        rid = recorder.start_run(task["id"], self.key, model or self.label, self.key,
                                 note="real")
        res = RunResult(run_id=rid, task_id=task["id"], harness=self.key,
                        model=model or self.label, provider=self.key)
        t0 = time.time()
        if not self.available():
            res.error = "%s not installed" % self.cli
            res.wall_ms = int((time.time() - t0) * 1000)
            recorder.finish_run(res)
            return res
        try:
            env = None
            if self.extra_env:
                env = {**os.environ}
                for k, v in self.extra_env.items():
                    if v is None:
                        env.pop(k, None)        # unset (e.g. a shadowing key)
                    else:
                        env[k] = v
            self._max_turns = max_turns          # let build_cmd honor the caller's turn budget
            r = subprocess.run(self.build_cmd(task["prompt"], model), cwd=cwd,
                               capture_output=True, text=True, timeout=timeout, env=env)
            d = self.parse(r.stdout, r.stderr)
            res.input_tokens = d.get("input_tokens", 0)
            res.output_tokens = d.get("output_tokens", 0)
            res.cache_read = d.get("cache_read", 0)
            res.cache_creation = d.get("cache_creation", 0)
            # fixed prefix = cached prefix if caching is on, else the whole input
            # (system prefix dominates a tiny task), else a measured fallback.
            res.prefix_tokens = (res.cache_read or res.input_tokens
                                 or MEASURED_PREFIX.get(self.key, 0))
            res.total_tokens = (res.input_tokens + res.output_tokens + res.cache_read +
                                d.get("cache_creation", 0))
            res.turns = d.get("num_turns", 0)
            res.answer = str(d.get("result", ""))[:2000]
            try:
                res.success = bool(task["check"](res.answer, cwd))
            except Exception:
                res.success = False
        except subprocess.TimeoutExpired:
            res.error = "timed out after %ds" % timeout
        except Exception as e:
            res.error = "%s: %s" % (type(e).__name__, e)
        res.wall_ms = int((time.time() - t0) * 1000)
        recorder.finish_run(res)
        return res


# --------------------------------------------------------------------------- #
class ClaudeCodeAdapter(HarnessAdapter):
    key, label, cli, usage_supported = "claude", "Claude Code", "claude", True

    def build_cmd(self, prompt, model):
        # bypassPermissions so headless CC can actually run read-only tools
        # (find/grep/read) in the throwaway sandbox instead of refusing.
        # honor the caller's max_turns (set by run()) instead of a hard-coded 8, so CC isn't
        # capped shorter than collie when the turn budget is tuned in a comparison.
        cmd = ["claude", "-p", prompt, "--output-format", "json",
               "--max-turns", str(getattr(self, "_max_turns", 8)),
               "--permission-mode", "bypassPermissions"]
        return cmd + (["--model", model] if model else [])

    def parse(self, stdout, stderr):
        d = _last_json(stdout)
        u = d.get("usage", {}) or {}
        return {"input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "cache_read": u.get("cache_read_input_tokens", 0),
                "cache_creation": u.get("cache_creation_input_tokens", 0),
                "num_turns": d.get("num_turns", 0),
                "result": d.get("result", stdout)}


class CodexAdapter(HarnessAdapter):
    key, label, cli, usage_supported = "codex", "OpenAI Codex CLI", "codex", True

    def build_cmd(self, prompt, model):
        cmd = ["codex", "exec", prompt, "--json"]
        return cmd + (["--model", model] if model else [])

    def parse(self, stdout, stderr):
        # codex exec --json emits JSONL events; find token usage + last message
        usage, result = {}, ""
        for line in stdout.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            t = ev.get("type", "")
            if "token" in t or "usage" in json.dumps(ev)[:200]:
                usage = ev.get("usage", ev) or usage
            if t in ("agent_message", "message", "assistant") or "message" in ev:
                result = ev.get("message") or ev.get("text") or result
        return {"input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "result": result or stdout.strip()}


class GeminiAdapter(HarnessAdapter):
    key, label, cli, usage_supported = "gemini", "Gemini CLI", "gemini", False

    def build_cmd(self, prompt, model):
        cmd = ["gemini", "-p", prompt]
        return cmd + (["-m", model] if model else [])

    def parse(self, stdout, stderr):
        return {"result": stdout.strip()}   # Gemini CLI prints text, no token JSON


class CursorAgentAdapter(HarnessAdapter):
    key, label, cli, usage_supported = "cursor", "Cursor Agent", "cursor-agent", True

    def build_cmd(self, prompt, model):
        cmd = ["cursor-agent", "-p", prompt, "--output-format", "json"]
        return cmd + (["--model", model] if model else [])

    def parse(self, stdout, stderr):
        d = _last_json(stdout)
        u = d.get("usage", {}) or {}
        return {"input_tokens": u.get("input_tokens", 0),
                "output_tokens": u.get("output_tokens", 0),
                "num_turns": d.get("num_turns", 0),
                "result": d.get("result") or d.get("text") or stdout.strip()}


class OpenCodeAdapter(HarnessAdapter):
    key, label, cli, usage_supported = "opencode", "OpenCode", "opencode", False

    def build_cmd(self, prompt, model):
        cmd = ["opencode", "run", prompt]
        return cmd + (["--model", model] if model else [])

    def parse(self, stdout, stderr):
        return {"result": stdout.strip()}


class PiAdapter(HarnessAdapter):
    # pi (@earendil-works/pi-coding-agent). On the flat Claude subscription via collie's oauth-proxy:
    # register a proxy-backed provider (the extension) + point pi at it. Needs `python -m
    # harness.oauth_proxy` running and ANTHROPIC_API_KEY set to any non-empty value (proxy ignores it).
    key, label, cli, usage_supported = "pi", "Pi", "pi", False
    extra_env = {"ANTHROPIC_API_KEY": "dummy-proxy-ignores-this"}
    _EXT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oauth_ext", "pi-oauth-proxy.js")

    def build_cmd(self, prompt, model):
        # model like "claude-opus-4-8"; the extension registers provider "claudesub" with it
        prov = "claudesub" if not model or model.startswith("claude") else "claudesub"
        cmd = ["pi", "-p", "--no-session", "--provider", prov, "--extension", self._EXT]
        if model:
            cmd += ["--model", model]
        return cmd + [prompt]

    def parse(self, stdout, stderr):
        return {"result": stdout.strip(), "num_turns": 0}


class AiderAdapter(HarnessAdapter):
    key, label, cli, usage_supported = "aider", "Aider", "aider", False

    def build_cmd(self, prompt, model):
        cmd = ["aider", "--message", prompt, "--yes", "--no-auto-commits", "--no-git"]
        return cmd + (["--model", model] if model else [])

    def parse(self, stdout, stderr):
        return {"result": stdout.strip()}


class OpenClawAdapter(HarnessAdapter):
    # Headless one-shot: `openclaw agent --agent <id> --message "<task>" --json --local`
    # (--local = run embedded, skip the gateway daemon). DeepSeek backend via
    # ~/.openclaw/openclaw.json model "deepseek/deepseek-chat". Needs Node 24.
    # --json emits {payloads:[{text}], meta:{durationMs}} — no token counts.
    key, label, cli, usage_supported = "openclaw", "OpenClaw", "openclaw", False
    agent_id = "main"                     # OpenClaw's default session
    extra_env = {"OPENAI_API_KEY": None}  # unset shadowing key -> use openclaw.json DeepSeek

    def build_cmd(self, prompt, model):
        return ["openclaw", "agent", "--agent", self.agent_id,
                "--message", prompt, "--json", "--local"]

    def parse(self, stdout, stderr):
        d = _last_json(stdout)
        text = ""
        for p in (d.get("payloads") or []):
            text += p.get("text", "")
        return {"result": text or stdout.strip(),
                "num_turns": 0}


class HermesAdapter(HarnessAdapter):
    # Headless one-shot: `hermes -z "<task>"` (final answer text only). DeepSeek via
    # ~/.hermes/config.yaml base_url https://api.deepseek.com/v1 + model deepseek-chat.
    # No machine-readable token count in headless output.
    key, label, cli, usage_supported = "hermes", "Hermes Agent", "hermes", False
    # unset a shadowing OPENAI_API_KEY so hermes reads its own ~/.hermes/.env (DeepSeek)
    extra_env = {"OPENAI_API_KEY": None}

    def build_cmd(self, prompt, model):
        # honor HERMES_PROVIDER/HERMES_MODEL as CLI FLAGS (env vars alone are NOT read by `hermes
        # -z`) — mirrors swe.predict_hermes. Without this the adapter silently ran the config
        # default (deepseek-chat) even when the caller asked for subscription Opus.
        cmd = ["hermes", "-z", prompt]
        hp, hm = os.environ.get("HERMES_PROVIDER"), os.environ.get("HERMES_MODEL")
        if hp:
            cmd += ["--provider", hp]
        if hm:
            cmd += ["-m", hm]
        return cmd

    def parse(self, stdout, stderr):
        return {"result": stdout.strip(), "num_turns": 0}


ADAPTERS = {a.key: a for a in (
    ClaudeCodeAdapter(), CodexAdapter(), GeminiAdapter(),
    CursorAgentAdapter(), OpenCodeAdapter(), PiAdapter(), AiderAdapter(),
    OpenClawAdapter(), HermesAdapter())}


def claude_on(base_url: str, model: str, auth_token: str = "x",
              key: str = "claude-alt", label: str = None) -> "ClaudeCodeAdapter":
    """Claude Code pointed at any Anthropic-compatible endpoint (local Ollama, or a
    cheap API like DeepSeek's /anthropic) — so collie and Claude Code run on the SAME
    model, isolating the harness variable."""
    a = ClaudeCodeAdapter()
    a.key = key
    a.label = label or ("Claude Code (%s)" % model)
    a.extra_env = {"ANTHROPIC_BASE_URL": base_url, "ANTHROPIC_AUTH_TOKEN": auth_token}
    return a


def claude_on_local(base_url: str = "http://localhost:11434",
                    model: str = "qwen2.5-coder:7b") -> "ClaudeCodeAdapter":
    return claude_on(base_url, model, "ollama", key="claude-local",
                     label="Claude Code (local:%s)" % model)


def discover() -> list[HarnessAdapter]:
    return [a for a in ADAPTERS.values() if a.available()]


def resolve(keys: list[str]) -> list[HarnessAdapter]:
    if keys == ["all"]:
        return list(ADAPTERS.values())
    if keys == ["discovered"]:
        return discover()
    return [ADAPTERS[k] for k in keys if k in ADAPTERS]


def _last_json(stdout: str) -> dict:
    stdout = (stdout or "").strip()
    if not stdout:
        return {}
    try:
        obj = json.loads(stdout)
        if isinstance(obj, list):
            for it in reversed(obj):
                if isinstance(it, dict) and it.get("type") == "result":
                    return it
            return obj[-1] if obj else {}
        return obj
    except json.JSONDecodeError:
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return {}
