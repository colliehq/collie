"""Oracle check for read_batch_probe tasks: baseline fails, a reference fix passes."""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("READ_BATCH_EXPERIMENT_DIR", tempfile.mkdtemp(prefix="rb-exp-"))
import read_batch_probe as rb  # noqa: E402

FIXES = {
    "config": {
        "app/env.py": lambda s: s.replace('return value == "true"', 'return value.lower() == "true"'),
        "app/merge.py": lambda s: s.replace(
            "        out[key] = value\n",
            "        if isinstance(value, dict) and isinstance(out.get(key), dict):\n"
            "            out[key] = merge(out[key], value)\n"
            "        else:\n"
            "            out[key] = value\n"),
    },
    "pricing": {
        "shop/discounts.py": lambda s: s.replace(
            "    for code in codes:\n",
            "    for code in sorted(codes, key=lambda c: 0 if c.endswith(\"PCT\") else 1):\n"),
        "shop/tax.py": lambda s: s.replace("return amount * RATES[state]",
                                           "return amount * RATES.get(state, Decimal(\"0\"))"),
    },
    "report": {
        "report/aggregate.py": lambda s: s.replace('team = row["team"]', 'team = row["team"].strip().lower()'),
        "report/fmt.py": lambda s: s.replace("sorted(totals.items())",
                                             "sorted(totals.items(), key=lambda kv: -kv[1])"),
    },
}


def run(task, fixed):
    spec = rb.TASKS[task]
    work = tempfile.mkdtemp(prefix="rb-oracle-")
    for rel, text in spec["files"].items():
        if fixed and rel in FIXES[task]:
            text = FIXES[task][rel](text)
        path = os.path.join(work, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "w", encoding="utf-8").write(text)
    open(os.path.join(work, spec["test_name"]), "w", encoding="utf-8").write(spec["tests"])
    r = subprocess.run([sys.executable, "-m", "unittest", "-q"], cwd=work, capture_output=True, text=True)
    return r.returncode, (r.stderr or "").strip().splitlines()[-1:]


for task in rb.TASKS:
    base, fixed = run(task, False), run(task, True)
    print(task, "baseline rc", base[0], base[1], "| fixed rc", fixed[0], fixed[1])
