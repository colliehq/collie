"""Exploratory paired experiment after observing repeated protocol failures."""
import concurrent.futures
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys

from run_second_window import gate, launch, source_check, TASKS
from runtime_inventory import sdk_cli

root = Path(__file__).resolve().parent
plan = {
    'registered_at': dt.datetime.now(dt.timezone.utc).isoformat(),
    'classification': 'Exploratory source repair following the original second-window cohorts.',
    'not_before_utc': '2026-09-08T05:30:05Z',
    'model': 'claude-opus-5', 'effort': 'high', 'response_mode': 'plain',
    'context_mode': 'full-tool-history', 'session_enabled': True,
    'tasks': TASKS.split(','), 'repetitions': 3, 'attempts': 12, 'concurrency_per_arm': 3,
    'baseline': '31d0b0c73e4f7e066915644de313686f9a28bdb0',
    'recovered': '310a3418647c652410b5bd88f5c0297600bfd1dc',
    'intervention': 'Restore the bounded format-repair allowance only after a valid model response. '
                    'The only production-code difference is harness/loop.py. All repairs remain budgeted.',
    'outcomes': ['independent patch correctness', 'clean completion', 'protocol failures',
                 'request budget stops', 'cache counters', 'wall time', 'session resets and cleanup'],
    'capacity_rule': 'Start after original full-history native cohorts complete; 6 coding workers '
                     'plus up to 4 normalized and 2 independent source reviews, global peak 12.',
}
if __name__ == '__main__':
    for name in ('native-session2-session-full-history', 'native-session2-stateless-full-history'):
        result = root/'second-window-launches'/(name+'.json')
        assert result.exists(), 'Original native cohort still running'
    gate(plan)
    with (root/'recovery-comparison-plan.json').open('x', encoding='utf-8') as file:
        json.dump(plan, file, indent=2)
    runtime = sdk_cli()
    def run(arm):
        repo = root/('session2-transport-worktree' if arm == 'baseline' else 'session2-recovery-prototype')
        source_check(repo, plan[arm])
        return launch('native-session2-recovery-'+arm, [sys.executable, str(root/'start_variant.py'),
            '--repo', str(repo), '--tasks', TASKS, '--name', 'native-session2-recovery-'+arm,
            '--arms', 'collie', '--repetitions', '3', '--concurrency', '3', '--structured-mode', 'plain',
            '--session-mode', 'session', '--context-mode', 'full-tool-history', '--native-runtime', str(runtime)])
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ('baseline', 'recovered')))
    (root/'recovery-comparison-launches.json').write_text(json.dumps(results, indent=2)+'\n', encoding='utf-8')
