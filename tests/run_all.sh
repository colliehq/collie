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

# One standalone check, with its output kept for the case that needs it. `>/dev/null 2>&1` made
# every failure look identical and unactionable: a UnicodeEncodeError that killed tests/test_e2e.py
# on its own check name left "e2e FAIL" in the Windows CI log and nothing else — no traceback, no
# failing label, no exit code, nothing to distinguish a broken relay from a console that cannot
# print an arrow. A green run stays as quiet as it was; a failure prints the tail of what the
# script actually said, indented under its line.
#   check_script <label> <command...>       (returns the script's own exit status)
check_script() {
  local label="$1"; shift
  local out status
  out=$("$@" 2>&1); status=$?
  if [ "$status" = "0" ]; then
    echo "  $label OK"
  else
    echo "  $label FAIL (exit $status)"
    printf '%s\n' "$out" | tail -25 | sed 's/^/      /'
  fi
  return $status
}

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

echo "── core component tests (Python) ────────────────────────"
"$PY" tests/test_core.py 2>&1 | grep -vE "RequestsDependency|warnings.warn|WARN\(costs\)"
[ "${PIPESTATUS[0]}" = "0" ] || rc=1

echo "── verifier protocol (done-check equivalence) ───────────"
check_script "verifier" "$PY" tests/test_verifier.py || rc=1
check_script "observe (real-socket e2e)" "$PY" tests/test_observe.py || rc=1
check_script "actions (confirm/executor/receipt)" "$PY" tests/test_actions.py || rc=1
check_script "jobs (lifecycle/registry/executor)" "$PY" tests/test_jobs.py || rc=1
check_script "leash (authority allow/ask/deny)" "$PY" tests/test_leash.py || rc=1
check_script "capabilities (note.append live e2e)" "$PY" tests/test_capabilities.py || rc=1
check_script "scheduler (durable wait/catch-up)" "$PY" tests/test_scheduler.py || rc=1
check_script "gate freshness (loop regression)" "$PY" tests/test_gate_freshness.py || rc=1
check_script "mandate (NL compiler)" "$PY" tests/test_mandate.py || rc=1
check_script "research (web capability)" "$PY" tests/test_research.py || rc=1
check_script "everyday (translate/summarize/reminder/note.list)" "$PY" tests/test_everyday.py || rc=1
check_script "jobs web (dashboard + CSRF)" "$PY" tests/test_jobsweb.py || rc=1
check_script "cli jobs (inbox/confirm/receipts)" "$PY" tests/test_cli_jobs.py || rc=1
check_script "plat (OS layer: detect/kill_tree/rmtree/open_excl)" "$PY" tests/test_plat.py || rc=1
check_script "mission (multi-step campaign: plan/loop/gate/hand-off)" "$PY" tests/test_mission.py || rc=1
check_script "mission web (NL front-door service: start/confirm/resume)" "$PY" tests/test_missionweb.py || rc=1
check_script "primitives (real: research/compose/observe/web.submit+verify/web.send)" "$PY" tests/test_primitives.py || rc=1
check_script "router (front-door classify: chat/code/mission + threshold/abstain/override)" "$PY" tests/test_router.py || rc=1
check_script "update (version compare + refuses unsigned/tampered downloads)" env COLLIE_SKIP_NET=1 "$PY" tests/test_update.py || rc=1
check_script "platform purity (one codebase, three OSes: no unguarded Windows-only API)" "$PY" tests/test_platform_purity.py || rc=1
check_script "desktop (ambient widgets/music: clean/lrc/intent/config/pick/resolve caps)" "$PY" tests/test_desktop.py || rc=1
check_script "desktop web (audio-proxy SSRF allow-list + relay CSRF-token gate)" "$PY" tests/test_desktopweb.py || rc=1

echo "── model catalog + codex provider (offline) ─────────────"
if catalog_out=$("$PY" tests/test_catalog.py 2>&1); then echo "  catalog OK"; else echo "  catalog FAIL"; echo "$catalog_out" | tail -30 | sed 's/^/      /'; rc=1; fi
check_script "codex_oauth" "$PY" tests/test_codex_oauth.py || rc=1

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

