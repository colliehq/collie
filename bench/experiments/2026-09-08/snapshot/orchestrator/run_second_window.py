"""Launch a registered phase using the second subscription window only."""
import argparse
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

from design_jobs import environment
from quota import snapshot
from runtime_inventory import sdk_cli

ROOT = Path(__file__).resolve().parent
TASKS = 'ordered-pathrule-priority-engine,capacity-ledger-reservation-audit'


def gate(plan):
    now = dt.datetime.now(dt.timezone.utc)
    if now < dt.datetime.fromisoformat(plan['not_before_utc'].replace('Z', '+00:00')):
        raise SystemExit('The second window is not open; no model process started')
    quota = snapshot()
    window = quota.get('five_hour') or {}
    extra = quota.get('extra_usage') or {}
    if not quota.get('ok') or extra.get('is_enabled') is not False:
        raise SystemExit('No confirmed subscription-only quota receipt; no model process started')
    used = window.get('utilization')
    if type(used) not in (int, float) or not 0 <= used < 100:
        raise SystemExit('Subscription window exhausted or unknown; no model process started')
    latest = dt.datetime(2026, 9, 8, 11, 0, tzinfo=dt.timezone.utc)
    reset_text = window.get('resets_at')
    reset = dt.datetime.fromisoformat(reset_text.replace('Z', '+00:00')) if reset_text else None
    # A fresh zero-utilization window can lack a new expiry until its first
    # request. A past expiry is usable only with an explicit fresh zero count.
    unused = used == 0 and (reset is None or reset <= now)
    if now >= latest or (not unused and (reset is None or not now < reset <= latest)):
        raise SystemExit('Not the registered second window; no model process started')
    return quota


def source_check(folder, expected):
    head = subprocess.check_output(['git', '-C', str(folder), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(folder), 'status', '--porcelain'], text=True).strip()
    if head != expected or dirty:
        raise SystemExit('Registered source pin changed or is dirty: '+folder.name)


def smoke_passed(receipt):
    turns = receipt.get('turns') or []
    reservations = receipt.get('reservations') or []
    return (receipt.get('passed') is True and len(turns) == len(reservations) == 3
            and all(type(row.get('request_count')) is int and row['request_count'] == 1
                    and row.get('stop_reason') != 'error' for row in turns)
            and all(row.get('outcome') == 'completed' for row in reservations))


def launch(name, argv, cwd=ROOT):
    logs = ROOT/'second-window-launches'
    logs.mkdir(exist_ok=True)
    started = dt.datetime.now(dt.timezone.utc).isoformat()
    begin = time.monotonic()
    env = environment()
    # The normalized guard permits no inherited CLAUDE_CODE routing/configuration
    # variables. Its frozen SDK sidecar sets zero HTTP retries inside the worker.
    if name.startswith('normalized-'):
        env.pop('CLAUDE_CODE_MAX_RETRIES', None)
    else:
        env['CLAUDE_CODE_MAX_RETRIES'] = '0'
    with (logs/(name+'.log')).open('x', encoding='utf-8') as log:
        process = subprocess.Popen(argv, cwd=cwd,
            env=env,
            stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        print(json.dumps({'event': 'start', 'name': name, 'pid': process.pid, 'at': started}), flush=True)
        code = process.wait()
    result = {'name': name, 'exit': code, 'started_at': started,
              'seconds': round(time.monotonic()-begin, 3)}
    (logs/(name+'.json')).write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'event': 'end', **result}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('a', 'b'), required=True)
    parser.add_argument('--i-will-spend', action='store_true')
    args = parser.parse_args()
    if not args.i_will_spend:
        parser.error('Real model requests require --i-will-spend')
    plan = json.loads((ROOT/'second-window-plan.json').read_text(encoding='utf-8'))
    assert plan['schema_version'] == 2 and plan['planned_coding_attempts'] == 52
    product = ROOT/'product026-session2-pin'
    prototype = ROOT/'session2-transport-worktree'
    source_check(product, plan['cohorts'][0]['source_commit'])
    source_check(prototype, plan['cohorts'][1]['source_commit'])
    runtime = sdk_cli()
    version = subprocess.check_output([str(runtime), '--version'], text=True).strip()
    if not version.startswith(plan['runtime']['native_and_sdk_cli']+' '):
        raise SystemExit('Registered Claude runtime changed')
    quota = gate(plan)
    gate_path = ROOT/('second-window-phase-'+args.phase+'-quota.json')
    with gate_path.open('x', encoding='utf-8') as file:
        json.dump(quota, file, indent=2)

    def native(name, repo, arms='collie', concurrency=3, session=None, context='default'):
        command = [sys.executable, str(ROOT/'start_variant.py'), '--repo', str(repo),
                   '--tasks', TASKS, '--name', name, '--arms', arms, '--repetitions', '3',
                   '--concurrency', str(concurrency), '--native-runtime', str(runtime),
                   '--structured-mode', 'plain', '--context-mode', context]
        if session:
            command += ['--session-mode', session]
        return launch(name, command)

    smoke_path = ROOT/'session-transport-smoke-v2/smoke.json'
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        if args.phase == 'a':
            product_future = pool.submit(native, 'native-session2-product', product,
                                         'collie,claude-code', 6)
            smoke = launch('session-transport-smoke-v2', [sys.executable,
                str(prototype/'bench/session_transport/smoke_session_transport.py'),
                '--out', str(smoke_path.parent), '--turns', '3', '--model', plan['model'],
                '--effort', plan['effort'], '--i-will-spend'], prototype)
            passed = smoke['exit'] == 0 and smoke_path.exists() and smoke_passed(json.loads(
                smoke_path.read_text(encoding='utf-8')))
            futures = [product_future]
            if passed:
                futures.extend(pool.submit(native, 'native-session2-'+mode+'-default',
                    prototype, session=mode) for mode in ('session', 'stateless'))
            else:
                print(json.dumps({'event': 'dependent_cohort_skipped', 'reason': 'transport_smoke_failed'}), flush=True)
        else:
            phase_a = ROOT/'second-window-phase-a.json'
            if not phase_a.exists():
                raise SystemExit('Phase A must finish before phase B')
            futures = [pool.submit(launch, 'normalized-session2', [sys.executable,
                str(ROOT/'start_normalized.py'), '--repo', str(product), '--tasks', TASKS,
                '--name', 'normalized-session2', '--repetitions', '2', '--concurrency', '4'])]
            if smoke_path.exists() and smoke_passed(json.loads(smoke_path.read_text(encoding='utf-8'))):
                futures.extend(pool.submit(native, 'native-session2-'+mode+'-full-history',
                    prototype, session=mode, context='full-tool-history') for mode in ('session', 'stateless'))
        results = [future.result() for future in concurrent.futures.as_completed(futures)]
    (ROOT/('second-window-phase-'+args.phase+'.json')).write_text(
        json.dumps({'phase': args.phase, 'results': results,
                    'finished_at': dt.datetime.now(dt.timezone.utc).isoformat()}, indent=2)+'\n', encoding='utf-8')


if __name__ == '__main__':
    main()
