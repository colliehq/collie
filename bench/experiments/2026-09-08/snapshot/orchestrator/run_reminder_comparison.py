"""Exploratory three-cell test of persistent response-format reminders."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import sys

from run_second_window import gate, launch, source_check, TASKS
from runtime_inventory import sdk_cli

root = Path(__file__).resolve().parent

if __name__ == '__main__':
    assert (root/'recovery-comparison-launches.json').exists()
    assert (root/'second-window-launches/normalized-session2-retry.json').exists()
    plan = {
        'registered_at': dt.datetime.now(dt.timezone.utc).isoformat(),
        'not_before_utc': '2026-09-08T05:30:05Z',
        'classification': 'Exploratory follow-up after the original and recovery cohorts; do not pool versions.',
        'source_commit': '310a3418647c652410b5bd88f5c0297600bfd1dc',
        'model': 'claude-opus-5', 'effort': 'high', 'response_mode': 'plain',
        'context_mode': 'full-tool-history', 'repetitions': 3, 'tasks': TASKS.split(','),
        'arms': ['stateless', 'session', 'session-with-response-reminder'],
        'attempts': 18, 'concurrency_per_arm': 3, 'global_peak': 12,
        'hypothesis': 'The initial flat prompt repeats the response contract, while persistent deltas '
                      'carry only new observations. Restating the same contract after plain deltas may '
                      'reduce multiple-object replies, format repairs and session resets.',
        'intervention': 'Only add the fixed response-format reminder to existing plain persistent deltas. '
                        'First turns and reset prompts are unchanged; host histories and compaction remain authoritative.',
        'outcomes': ['independent artifact correctness', 'clean completion', 'protocol repairs and failures',
                     'physical requests', 'cache counters', 'session reuse and cleanup', 'wall time'],
        'claim_limit': 'The native conversation format differs from stateless flattening; not a cache-only treatment. '
                       'Small task set and concurrent source reviews do not establish a general latency ranking.',
    }
    gate(plan)
    repo = root/'session2-recovery-prototype'
    source_check(repo, plan['source_commit'])
    with (root/'reminder-comparison-plan.json').open('x', encoding='utf-8') as file:
        json.dump(plan, file, indent=2)
    runtime = sdk_cli()
    def run(arm):
        name = 'native-session2-reminder-'+arm
        command = [sys.executable, str(root/'start_variant.py'), '--repo', str(repo),
            '--tasks', TASKS, '--name', name, '--arms', 'collie', '--repetitions', '3',
            '--concurrency', '3', '--structured-mode', 'plain', '--context-mode', 'full-tool-history',
            '--session-mode', 'stateless' if arm == 'stateless' else 'session',
            '--native-runtime', str(runtime)]
        if arm == 'reminded':
            command.append('--response-reminder')
        return launch(name, command)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(run, ('stateless', 'session', 'reminded')))
    (root/'reminder-comparison-launches.json').write_text(json.dumps(results, indent=2)+'\n', encoding='utf-8')