echo "── browser bridge tools (batching / spaces / warnings) ──"
check_script "browserbridge" "$PY" tests/test_browserbridge.py || rc=1

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

echo "── phone notifications: when a run is worth a buzz ──────"
check_script "notify" "$PY" tests/test_notify.py || rc=1
check_script "pairprompt" "$PY" tests/test_pairprompt.py || rc=1
check_script "e2e_persist" "$PY" tests/test_e2e_persist.py || rc=1
check_script "playhere" "$PY" tests/test_playhere.py || rc=1
if app_out=$("$PY" tests/test_app_port.py 2>&1); then echo "  app_port OK"; else echo "  app_port FAIL"; echo "$app_out" | tail -20 | sed 's/^/      /'; rc=1; fi
check_script "output_encoding" "$PY" tests/test_output_encoding.py || rc=1
check_script "data_dir" "$PY" tests/test_data_dir.py || rc=1
check_script "model_pin" "$PY" tests/test_model_pin.py || rc=1
check_script "no_console_flash" "$PY" tests/test_no_console_flash.py || rc=1
check_script "settings_fallback" "$PY" tests/test_settings_fallback.py || rc=1
check_script "relay_keepalive" "$PY" tests/test_relay_keepalive.py || rc=1
check_script "remote_protocol_v2" "$PY" -m pytest -q tests/test_remote_protocol_v2.py || rc=1
check_script "repos_deadline" "$PY" tests/test_repos_deadline.py || rc=1
check_script "runs_registry" "$PY" tests/test_runs_registry.py || rc=1
check_script "mirror_backlog" "$PY" tests/test_mirror_backlog.py || rc=1
check_script "worktree" "$PY" tests/test_worktree.py || rc=1
check_script "mcp_catalog" "$PY" tests/test_mcp_catalog.py || rc=1
check_script "mcp_confidential" "$PY" tests/test_mcp_confidential.py || rc=1
check_script "slackbot" "$PY" tests/test_slackbot.py || rc=1
check_script "slack guard (parent/process-tree ownership)" "$PY" tests/test_slack_guard.py || rc=1
check_script "slack setup (one app per dog)" "$PY" tests/test_slack_setup.py || rc=1
check_script "dog mail (sealed to the dog, replay-proof)" "$PY" tests/test_dogmail.py || rc=1
check_script "dog mail wire (python ↔ worker agree on the bytes)" "$PY" tests/test_dogmail_wire.py || rc=1
check_script "packaging" "$PY" tests/test_packaging_facts.py || rc=1

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

echo "── remote E2E crypto (zero-knowledge relay) ─────────────"
if "$PY" -c "import cryptography" >/dev/null 2>&1; then
  check_script "e2e" "$PY" tests/test_e2e.py || rc=1
else
  echo "  e2e SKIP (needs collie-harness[remote])"
fi

echo "── pair code (collie's own optical format) ──────────────"
check_script "paircode" "$PY" tests/test_paircode.py || rc=1

echo "── QR encoder (fallback pairing code) ───────────────────"
check_script "qr" "$PY" tests/test_qr.py || rc=1

echo "── web --lan host guard (phone pairing) ─────────────────"
check_script "web --lan" "$PY" tests/test_web_lan.py || rc=1

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

echo "── what collie slack does with an ask ───────────────────"
check_script "slack worker" "$PY" tests/test_slack_worker.py || rc=1
check_script "whoami (which dog is this)" "$PY" tests/test_whoami.py || rc=1
check_script "slack answer (executed)" "$PY" tests/test_slack_answer.py || rc=1

echo "── a face per dog (deterministic logo variants) ─────────"
check_script "avatar" "$PY" tests/test_avatar.py || rc=1

echo "── which directories are a user's projects (star-map) ───"
check_script "repo discovery" "$PY" tests/test_repo_discovery.py || rc=1

echo "── what the star-map shows when you just open it ────────"
check_script "map landing" "$PY" tests/test_map_landing.py || rc=1

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
