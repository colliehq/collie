"""Recorded comparison the read-batch envelope was waiting for: fewer round trips, same outcomes?

Real Collie loop (make_harness -> Harness.run) on the claude-agent-sdk route with its product
default model, read_batch off vs on, on three small multi-file repair tasks. Everything Collie keeps
is redirected to this experiment directory (settings, MCP config, data, sessions, state); HOME stays
real only so the SDK's CLI finds the existing Claude login. Desktop, screenshot and MCP-management
tools are removed in both arms. The grader re-runs the unit tests outside the agent and checks that
the test file is byte-identical.
"""
import hashlib
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
# Everything a run writes goes here (settings, MCP config, memory, sessions, work folders).
EXP = os.path.abspath(os.environ.get("READ_BATCH_EXPERIMENT_DIR") or
                      os.path.join(REPO, ".read-batch-experiment"))
os.makedirs(EXP, exist_ok=True)
with open(os.path.join(EXP, "settings.json"), "w", encoding="utf-8") as f:
    json.dump({}, f)
with open(os.path.join(EXP, "mcp.json"), "w", encoding="utf-8") as f:
    json.dump({"servers": {}}, f)
os.environ.update({
    "COLLIE_SETTINGS_PATH": os.path.join(EXP, "settings.json"),
    "COLLIE_MCP_CONFIG": os.path.join(EXP, "mcp.json"),
    "COLLIE_DATA_DIR": os.path.join(EXP, "data"),
    "COLLIE_SESSIONS_DIR": os.path.join(EXP, "data", "sessions"),
    "COLLIE_STATE_DIR": os.path.join(EXP, "state"),
    "COLLIE_EMBED": "bm25",
})
sys.path.insert(0, REPO)

from harness.cli import make_harness  # noqa: E402

OUT = os.path.join(EXP, "results.jsonl")

