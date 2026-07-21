"""Side-by-side comparison: collie vs Claude Code, same task, same schema.

- run_collie     : run a task through this harness (any provider).
- run_cc     : run the SAME task through `claude -p --output-format json` and
               record real usage (input/output/cache tokens, turns, duration).
- cc_baseline: insert one reference row from the MEASURED Claude Code steady
               prefix (~17K, from the 2026-07-05 report) so the dashboard can draw
               the prefix contrast without spending, clearly labelled as an
               estimate rather than a per-task execution.

Everything lands in the same runs.db, so the dashboard compares them directly.
"""
from __future__ import annotations
import json
import os
import re
import subprocess
import time

from .recorder import Recorder, RunResult


def _num_in(a, n):
    """Word-boundary match for a number, so "7" doesn't false-pass on "17"/"27"/"test_7"/"7.2"."""
    return re.search(r'(?<![\d.])%d(?![\d.])' % int(n), a or "") is not None

# Measured Claude Code steady-state prefix on the report machine (see §01/§09).
CC_MEASURED_PREFIX = 17000


# --------------------------------------------------------------------------- #
#  Benchmark task suite (deterministic; runs against a built sandbox)
# --------------------------------------------------------------------------- #
FIXTURES = {
    "pkg/__init__.py": '"""demo package used by collie\'s benchmark tasks."""\n',
    "pkg/money.py": (
        'class Account:\n'
        '    """A simple bank account."""\n\n'
        '    def __init__(self, balance=0):\n'
        '        self.balance = balance\n\n'
        '    def deposit(self, amt):\n'
        '        self.balance += amt\n'
        '        return self.balance\n\n'
        '    def withdraw(self, amt):\n'
        '        # BUG: allows overdraft; should raise ValueError if amt > balance\n'
        '        self.balance -= amt\n'
        '        return self.balance\n'),
    "pkg/stats.py": (
        'def mean(xs):\n'
        '    # BUG: crashes on empty list; should return 0.0\n'
        '    return sum(xs) / len(xs)\n\n\n'
        'def median(xs):\n'
        '    s = sorted(xs)\n'
        '    n = len(s)\n'
        '    if n == 0:\n'
        '        return 0.0\n'
        '    m = n // 2\n'
        '    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2\n'),
    "pkg/text.py": (
        'def truncate(s, n):\n'
        '    # TODO: return s unchanged if len(s) <= n, else first n chars + "..."\n'
        '    return s\n\n\n'
        '# slugify(s) is not implemented yet\n'),
    "tests/test_money.py": (
        'import pytest\n'
        'from pkg.money import Account\n\n\n'
        'def test_deposit():\n    assert Account().deposit(10) == 10\n\n\n'
        'def test_overdraft():\n'
        '    a = Account(5)\n'
        '    with pytest.raises(ValueError):\n'
        '        a.withdraw(10)\n'),
    "tests/test_stats.py": (
        'from pkg.stats import mean, median\n\n\n'
        'def test_mean():\n    assert mean([2, 4]) == 3\n\n\n'
        'def test_mean_empty():\n    assert mean([]) == 0.0\n\n\n'
        'def test_median():\n    assert median([1, 2, 3]) == 2\n'),
    "tests/test_text.py": (
        'from pkg.text import truncate\n\n\n'
        'def test_truncate_short():\n    assert truncate("hi", 5) == "hi"\n\n\n'
        'def test_truncate_long():\n    assert truncate("hello world", 5) == "hello..."\n'),
    "README.md": "# demo package\nA small library fixture for collie's benchmark tasks.\n",
}


def build_sandbox(root: str, pristine: bool = False) -> dict:
    """Write a small real Python package (multi-module, real test suite, seeded
    bugs). Also used to RESET between runs so edit tasks are fair."""
    if pristine:
        # wipe the package/test dirs first so one arm's AGENT-CREATED files (a new module, a stray
        # conftest.py, __pycache__) can't leak in and satisfy the NEXT arm's check — that biased the
        # head-to-head by whoever ran first. Only touch the sandbox's own dirs, never the root.
        import shutil
        for d in ("pkg", "tests"):
            shutil.rmtree(os.path.join(root, d), ignore_errors=True)
    for rel, content in FIXTURES.items():
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p) or root, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
    # 7 .py: pkg/{__init__,money,stats,text} + tests/{money,stats,text}
    # initially 3 tests fail: test_overdraft, test_mean_empty, test_truncate_long
    return {"n_py": 7, "n_fail": 3}


def reset_sandbox(root: str) -> dict:
    """Pristine reset BETWEEN comparison arms — wipes agent-created files, not just the fixtures."""
    return build_sandbox(root, pristine=True)


def _run_py(cwd, code, timeout=30):
    r = subprocess.run([__import__("sys").executable, "-c", code], cwd=cwd, capture_output=True,
                       text=True, timeout=timeout)
    return (r.stdout + r.stderr).strip(), r.returncode


