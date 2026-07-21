#!/usr/bin/env bash
# One command to run every collie regression suite. Exit 0 = all green.
#   bash tests/run_all.sh
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
rc=0

echo "── py_compile (all modules) ─────────────────────────────"
if $PY -m py_compile harness/*.py; then echo "  OK"; else echo "  FAIL"; rc=1; fi

echo "── core component tests (Python) ────────────────────────"
$PY tests/test_core.py 2>&1 | grep -vE "RequestsDependency|warnings.warn|WARN\(costs\)"
[ "${PIPESTATUS[0]}" = "0" ] || rc=1

echo "── verifier protocol (done-check equivalence) ───────────"
if $PY tests/test_verifier.py >/dev/null 2>&1; then echo "  verifier OK"; else echo "  verifier FAIL"; rc=1; fi
if $PY tests/test_observe.py >/dev/null 2>&1; then echo "  observe (real-socket e2e) OK"; else echo "  observe FAIL"; rc=1; fi
if $PY tests/test_actions.py >/dev/null 2>&1; then echo "  actions (confirm/executor/receipt) OK"; else echo "  actions FAIL"; rc=1; fi
if $PY tests/test_jobs.py >/dev/null 2>&1; then echo "  jobs (lifecycle/registry/executor) OK"; else echo "  jobs FAIL"; rc=1; fi

echo "── model catalog + codex provider (offline) ─────────────"
if $PY tests/test_catalog.py >/dev/null 2>&1; then echo "  catalog OK"; else echo "  catalog FAIL"; rc=1; fi
if $PY tests/test_codex_oauth.py >/dev/null 2>&1; then echo "  codex_oauth OK"; else echo "  codex_oauth FAIL"; rc=1; fi

echo "── renderer tests (JS) ──────────────────────────────────"
if command -v node >/dev/null 2>&1; then
  node tests/render_test.js || rc=1
else
  echo "  (node not found — skipping renderer suite)"
fi

echo "── GUI interactive components (Playwright, mock, \$0) ────"
if python3 -c "import playwright" >/dev/null 2>&1; then
  python3 tests/gui_test.py 2>&1 | grep -E "PASS|FAIL|GUI:" ; [ "${PIPESTATUS[0]}" = "0" ] || rc=1
else
  echo "  (playwright not found — skipping GUI suite)"
fi

echo "── CLI surfaces (run/dashboard/repl/tui/acp/bridge, mock) ─"
$PY tests/surfaces_test.py 2>&1 | grep -E "PASS|FAIL|SURFACES:"
[ "${PIPESTATUS[0]}" = "0" ] || rc=1

echo "── selftest (mock provider, \$0 — informational) ─────────"
# NOTE: mock can't actually count files, so count_py fails by construction -> 2/3 is the
# expected baseline; this smoke is informational and does NOT gate the suite.
$PY -m harness.cli selftest 2>&1 | grep -E "tasks passed"

echo
[ $rc -eq 0 ] && echo "✅ ALL GATED SUITES GREEN (compile + core + renderer)" || echo "❌ SOME SUITES FAILED"
exit $rc
