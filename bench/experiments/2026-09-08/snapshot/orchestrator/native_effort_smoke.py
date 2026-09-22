"""Two real start/resume workflows through the product ClaudeCodeRunner."""
import concurrent.futures
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parent
REPO = Path('C:/workspace/collie')
sys.path.insert(0, str(REPO))
from harness.agent_runners import SubprocessRunner
from harness.claude_code_runner import ClaudeCodeRunner
from quota import snapshot
from runtime_inventory import describe

OUT = ROOT/'native-effort-real-smoke'
OUT.mkdir(exist_ok=False)
quota = snapshot()
(OUT/'quota.json').write_text(json.dumps(quota, indent=2))
assert quota.get('ok') and quota['extra_usage']['is_enabled'] is False
assert quota['five_hour']['utilization'] < 96
CLI = Path('C:/Users/Sining Xu/AppData/Local/Python/pythoncore-3.14-64/Lib/site-packages/claude_agent_sdk/_bundled/claude.exe')
runtime = describe(CLI)
runtime['source_commit'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, text=True).strip()
(OUT/'runtime.json').write_text(json.dumps(runtime, indent=2))


def run(case):
    folder = OUT/case
    folder.mkdir()
    work = folder/'work'
    work.mkdir()
    marker = 'violet-sparrow-17' if case == 'edit' else 'silver-heron-23'
    (work/'seed.txt').write_text(marker, encoding='utf-8')
    calls = []

    class Recorder(SubprocessRunner):
        def run(self, argv, **kwargs):
            calls.append({'argv':list(argv), 'cwd':kwargs['cwd']})
            return super().run(argv, **kwargs)

    runner = ClaudeCodeRunner(executable=str(CLI), model='claude-opus-5', effort='high',
                              process_runner=Recorder(), default_timeout_s=240)
    start = time.monotonic()
    first = runner.start(
        'Work only in this directory. Read seed.txt. Remember its exact marker for the next turn. '
        + ('Write note.txt containing the marker and newline. ' if case == 'edit' else '')
        + 'Reply with the marker. Do not access other files, network, credentials, or invoke other agents.', str(work))
    (folder/'first.json').write_text(json.dumps(first.to_dict(), indent=2), encoding='utf-8')
    assert first.settled and not first.error and not first.recovery_required, first.error
    second = runner.resume(first,
        'Continue the previous turn. Without rereading seed.txt, '
        + ('append exactly NEXT on a new line to note.txt, then ' if case == 'edit' else '')
        + 'reply with the marker you remember from the previous turn. Work only in this directory; no other agents or network.')
    (folder/'second.json').write_text(json.dumps(second.to_dict(), indent=2), encoding='utf-8')
    checks = {'first_settled':first.settled, 'second_settled':second.settled,
              'no_error':not first.error and not second.error,
              'no_recovery_required':not first.recovery_required and not second.recovery_required,
              'same_session':first.thread_id == second.thread_id,
              'remembered_marker':marker in second.final_output,
              'effort_on_both':len(calls)==2 and all(c['argv'][c['argv'].index('--effort')+1]=='high' for c in calls),
              'resumed_cli':len(calls)==2 and '--session-id' in calls[0]['argv'] and '--resume' in calls[1]['argv']}
    if case == 'edit':
        text = (work/'note.txt').read_text().strip().splitlines()
        checks['edits_preserved'] = text == [marker, 'NEXT']
    record = {'case':case, 'checks':checks, 'passed':all(checks.values()),
              'seconds':round(time.monotonic()-start,3), 'calls':calls,
              'usage':second.usage, 'invocations':second.invocation}
    (folder/'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    return record


with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    results = list(pool.map(run, ('read', 'edit')))
record = {'source_commit':runtime['source_commit'], 'passed':all(r['passed'] for r in results),
          'cases':results, 'claim':'Real native product start/resume smoke, not coding benchmark attempts.'}
(OUT/'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps(record))
raise SystemExit(0 if record['passed'] else 1)