def _pytest_passes(cwd, target, timeout=60):
    try:
        r = subprocess.run([__import__("sys").executable, "-m", "pytest", "-q", target], cwd=cwd,
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except Exception:
        return False


def basic_tasks(facts: dict) -> list[dict]:
    """Mock-friendly tasks (no editing) — used by selftest."""
    npy = facts["n_py"]
    return [
        {"id": "count_py",
         "prompt": "How many Python (.py) files are in this project? Answer with the number.",
         "check": lambda a, cwd: _num_in(a, npy)},          # word-boundary: "7" must not match "17"
        {"id": "find_todo",
         "prompt": "Which file contains a TODO comment? Answer with the filename.",
         "check": lambda a, cwd: "text.py" in a},
        {"id": "recall_design",
         "prompt": "What did we decide about the embedding memory design? Recall from memory.",
         # discriminating tokens only — "embedding" is in the PROMPT, so a refusal/echo false-passed.
         "check": lambda a, cwd: any(k in a.lower() for k in ("jina", "hybrid", "sqlite", "fastembed", "bge", "fts"))},
    ]


def full_tasks(facts: dict) -> list[dict]:
    """Real repo-level tasks: fix failing tests, implement a function, run the
    suite — all verified by actually running code. Needs a real model."""
    def chk_overdraft(a, cwd):
        return _pytest_passes(cwd, "tests/test_money.py::test_overdraft")

    def chk_mean_empty(a, cwd):
        return _pytest_passes(cwd, "tests/test_stats.py::test_mean_empty")

    def chk_truncate(a, cwd):
        return _pytest_passes(cwd, "tests/test_text.py::test_truncate_long")

    def chk_slugify(a, cwd):
        out, rc = _run_py(cwd, "from pkg.text import slugify; print(slugify('Hello World'))")
        return rc == 0 and bool(out) and out.splitlines()[-1].strip() == "hello-world"

    def chk_count_fail(a, cwd):
        return _num_in(a, facts.get("n_fail", 3))          # word-boundary: "3" must not match "13"

    return basic_tasks(facts) + [
        {"id": "explain_median",
         "prompt": "In one sentence, what does the function `median` in pkg/stats.py compute?",
         # "median" is the prompt echo — require a real description (middle/sorted/中位…).
         "check": lambda a, cwd: any(k in a.lower() for k in
                                     ("middle", "sorted", "中位", "中间"))},
        {"id": "find_account",
         "prompt": "Which file defines the class `Account`? Answer with just the path.",
         "check": lambda a, cwd: "money.py" in a},
        {"id": "count_failing",
         "prompt": "Run the test suite. How many tests currently FAIL? Answer with the number.",
         "check": chk_count_fail},
        {"id": "fix_overdraft",
         "prompt": "Bug: Account.withdraw in pkg/money.py allows overdraft. Fix it so that "
                   "withdrawing more than the balance raises ValueError (test_overdraft).",
         "check": chk_overdraft},
        {"id": "fix_mean_empty",
         "prompt": "Bug: mean([]) in pkg/stats.py crashes on an empty list. Fix it to return "
                   "0.0 for an empty list (test_mean_empty).",
         "check": chk_mean_empty},
        {"id": "impl_truncate",
         "prompt": "Implement truncate(s, n) in pkg/text.py: return s unchanged if len(s) <= n, "
                   "else the first n characters followed by '...' (test_truncate_long).",
         "check": chk_truncate},
        {"id": "impl_slugify",
         "prompt": "Add a function slugify(s) in pkg/text.py that lowercases s and replaces "
                   "spaces with hyphens. E.g. 'Hello World' -> 'hello-world'.",
         "check": chk_slugify},
    ]


def task_suite(facts: dict, full: bool = True) -> list[dict]:
    return full_tasks(facts) if full else basic_tasks(facts)


# --------------------------------------------------------------------------- #
def run_collie(harness, task: dict) -> RunResult:
    try:
        res = harness.run(task["id"], task["prompt"])
    except Exception as e:
        # a crashed collie arm must be a recorded FAIL, not an escaped exception — else it silently
        # drops out of the denominator that the CC arm (which catches its own crashes) is charged on.
        rid = harness.recorder.start_run(task["id"], "collie", getattr(harness.provider, "model", "?"),
                                         getattr(harness.provider, "name", "?"), note="crash")
        res = RunResult(run_id=rid, task_id=task["id"], harness="collie",
                        error="%s: %s" % (type(e).__name__, e), success=False)
        harness.recorder.finish_run(res)
        return res
    try:
        res.success = bool(task["check"](res.answer, harness.cwd))
    except Exception:
        res.success = False
    harness.recorder.finish_run(res)   # re-persist success flag
    return res


def grade_and_cost(res: RunResult, task_prompt: str, judge_provider=None) -> RunResult:
    """Attach estimated $ cost and an LLM-judge quality score (0-10) to a run."""
    from . import costs, judge as _judge
    res.cost_usd = costs.cost_usd(res.model, res.input_tokens, res.output_tokens,
                                  res.cache_read, res.cache_creation)
    res.quality = _judge.judge_quality(judge_provider, task_prompt, res.answer, res.success)
    return res


def run_cc(recorder: Recorder, task: dict, cwd: str, model: str = "",
           max_turns: int = 8, timeout: int = 240) -> RunResult:
    """Run the task through Claude Code headless and record real metrics."""
    rid = recorder.start_run(task["id"], "cc", model or "claude-code", "claude-code",
                             note="real")
    res = RunResult(run_id=rid, task_id=task["id"], harness="cc",
                    model=model or "claude-code", provider="claude-code")
    t0 = time.time()
    # bypassPermissions to match every other CC path (adapters.py, swe.predict_claude_code):
    # without it, headless `claude -p` auto-DENIES tool use in the throwaway sandbox, so CC returns
    # degraded/empty answers and loses tasks it could solve — an unfair asymmetry favoring collie.
    cmd = ["claude", "-p", task["prompt"], "--output-format", "json",
           "--permission-mode", "bypassPermissions", "--max-turns", str(max_turns)]
    if model:
        cmd += ["--model", model]
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        data = _parse_cc_json(r.stdout)
        u = data.get("usage", {}) or {}
        res.input_tokens = u.get("input_tokens", 0)
        res.output_tokens = u.get("output_tokens", 0)
        res.cache_read = u.get("cache_read_input_tokens", 0)
        res.cache_creation = u.get("cache_creation_input_tokens", 0)
        # use the REAL measured cache-read as the prefix for a live run — don't inject the synthetic
        # CC_MEASURED_PREFIX (17000) when cache_read==0, which fabricated CC's efficiency numbers in
        # the head-to-head. The synthetic constant stays only on the explicit cc_baseline row.
        res.prefix_tokens = res.cache_read
        res.total_tokens = (res.input_tokens + res.output_tokens + res.cache_read +
                            u.get("cache_creation_input_tokens", 0))
        res.turns = data.get("num_turns", 0)
        res.answer = str(data.get("result", ""))[:2000]
        try:
            # checks are 2-arg (answer, cwd) — matching run_collie. Calling with one arg raised
            # TypeError, was swallowed below, and pinned EVERY Claude Code run to success=False,
            # invalidating the whole head-to-head (baseline forced to 0%).
            res.success = bool(task["check"](res.answer, cwd))
        except Exception:
            res.success = False
        if data.get("is_error"):
            res.error = "cc reported is_error"
    except FileNotFoundError:
        res.error = "claude CLI not found on PATH"
    except subprocess.TimeoutExpired:
        res.error = "cc timed out after %ds" % timeout
    except Exception as e:
        res.error = "%s: %s" % (type(e).__name__, e)
    res.wall_ms = int((time.time() - t0) * 1000)
    recorder.finish_run(res)
    return res


def cc_baseline(recorder: Recorder, task_id: str = "*") -> None:
    """Insert a labelled reference row from the measured CC prefix (no spend)."""
    rid = recorder.start_run(task_id, "cc-baseline", "claude-code(measured)",
                             "measured", note="2026-07-05 report §01, not executed")
    res = RunResult(run_id=rid, task_id=task_id, harness="cc-baseline",
                    model="claude-code(measured)", provider="measured",
                    prefix_tokens=CC_MEASURED_PREFIX, cache_read=CC_MEASURED_PREFIX,
                    total_tokens=CC_MEASURED_PREFIX, turns=0, success=False,
                    answer="(reference prefix only — run `compare --vs claude --real` for a live head-to-head)")
    recorder.finish_run(res)


def baseline(recorder: Recorder, key: str, task_id: str = "*") -> bool:
    """Insert a measured-prefix reference row for any harness that has one."""
    from .adapters import MEASURED_PREFIX, ADAPTERS
    pref = MEASURED_PREFIX.get(key)
    if not pref:
        return False
    label = ADAPTERS[key].label if key in ADAPTERS else key
    rid = recorder.start_run(task_id, key + "-baseline", label + "(measured)",
                             "measured", note="measured prefix, not executed")
    res = RunResult(run_id=rid, task_id=task_id, harness=key + "-baseline",
                    model=label + "(measured)", provider="measured",
                    prefix_tokens=pref, cache_read=pref, total_tokens=pref,
                    turns=0, success=False, answer="(reference prefix only)")
    recorder.finish_run(res)
    return True


def _parse_cc_json(stdout: str) -> dict:
    stdout = (stdout or "").strip()
    if not stdout:
        return {}
    try:
        obj = json.loads(stdout)
        if isinstance(obj, list):     # stream-json array -> take last result obj
            for item in reversed(obj):
                if isinstance(item, dict) and item.get("type") == "result":
                    return item
            return obj[-1] if obj else {}
        return obj
    except json.JSONDecodeError:
        for line in reversed(stdout.splitlines()):     # ndjson fallback
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
    return {}
