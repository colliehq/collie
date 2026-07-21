"""Aider-Polyglot axis — multi-language coding, to break SWE-bench's Python monoculture
(the 2026 critique). Each Exercism exercise: give the agent the instructions + the solution
stub (test hidden), let it edit, then run the language's real test suite.

Reports, per language and overall:
  - pass_rate : tests green after the agent's edit
  - wellformed: the edited solution actually parses/compiles (Aider's "well-formed edits"
                metric — instruction-following reliability, independent of correctness)
  - efficiency: per-exercise wall-time (all agents) + tokens (collie only), mirroring the
                SWE efficiency table.

4-way agent comparison (pass --model claude-opus-4-8 to put the collie/cc/hermes runs on Opus):
  - collie        : the collie harness. DeepSeek by default; with --model <opus> it uses
                    provider 'anthropic-oauth' + that model. Same self-verify config PATH as
                    SWE (respects COLLIE_SWE_VERIFY / COLLIE_ASSERT_VERIFY / COLLIE_VERIFY_GATE).
  - collie_assert : identical, but forces COLLIE_ASSERT_VERIFY=1 (executable-assertion gate).
  - hermes        : the Hermes CLI (Opus via --provider anthropic -m <model> when --model set).
  - cc            : first-party Claude Code — `claude -p ... --permission-mode bypassPermissions`.

    # collie on DeepSeek (needs DEEPSEEK_API_KEY):
    DEEPSEEK_API_KEY=... python -m bench.polyglot_eval --langs python,cpp --n 3 --agent collie
    # Opus 4-way (collie/collie_assert on anthropic-oauth; cc/hermes on the CLI):
    python -m bench.polyglot_eval --langs python,cpp,javascript --n 6 \
        --agent collie_assert --model claude-opus-4-8

Toolchains needed: python3 (pytest), g++/cmake (cpp), node/npm (js). Missing ones are skipped.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLY = os.path.join(ROOT, "data", "polyglot")


def _cfg(ex_dir):
    c = json.load(open(os.path.join(ex_dir, ".meta", "config.json")))
    f = c.get("files", {})
    return f.get("solution", []), f.get("test", [])


def _instructions(ex_dir):
    p = os.path.join(ex_dir, ".docs", "instructions.md")
    txt = open(p, encoding="utf-8", errors="ignore").read() if os.path.exists(p) else ""
    ap = os.path.join(ex_dir, ".docs", "instructions.append.md")
    if os.path.exists(ap):
        txt += "\n" + open(ap, encoding="utf-8", errors="ignore").read()
    return txt[:4000]


# ---- language test runners: return (passed: bool, wellformed: bool) --------------------
def _test_python(wd, sol, test):
    wf = all(subprocess.run([sys.executable, "-m", "py_compile", os.path.join(wd, s)],
                            capture_output=True).returncode == 0 for s in sol)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q"] + test, cwd=wd,
                       capture_output=True, timeout=120)
    return (r.returncode == 0, wf)


def _test_cpp(wd, sol, test):
    wf = all(subprocess.run(["g++", "-std=c++17", "-fsyntax-only", os.path.join(wd, s)],
                            capture_output=True).returncode == 0 for s in sol if s.endswith((".cpp", ".cc")))
    build = os.path.join(wd, "build")
    for cmd in (["cmake", "-B", build, "-S", wd], ["cmake", "--build", build]):
        r = subprocess.run(cmd, cwd=wd, capture_output=True, timeout=240)
        if r.returncode != 0:
            return (False, wf)
    r = subprocess.run(["ctest", "--test-dir", build, "--output-on-failure"],
                       cwd=wd, capture_output=True, timeout=180)
    return (r.returncode == 0, wf)


def _test_js(wd, sol, test):
    wf = all(subprocess.run(["node", "--check", os.path.join(wd, s)],
                            capture_output=True).returncode == 0 for s in sol)
    subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=wd,
                   capture_output=True, timeout=300)
    r = subprocess.run(["npm", "test"], cwd=wd, capture_output=True, timeout=180)
    return (r.returncode == 0, wf)


LANGS = {
    "python": {"dir": "python", "test": _test_python, "need": [sys.executable]},
    "cpp":    {"dir": "cpp", "test": _test_cpp, "need": ["g++", "cmake"]},
    "javascript": {"dir": "javascript", "test": _test_js, "need": ["node", "npm"]},
}


def _have(bins):
    return all(shutil.which(b) or os.path.exists(b) for b in bins)


# ---- agent runners ---------------------------------------------------------------------
def _prompt(instr, sol):
    return ("Solve this coding exercise by editing ONLY the solution file(s): %s. Do NOT edit "
            "any test/spec file. Implement the full solution so the hidden tests pass; handle "
            "the edge cases the description implies.\n\nEXERCISE:\n%s" % (", ".join(sol), instr))


def _run_collie(wd, prompt, model=""):
    """Run the collie harness through the SAME config path as harness/swe.py::predict_collie —
    so the assert-verify variant is testable here. Returns {tokens, wall_ms} from the RunResult."""
    from harness.cli import make_harness
    from harness import swe                       # reuse the exact SWE verify/assert nudges
    # --model <opus> flips the backend to the subscription/OAuth path; default = DeepSeek.
    # COLLIE_PROVIDER / COLLIE_MODEL env still override (parity with predict_collie).
    provider = "anthropic-oauth" if (model or os.environ.get("COLLIE_MODEL")) else "deepseek"
    provider = os.environ.get("COLLIE_PROVIDER", provider)
    model = os.environ.get("COLLIE_MODEL", model) or None
    h = make_harness(wd, provider=provider, model=model, project="poly",
                     code_search=True, embed="hash")
    h.max_turns = 50                              # verify loop needs headroom (was 25)
    # ---- verify config, copied attribute-for-attribute from predict_collie ----------------
    if os.environ.get("COLLIE_SWE_VERIFY", "1") not in ("0", "false", "off"):
        h.self_verify = True
        h.verify_nudge = swe._SWE_VERIFY_NUDGE
        h.verify_gate = os.environ.get("COLLIE_VERIFY_GATE", "0") not in ("0", "false", "off")
        h.verify_max = int(os.environ.get("COLLIE_VERIFY_ROUNDS", "2"))
        # ASSERT-VERIFY (opt-in, COLLIE_ASSERT_VERIFY=1): gate finish on an executed
        # `assert actual == expected` + drive edit-iteration. Same knobs as SWE.
        if os.environ.get("COLLIE_ASSERT_VERIFY") in ("1", "true", "on"):
            h.verify_gate = True
            h.require_assert = True
            h.verify_nudge = swe._SWE_ASSERT_VERIFY_NUDGE
            h.repair_nudge = swe._SWE_ASSERT_REPAIR_NUDGE
            h.verify_max = int(os.environ.get("COLLIE_VERIFY_ROUNDS", "3"))
    else:
        h.self_verify = False
    h.force_edit = True                           # converge to an edit (no empty patches)
    res = h.run("poly", prompt, consolidate=False)
    h.memory.close(); h.recorder.close()
    return {"tokens": res.total_tokens, "wall_ms": res.wall_ms}


def _run_collie_assert(wd, prompt, model=""):
    """collie-assert = base collie with the executable-assertion verify gate forced on."""
    os.environ["COLLIE_ASSERT_VERIFY"] = "1"
    return _run_collie(wd, prompt, model)


def _run_hermes(wd, prompt, model=""):
    env = dict(os.environ); env.pop("OPENAI_API_KEY", None)
    cmd = ["hermes", "-z", prompt]
    # --model <opus> -> run Hermes on Opus via its anthropic provider (HERMES_* env overrides win).
    prov = os.environ.get("HERMES_PROVIDER") or ("anthropic" if model else None)
    m = os.environ.get("HERMES_MODEL") or model
    if prov:
        cmd += ["--provider", prov]
    if m:
        cmd += ["-m", m]
    subprocess.run(cmd, cwd=wd, env=env, capture_output=True, timeout=1800)
    return {}


def _run_cc(wd, prompt, model=""):
    """First-party Claude Code: `claude -p <prompt> --permission-mode bypassPermissions`."""
    cmd = ["claude", "-p", prompt, "--permission-mode", "bypassPermissions"]
    if model:
        cmd += ["--model", model]
    subprocess.run(cmd, cwd=wd, text=True, check=False, timeout=1800, capture_output=True)
    return {}


AGENTS = {
    "collie":        _run_collie,          # base (Opus via --model, else DeepSeek)
    "collie_assert": _run_collie_assert,   # + COLLIE_ASSERT_VERIFY
    "hermes":        _run_hermes,
    "cc":            _run_cc,               # `claude -p`
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--langs", default="python,cpp")
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--agent", default="collie", choices=sorted(AGENTS))
    ap.add_argument("--model", default="",
                    help="backend model. Empty = DeepSeek (collie) / default CLI model. "
                         "Set e.g. claude-opus-4-8 to run collie on provider anthropic-oauth "
                         "and cc/hermes on Opus.")
    args = ap.parse_args()
    run_agent = AGENTS[args.agent]
    # DeepSeek key is only needed for the collie DeepSeek path (no --model / COLLIE_MODEL).
    needs_deepseek = (args.agent in ("collie", "collie_assert")
                      and not args.model and not os.environ.get("COLLIE_MODEL"))
    if needs_deepseek and not os.environ.get("DEEPSEEK_API_KEY"):
        print("set DEEPSEEK_API_KEY (or pass --model <opus> for the anthropic-oauth path)"); return

    grand = {}
    for lang in args.langs.split(","):
        lang = lang.strip()
        L = LANGS.get(lang)
        if not L or not _have(L["need"]):
            print("skip %s (toolchain missing)" % lang); continue
        practice = os.path.join(POLY, L["dir"], "exercises", "practice")
        exs = sorted(os.listdir(practice))[: args.n]
        pas = wf = n = 0
        tok_sum = wall_sum = 0.0     # efficiency accumulators (tokens: collie only)
        for ex in exs:
            ex_dir = os.path.join(practice, ex)
            sol, test = _cfg(ex_dir)
            if not sol or not test:
                continue
            # workdir MUST be named after the exercise: the cpp CMakeLists derives the
            # target/source name from the directory name (get_filename_component ... NAME).
            wd = os.path.join(tempfile.mkdtemp(prefix="poly_"), ex)
            os.makedirs(wd)
            for item in os.listdir(ex_dir):
                s = os.path.join(ex_dir, item)
                d = os.path.join(wd, item)
                shutil.copytree(s, d) if os.path.isdir(s) else shutil.copy(s, d)
            t0 = time.time()
            stats, agent_s = {}, 0.0
            try:
                stats = run_agent(wd, _prompt(_instructions(ex_dir), sol), args.model) or {}
                agent_s = time.time() - t0          # WALL-TIME of the agent (before tests)
                passed, wellformed = L["test"](wd, sol, test)
            except Exception as e:
                agent_s = time.time() - t0
                passed, wellformed = False, False
                print("    %s ERROR %s" % (ex, str(e)[:60]))
            tok = int(stats.get("tokens", 0) or 0)
            pas += passed; wf += wellformed; n += 1
            tok_sum += tok; wall_sum += agent_s
            print("  [%s/%s] %-22s %s  wf=%s  (%.0fs%s)" % (
                lang, args.agent, ex, "PASS" if passed else "fail",
                "y" if wellformed else "n", agent_s,
                ", %d tok" % tok if tok else ""), flush=True)
            shutil.rmtree(os.path.dirname(wd), ignore_errors=True)
        grand[lang] = (pas, wf, n, tok_sum, wall_sum)
    print("\n=== Aider-Polyglot (%s%s) ===" % (
        args.agent, " / " + args.model if args.model else ""))
    tp = tw = tn = 0
    ttok = twall = 0.0
    for lang, (p, w, n, tk, wl) in grand.items():
        tp += p; tw += w; tn += n; ttok += tk; twall += wl
        print("  %-11s pass %d/%d (%.0f%%)  well-formed %d/%d  avg %.0fs/ex%s" % (
            lang, p, n, 100 * p / (n or 1), w, n, wl / (n or 1),
            "  %.0f tok/ex" % (tk / (n or 1)) if tk else ""))
    print("  %-11s pass %d/%d (%.0f%%)  well-formed %d/%d  avg %.0fs/ex%s" % (
        "OVERALL", tp, tn, 100 * tp / (tn or 1), tw, tn, twall / (tn or 1),
        "  %.0f tok/ex" % (ttok / (tn or 1)) if ttok else ""))


if __name__ == "__main__":
    main()
