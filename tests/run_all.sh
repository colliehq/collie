#!/usr/bin/env bash
# One command to run every collie regression suite. Exit 0 = all green.
# Kept LF by .gitattributes: this entrypoint runs under Git Bash on Windows CI too.
#   bash tests/run_all.sh
cd "$(dirname "$0")/.."
# PowerShell's bare `bash` commonly resolves to WSL, although this Windows
# checkout and its CI contract require Git Bash. Fail with one actionable
# sentence instead of producing hundreds of path/runtime false negatives.
if [ -n "${WSL_INTEROP:-}" ] && case "$PWD" in /mnt/[a-zA-Z]/*) true;; *) false;; esac; then
  echo "ERROR: Windows checkout opened through WSL bash. Run with Git Bash: 'C:\Program Files\Git\bin\bash.exe' tests/run_all.sh" >&2
  exit 2
fi
# Resolve once to a real Python executable. A functioning Windows Store alias
# can broker children outside the caller's Job even when its version check passes.
PY="${COLLIE_TEST_PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in .venv/bin/python .venv/Scripts/python.exe python python3; do
    resolved=$("$candidate" -c 'import sys; assert sys.version_info[0] == 3; print(sys.executable)' 2>/dev/null) || continue
    PY=$(printf '%s' "$resolved" | tr -d '\r')
    break
  done
else
  resolved=$("$PY" -c 'import sys; assert sys.version_info[0] == 3; print(sys.executable)' 2>/dev/null) || {
    echo "ERROR: COLLIE_TEST_PYTHON must name a working Python 3 interpreter" >&2
    exit 2
  }
  PY=$(printf '%s' "$resolved" | tr -d '\r')
fi
if [ -z "$PY" ]; then
  echo "ERROR: Python 3 is required to run the regression suites" >&2
  exit 2
fi

# Every Python suite runs once, through pytest below: the standalone scripts (a main() and no
# test functions) are collected by tests/conftest.py and run in their own interpreter, and
# their verdict is the exit status. They used to be invoked one by one here as well, which
# ran 26 of them twice (pytest collected them too) and left `pytest tests/` without the
# other 33.

rc=0
NODE_OK=0
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR=$(node -p "Number(process.versions.node.split('.')[0])" 2>/dev/null || echo 0)
  if [ "$NODE_MAJOR" -ge 20 ] 2>/dev/null; then
    NODE_OK=1
  else
    echo "ERROR: Node >=20 is required for JavaScript regressions (found $(node --version 2>/dev/null || echo unknown))" >&2
    rc=1
  fi
else
  echo "ERROR: Node >=20 is required for JavaScript regressions" >&2
  rc=1
fi

echo "── py_compile (all modules) ─────────────────────────────"
if "$PY" -m py_compile harness/*.py; then echo "  OK"; else echo "  FAIL"; rc=1; fi

echo "── renderer tests (JS) ──────────────────────────────────"
if [ "$NODE_OK" = "1" ]; then
  node tests/render_test.js || rc=1
  node tests/mail_names_test.js || rc=1
  node tests/automation_budget_test.js || rc=1
else
  echo "  (node not found — skipping renderer suite)"
fi

echo "── browser extension: page-side logic (JS) ──────────────"
if [ "$NODE_OK" = "1" ]; then
  node tests/browser_ext_test.js || rc=1
  node tests/vscode_extension_test.js || rc=1
else
  echo "  (node not found — skipping browser + VS Code extension suites)"
fi

echo "── browser, LIVE (opt-in: COLLIE_BROWSER_LIVE=1 + extension) ─"
# The checks stubs cannot make — does CDP input reach a background tab, is a cross-origin iframe
# really readable, did the click land. Skips itself without a browser, so the suite stays hermetic.
live_out=$("$PY" tests/browser_live_test.py 2>&1); live_rc=$?
echo "$live_out" | grep -E "SKIP|FAIL|passed ==" | sed 's/^/  /'
[ "$live_rc" = "0" ] || rc=1

echo "── relay pairing handshake (JS) ─────────────────────────"
if [ "$NODE_OK" = "1" ]; then
  node tests/relay_pairing_test.js || rc=1
  node tests/relay_sealed_test.js || rc=1
  node tests/relay_presence_test.js || rc=1
else
  echo "  (node not found — skipping relay suite)"
fi

echo "── relay push + APNs bearer token (JS) ──────────────────"
if [ "$NODE_OK" = "1" ]; then
  node tests/relay_push_test.js || rc=1
  node tests/landing_security_test.mjs || rc=1
  node tests/slack_presence_worker_test.js || rc=1
else
  echo "  (node not found — skipping push + landing security suites)"
fi

echo "── GUI interactive components (Playwright, mock, \$0) ────"
if "$PY" -c "import playwright" >/dev/null 2>&1; then
  # Keep the output when it fails. Piping through grep and reporting only the exit status meant a
  # GUI suite that died before printing a single PASS line left NOTHING in the log — CI showed the
  # section header, the next header, and "SOME SUITES FAILED" with no reason anywhere. The filter is
  # for the happy path; a failure gets the whole thing.
  gui_out=$("$PY" tests/gui_test.py 2>&1); gui_rc=$?
  if [ "$gui_rc" = "0" ]; then
    echo "$gui_out" | grep -E "PASS|FAIL|GUI:"
  else
    echo "  GUI suite FAILED (exit $gui_rc) — full output follows:"
    echo "$gui_out" | tail -40 | sed "s/^/    /"
    rc=1
  fi
  # Suites that need a live server as well as a browser. browser_suite.py starts
  # a throwaway `collie web` for each, so they can never touch the user's real one.
  for t in steer_ui_check parallel_ui_check live_review_ui_check automation_editor_ui_check; do
    out=$("$PY" tests/browser_suite.py "$t" 2>&1); trc=$?
    if [ "$trc" = "0" ]; then echo "  $t OK"
    else echo "  $t FAIL"; echo "$out" | tail -14 | sed "s/^/    /"; rc=1; fi
  done
else
  echo "  (playwright not found — skipping GUI suite)"
fi

echo "── all collected pytest regressions ─────────────────────"
# Many files are written as bare `def test_*` with no __main__ block, so `"$PY" tests/x.py` imports
# them, runs nothing, and exits 0. Run the complete collected suite here—not a hand-maintained list
# that silently forgets each new runtime, Library, release, or security regression file.
if "$PY" -c "import pytest" >/dev/null 2>&1; then
  # Stream progress: collecting the complete suite into a shell variable made
  # a healthy multi-minute run indistinguishable from a hung process.
  if "$PY" -m pytest -q; then
    echo "  pytest suite OK"
  else
    pytest_rc=$?
    echo "  pytest suite FAIL (exit $pytest_rc)"
    rc=1
  fi
else
  # Not silently skipped: an unrunnable suite is a fact about this checkout, not a pass.
  echo "  gate suite NOT RUN — pytest is not installed (pip install pytest)"; rc=1
fi

echo "── CLI surfaces (run/dashboard/repl/tui/acp/bridge, mock) ─"
# Same rule as the GUI suite: the grep is for the happy path, and a failure gets everything. Piping
# straight into it meant a suite that died before its first PASS — an import error, a decode error
# in the harness itself — left the section header and nothing else.
surfaces_out=$("$PY" tests/surfaces_test.py 2>&1); surfaces_rc=$?
printf '%s\n' "$surfaces_out" | grep -E "PASS|FAIL|SKIP|SURFACES:"
if [ "$surfaces_rc" != "0" ]; then
  echo "  surfaces FAIL (exit $surfaces_rc) — tail of the full output:"
  printf '%s\n' "$surfaces_out" | tail -25 | sed 's/^/      /'
  rc=1
fi

echo "── selftest (mock provider, \$0 — informational) ─────────"
# NOTE: mock can't actually count files, so count_py fails by construction -> 2/3 is the
# expected baseline; this smoke is informational and does NOT gate the suite.
"$PY" -m harness.cli selftest 2>&1 | grep -E "tasks passed"

echo
[ $rc -eq 0 ] && echo "✅ ALL GATED SUITES GREEN (compile + core + renderer)" || echo "❌ SOME SUITES FAILED"
exit $rc
