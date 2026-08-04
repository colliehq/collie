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

echo "── browser extension: page-side logic (JS) ──────────────"
if command -v node >/dev/null 2>&1; then
  node tests/browser_ext_test.js || rc=1
else
  echo "  (node not found — skipping browser extension suite)"
fi

echo "── browser bridge tools (batching / spaces / warnings) ──"
if $PY tests/test_browserbridge.py >/dev/null 2>&1; then echo "  browserbridge OK"; else echo "  browserbridge FAIL"; rc=1; fi

echo "── browser, LIVE (opt-in: COLLIE_BROWSER_LIVE=1 + extension) ─"
# The checks stubs cannot make — does CDP input reach a background tab, is a cross-origin iframe
# really readable, did the click land. Skips itself without a browser, so the suite stays hermetic.
live_out=$($PY tests/browser_live_test.py 2>&1); live_rc=$?
echo "$live_out" | grep -E "SKIP|FAIL|passed ==" | sed 's/^/  /'
[ "$live_rc" = "0" ] || rc=1

echo "── relay pairing handshake (JS) ─────────────────────────"
if command -v node >/dev/null 2>&1; then
  node tests/relay_pairing_test.js || rc=1
else
  echo "  (node not found — skipping relay suite)"
fi

echo "── relay push + APNs bearer token (JS) ──────────────────"
if command -v node >/dev/null 2>&1; then
  node tests/relay_push_test.js || rc=1
else
  echo "  (node not found — skipping push suite)"
fi

echo "── phone notifications: when a run is worth a buzz ──────"
if $PY tests/test_notify.py >/dev/null 2>&1; then echo "  notify OK"; else echo "  notify FAIL"; rc=1; fi
if $PY tests/test_pairprompt.py >/dev/null 2>&1; then echo "  pairprompt OK"; else echo "  pairprompt FAIL"; rc=1; fi
if $PY tests/test_e2e_persist.py >/dev/null 2>&1; then echo "  e2e_persist OK"; else echo "  e2e_persist FAIL"; rc=1; fi
if $PY tests/test_playhere.py >/dev/null 2>&1; then echo "  playhere OK"; else echo "  playhere FAIL"; rc=1; fi
if app_out=$($PY tests/test_app_port.py 2>&1); then echo "  app_port OK"; else echo "  app_port FAIL"; echo "$app_out" | tail -20 | sed 's/^/      /'; rc=1; fi
if $PY tests/test_output_encoding.py >/dev/null 2>&1; then echo "  output_encoding OK"; else echo "  output_encoding FAIL"; rc=1; fi
if $PY tests/test_data_dir.py >/dev/null 2>&1; then echo "  data_dir OK"; else echo "  data_dir FAIL"; rc=1; fi
if $PY tests/test_relay_keepalive.py >/dev/null 2>&1; then echo "  relay_keepalive OK"; else echo "  relay_keepalive FAIL"; rc=1; fi
if $PY tests/test_repos_deadline.py >/dev/null 2>&1; then echo "  repos_deadline OK"; else echo "  repos_deadline FAIL"; rc=1; fi
if $PY tests/test_runs_registry.py >/dev/null 2>&1; then echo "  runs_registry OK"; else echo "  runs_registry FAIL"; rc=1; fi
if $PY tests/test_mirror_backlog.py >/dev/null 2>&1; then echo "  mirror_backlog OK"; else echo "  mirror_backlog FAIL"; rc=1; fi
if $PY tests/test_worktree.py >/dev/null 2>&1; then echo "  worktree OK"; else echo "  worktree FAIL"; rc=1; fi
if $PY tests/test_slackbot.py >/dev/null 2>&1; then echo "  slackbot OK"; else echo "  slackbot FAIL"; rc=1; fi
if $PY tests/test_packaging_facts.py >/dev/null 2>&1; then echo "  packaging OK"; else echo "  packaging FAIL"; rc=1; fi

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
  # Two suites that need a live server as well as a browser: the transcript's own honesty (a steer
  # shown where it happened) and that more than one thread can run at once. browser_suite.py starts
  # a throwaway `collie web` for each, so they can never touch the user's real one.
  for t in steer_ui_check parallel_ui_check; do
    out=$("$PY" tests/browser_suite.py "$t" 2>&1); trc=$?
    if [ "$trc" = "0" ]; then echo "  $t OK"
    else echo "  $t FAIL"; echo "$out" | tail -14 | sed "s/^/    /"; rc=1; fi
  done
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