TASKS = {
    "config": {
        "files": {
            "app/__init__.py": "",
            "app/defaults.py": 'DEFAULTS = {"retries": 3, "timeout": 30,\n'
                               '            "http": {"proxy": None, "verify": True, "headers": {"ua": "app"}}}\n',
            "app/env.py": ('import os\n\n\ndef overrides(environ=None):\n'
                           '    """APP_<SECTION>__<KEY>=value -> nested dict; "true"/"false" become bools."""\n'
                           '    environ = os.environ if environ is None else environ\n'
                           '    out = {}\n'
                           '    for name, value in environ.items():\n'
                           '        if not name.startswith("APP_"):\n'
                           '            continue\n'
                           '        path = name[4:].lower().split("__")\n'
                           '        node = out\n'
                           '        for part in path[:-1]:\n'
                           '            node = node.setdefault(part, {})\n'
                           '        node[path[-1]] = _coerce(value)\n'
                           '    return out\n\n\n'
                           'def _coerce(value):\n'
                           '    if value.lower() in ("true", "false"):\n'
                           '        return value == "true"\n'
                           '    if value.isdigit():\n'
                           '        return int(value)\n'
                           '    return value\n'),
            "app/merge.py": ('def merge(base, extra):\n'
                             '    """Deep-merge extra into a copy of base; nested dicts merge key by key."""\n'
                             '    out = dict(base)\n'
                             '    for key, value in extra.items():\n'
                             '        out[key] = value\n'
                             '    return out\n'),
            "app/config.py": ('from .defaults import DEFAULTS\nfrom .env import overrides\nfrom .merge import merge\n\n\n'
                              'def load(environ=None):\n    return merge(DEFAULTS, overrides(environ))\n'),
        },
        "tests": ('import unittest\n\nfrom app.config import load\n\n\n'
                  'class ConfigTests(unittest.TestCase):\n'
                  '    def test_defaults(self):\n'
                  '        self.assertEqual(load({})["http"]["verify"], True)\n\n'
                  '    def test_nested_override_keeps_siblings(self):\n'
                  '        cfg = load({"APP_HTTP__PROXY": "http://p:8080"})\n'
                  '        self.assertEqual(cfg["http"]["proxy"], "http://p:8080")\n'
                  '        self.assertEqual(cfg["http"]["headers"], {"ua": "app"})\n\n'
                  '    def test_boolean_is_case_insensitive(self):\n'
                  '        self.assertIs(load({"APP_HTTP__VERIFY": "FALSE"})["http"]["verify"], False)\n'
                  '        self.assertIs(load({"APP_HTTP__VERIFY": "True"})["http"]["verify"], True)\n\n'
                  '    def test_deep_header_override(self):\n'
                  '        cfg = load({"APP_HTTP__HEADERS__UA": "cli"})\n'
                  '        self.assertEqual(cfg["http"]["headers"], {"ua": "cli"})\n'
                  '        self.assertEqual(cfg["retries"], 3)\n\n\n'
                  'if __name__ == "__main__":\n    unittest.main()\n'),
        "test_name": "test_config.py",
    },
    "pricing": {
        "files": {
            "shop/__init__.py": "",
            "shop/money.py": ('from decimal import Decimal, ROUND_HALF_UP\n\n\n'
                              'def cents(value):\n'
                              '    """Round to whole cents, halves away from zero."""\n'
                              '    return Decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)\n'),
            "shop/discounts.py": ('from decimal import Decimal\n\n\n'
                                  'def apply(subtotal, codes):\n'
                                  '    """Percentage codes first, then fixed-amount codes; never below zero."""\n'
                                  '    total = Decimal(subtotal)\n'
                                  '    for code in codes:\n'
                                  '        if code.endswith("OFF"):\n'
                                  '            total -= Decimal(code[:-3])\n'
                                  '        elif code.endswith("PCT"):\n'
                                  '            total -= total * Decimal(code[:-3]) / 100\n'
                                  '    return max(total, Decimal("0"))\n'),
            "shop/tax.py": ('from decimal import Decimal\n\nRATES = {"CA": Decimal("0.0725"), "OR": Decimal("0")}\n\n\n'
                            'def tax(amount, state):\n    return amount * RATES[state]\n'),
            "shop/cart.py": ('from decimal import Decimal\n\nfrom .discounts import apply\nfrom .money import cents\nfrom .tax import tax\n\n\n'
                             'def total(lines, codes=(), state="CA"):\n'
                             '    subtotal = sum(Decimal(str(price)) * qty for price, qty in lines)\n'
                             '    discounted = apply(subtotal, codes)\n'
                             '    return cents(discounted) + cents(tax(discounted, state))\n'),
        },
        "tests": ('import unittest\nfrom decimal import Decimal\n\nfrom shop.cart import total\n\n\n'
                  'class CartTests(unittest.TestCase):\n'
                  '    def test_plain(self):\n'
                  '        self.assertEqual(total([(10, 2)], state="OR"), Decimal("20.00"))\n\n'
                  '    def test_percent_applies_before_fixed_whatever_the_order(self):\n'
                  '        self.assertEqual(total([(100, 1)], ["10OFF", "20PCT"], state="OR"), Decimal("70.00"))\n\n'
                  '    def test_unknown_state_is_untaxed_rather_than_an_error(self):\n'
                  '        self.assertEqual(total([(5, 1)], state="NV"), Decimal("5.00"))\n\n'
                  '    def test_tax_rounds_half_up(self):\n'
                  '        self.assertEqual(total([(Decimal("0.20"), 1)], state="CA"), Decimal("0.21"))\n\n\n'
                  'if __name__ == "__main__":\n    unittest.main()\n'),
        "test_name": "test_cart.py",
    },
    "report": {
        "files": {
            "report/__init__.py": "",
            "report/parse.py": ('import csv\nimport io\n\n\n'
                                'def rows(text):\n'
                                '    """CSV with a header row -> list of dicts (values stripped)."""\n'
                                '    return [{k: v for k, v in row.items()} for row in csv.DictReader(io.StringIO(text))]\n'),
            "report/aggregate.py": ('def by_team(rows):\n'
                                    '    """Sum hours per team; team names are case- and space-insensitive."""\n'
                                    '    out = {}\n'
                                    '    for row in rows:\n'
                                    '        team = row["team"]\n'
                                    '        out[team] = out.get(team, 0) + float(row["hours"])\n'
                                    '    return out\n'),
            "report/fmt.py": ('def table(totals):\n'
                              '    """Two columns, team left-aligned to the longest name, hours with one decimal, sorted by hours desc."""\n'
                              '    width = max(len(t) for t in totals)\n'
                              '    lines = []\n'
                              '    for team, hours in sorted(totals.items()):\n'
                              '        lines.append("%s  %.1f" % (team.ljust(width), hours))\n'
                              '    return "\\n".join(lines)\n'),
            "report/main.py": ('from .aggregate import by_team\nfrom .fmt import table\nfrom .parse import rows\n\n\n'
                               'def render(text):\n    return table(by_team(rows(text)))\n'),
        },
        "tests": ('import unittest\n\nfrom report.main import render\n\n'
                  'CSV = "team,hours\\n Core ,3\\ncore,2.5\\nWeb,4\\nweb ,1\\nData,2\\n"\n\n\n'
                  'class ReportTests(unittest.TestCase):\n'
                  '    def test_render(self):\n'
                  '        self.assertEqual(render(CSV), "core  5.5\\nweb   5.0\\ndata  2.0")\n\n\n'
                  'if __name__ == "__main__":\n    unittest.main()\n'),
        "test_name": "test_report.py",
    },
}

