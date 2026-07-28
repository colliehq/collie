#!/usr/bin/env bash
# One command to run every collie regression suite. Exit 0 = all green.
#   bash tests/run_all.sh
cd "$(dirname "$0")/.."
PY=.venv/bin/python
# fall back to whatever interpreter this OS actually ships. Use the BARE command name (not the
# `command -v` path — that resolves to spaces like "C:\Users\First Last\..." which split an unquoted
# $PY, and to the broken Windows-Store python3 stub) and VERIFY it's a real Python 3 before picking
# it — so the Store stub is skipped and `python` wins on Windows, `python3` on Linux/macOS.
if [ ! -x "$PY" ]; then
  PY=""
  for c in python3 python; do
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info[0]==3 else 1)' >/dev/null 2>&1; then
      PY=$c; break
    fi
  done
fi
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
if $PY tests/test_leash.py >/dev/null 2>&1; then echo "  leash (authority allow/ask/deny) OK"; else echo "  leash FAIL"; rc=1; fi
if $PY tests/test_capabilities.py >/dev/null 2>&1; then echo "  capabilities (note.append live e2e) OK"; else echo "  capabilities FAIL"; rc=1; fi
if $PY tests/test_scheduler.py >/dev/null 2>&1; then echo "  scheduler (durable wait/catch-up) OK"; else echo "  scheduler FAIL"; rc=1; fi
if $PY tests/test_gate_freshness.py >/dev/null 2>&1; then echo "  gate freshness (loop regression) OK"; else echo "  gate freshness FAIL"; rc=1; fi
if $PY tests/test_mandate.py >/dev/null 2>&1; then echo "  mandate (NL compiler) OK"; else echo "  mandate FAIL"; rc=1; fi
if $PY tests/test_research.py >/dev/null 2>&1; then echo "  research (web capability) OK"; else echo "  research FAIL"; rc=1; fi
if $PY tests/test_everyday.py >/dev/null 2>&1; then echo "  everyday (translate/summarize/reminder/note.list) OK"; else echo "  everyday FAIL"; rc=1; fi
if $PY tests/test_jobsweb.py >/dev/null 2>&1; then echo "  jobs web (dashboard + CSRF) OK"; else echo "  jobs web FAIL"; rc=1; fi
if $PY tests/test_cli_jobs.py >/dev/null 2>&1; then echo "  cli jobs (inbox/confirm/receipts) OK"; else echo "  cli jobs FAIL"; rc=1; fi
if $PY tests/test_plat.py >/dev/null 2>&1; then echo "  plat (OS layer: detect/kill_tree/rmtree/open_excl) OK"; else echo "  plat FAIL"; rc=1; fi
if $PY tests/test_mission.py >/dev/null 2>&1; then echo "  mission (multi-step campaign: plan/loop/gate/hand-off) OK"; else echo "  mission FAIL"; rc=1; fi
if $PY tests/test_missionweb.py >/dev/null 2>&1; then echo "  mission web (NL front-door service: start/confirm/resume) OK"; else echo "  mission web FAIL"; rc=1; fi
if $PY tests/test_primitives.py >/dev/null 2>&1; then echo "  primitives (real: research/compose/observe/web.submit+verify/web.send) OK"; else echo "  primitives FAIL"; rc=1; fi
if $PY tests/test_router.py >/dev/null 2>&1; then echo "  router (front-door classify: chat/code/mission + threshold/abstain/override) OK"; else echo "  router FAIL"; rc=1; fi
if COLLIE_SKIP_NET=1 $PY tests/test_update.py >/dev/null 2>&1; then echo "  update (version compare + refuses unsigned/tampered downloads) OK"; else echo "  update FAIL"; rc=1; fi
if $PY tests/test_platform_purity.py >/dev/null 2>&1; then echo "  platform purity (one codebase, three OSes: no unguarded Windows-only API) OK"; else echo "  platform purity FAIL"; rc=1; fi
if $PY tests/test_desktop.py >/dev/null 2>&1; then echo "  desktop (ambient widgets/music: clean/lrc/intent/config/pick/resolve caps) OK"; else echo "  desktop FAIL"; rc=1; fi
if $PY tests/test_desktopweb.py >/dev/null 2>&1; then echo "  desktop web (audio-proxy SSRF allow-list + relay CSRF-token gate) OK"; else echo "  desktop web FAIL"; rc=1; fi

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
if "$PY" -c "import playwright" >/dev/null 2>&1; then
  "$PY" tests/gui_test.py 2>&1 | grep -E "PASS|FAIL|GUI:" ; [ "${PIPESTATUS[0]}" = "0" ] || rc=1
else
  echo "  (playwright not found — skipping GUI suite)"
fi

echo "── remote E2E crypto (zero-knowledge relay) ─────────────"
if $PY -c "import cryptography" >/dev/null 2>&1; then
  if $PY tests/test_e2e.py >/dev/null 2>&1; then echo "  e2e OK"; else echo "  e2e FAIL"; rc=1; fi
else
  echo "  e2e SKIP (needs collie-harness[remote])"
fi

echo "── pair code (collie's own optical format) ──────────────"
if $PY tests/test_paircode.py >/dev/null 2>&1; then echo "  paircode OK"; else echo "  paircode FAIL"; rc=1; fi

echo "── QR encoder (fallback pairing code) ───────────────────"
if $PY tests/test_qr.py >/dev/null 2>&1; then echo "  qr OK"; else echo "  qr FAIL"; rc=1; fi

echo "── web --lan host guard (phone pairing) ─────────────────"
if $PY tests/test_web_lan.py >/dev/null 2>&1; then echo "  web --lan OK"; else echo "  web --lan FAIL"; rc=1; fi

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
