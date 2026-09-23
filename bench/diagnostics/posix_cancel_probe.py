"""Temporary public-runner diagnostic, never part of the release or default CI.

Observe actual kernel answers for our own direct child's zombie, then repeat the
real nested cancellation test. Baseline and proposed cleanup run in separate jobs.
Neither an EPERM nor a successful SIGKILL is treated as proof a tree has stopped.
"""
import errno
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
BASE = 'e542a3dacf3ced84f8fc4a20286dc74676bf8f80'
variant = os.environ['DIAGNOSTIC_VARIANT']
assert variant in ('baseline', 'reap-exited-first')
print(json.dumps({'platform': platform.platform(), 'machine': platform.machine(),
                  'python': sys.version, 'variant': variant}), flush=True)
if variant == 'baseline':
    original = subprocess.check_output(['git', 'show', BASE+':harness/tool_process.py'], cwd=ROOT)
    (ROOT/'harness/tool_process.py').write_bytes(original)


def group_answer(pgid, sig):
    assert pgid > 1 and pgid != os.getpgrp()
    try:
        os.killpg(pgid, sig)
        return {'success': True}
    except OSError as exc:
        return {'success': False, 'errno': exc.errno, 'error': str(exc)}


observations = []
for index in range(12):
    # Bootstrap reads a release byte before doing the one controlled action.
    child = subprocess.Popen([sys.executable, '-I', '-c',
        'import sys; sys.stdin.buffer.read(1)'], stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    assert os.getpgid(child.pid) == child.pid
    child.stdin.write(b'G'); child.stdin.close()
    row = {'index': index, 'pid': child.pid, 'observed_zombie': False}
    try:
        end = time.monotonic()+4
        while time.monotonic() < end:
            # ps observes this one known child without waitpid/poll, which would reap it.
            state = subprocess.run(['ps', '-p', str(child.pid), '-o', 'stat='],
                                   capture_output=True, text=True, timeout=2).stdout.strip()
            if state.startswith('Z'):
                row['observed_zombie'] = True
                break
            time.sleep(.01)
        row['before_signal'] = group_answer(child.pid, 0)
        row['kill_unreaped'] = group_answer(child.pid, signal.SIGKILL)
        row['after_signal_before_reap'] = group_answer(child.pid, 0)
        child.wait(timeout=3)
        row['after_reap'] = group_answer(child.pid, 0)
    finally:
        if child.returncode is None:
            group_answer(child.pid, signal.SIGKILL)
            child.wait(timeout=3)
    observations.append(row)
    print('DIRECT_CHILD '+json.dumps(row), flush=True)

# Put the expanded assertion payload in BOTH variants: diagnostics must not be
# mistaken for a runtime change or withheld only from the baseline.
node = 'tests/test_interruption_lifecycle.py::test_cancel_nested_python_command_stops_descendants_and_resumes'
attempts = []
for index in range(30):
    started = time.monotonic()
    try:
        done = subprocess.run([sys.executable, '-m', 'pytest', '-q', '--no-header',
                               '-p', 'no:randomly', node], cwd=ROOT, capture_output=True,
                              text=True, timeout=35)
    except subprocess.TimeoutExpired:
        print('NESTED_TIMEOUT '+json.dumps({'index': index}), flush=True)
        # Stop the diagnostic instead of admitting another owned test after unknown cleanup.
        raise
    row = {'index': index, 'exit': done.returncode, 'seconds': round(time.monotonic()-started, 3)}
    attempts.append(row)
    print('NESTED '+json.dumps(row), flush=True)
    if done.returncode:
        print(done.stdout, flush=True)
        print(done.stderr, flush=True)

result = {'variant': variant, 'observations': observations, 'attempts': attempts,
          'passed': sum(r['exit'] == 0 for r in attempts),
          'failed': sum(r['exit'] != 0 for r in attempts)}
print('SUMMARY '+json.dumps(result), flush=True)
# A failed reproduction is data; keep the job red so it cannot masquerade as a gate.
raise SystemExit(1 if result['failed'] else 0)