PROMPT = ("The unit tests in this folder fail. Fix the source (not the tests) so that "
          "`python -m unittest -q` passes, and run the tests to confirm. Do not modify {test}.")


def one(task, arm, rep):
    spec = TASKS[task]
    work = os.path.join(EXP, "runs", "%s-%s-%d-%d" % (task, arm, rep, int(time.time())))
    for rel, text in spec["files"].items():
        path = os.path.join(work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    with open(os.path.join(work, spec["test_name"]), "w", encoding="utf-8", newline="\n") as f:
        f.write(spec["tests"])
    digest = hashlib.sha256(spec["tests"].encode()).hexdigest()
    h = make_harness(work, provider="claude-agent-sdk", code_search=True, exec_code=True,
                     project="read-batch-probe")
    for name in list(h.registry._tools):
        if name.startswith(("desktop_", "mcpctl_", "mcp__")) or name in (
                "screenshot", "live_copilot", "enable_capability"):
            del h.registry._tools[name]
    h.provider.read_batch = arm == "on"
    h.max_turns = 40
    responses = []
    real_complete = h.provider.complete

    def complete(system, messages, tool_schemas, on_text=None):
        t0 = time.time()
        c = real_complete(system, messages, tool_schemas, on_text)
        names = [tc.name for tc in c.tool_calls]
        responses.append({"s": round(time.time() - t0, 1), "tools": names,
                          "stop": c.stop_reason, "err": (c.text or "")[:160] if c.stop_reason == "error" else ""})
        return c

    h.provider.complete = complete
    t0 = time.time()
    res = h.run("rb-%s-%s-%d" % (task, arm, rep), PROMPT.format(test=spec["test_name"]))
    wall = time.time() - t0
    graded = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=work,
                            capture_output=True, text=True, timeout=120)
    with open(os.path.join(work, spec["test_name"]), encoding="utf-8") as f:
        untouched = hashlib.sha256(f.read().encode()).hexdigest() == digest
    row = {"task": task, "arm": arm, "rep": rep, "wall_s": round(wall, 1),
           "responses": len(responses), "model_calls": res.model_calls, "turns": res.turns,
           "passed": graded.returncode == 0 and untouched, "tests_untouched": untouched,
           "error": (res.error or "")[:200],
           "batched_responses": sum(1 for r in responses if r["tools"].count("read_file") > 1),
           "reads": sum(r["tools"].count("read_file") for r in responses),
           "input_tokens": res.input_tokens, "output_tokens": res.output_tokens,
           "cache_read": res.cache_read, "trace": responses}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
    print(json.dumps({k: v for k, v in row.items() if k != "trace"}), flush=True)


if __name__ == "__main__":
    tasks = sys.argv[1].split(",") if len(sys.argv) > 1 else list(TASKS)
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    for rep in range(reps):
        for task in tasks:
            order = ["off", "on"] if (rep + len(task)) % 2 == 0 else ["on", "off"]
            for arm in order:
                one(task, arm, rep)
