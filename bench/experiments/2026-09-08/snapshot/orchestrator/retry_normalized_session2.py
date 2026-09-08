"""Resume the unchanged prepared cohort after a pre-inference environment rejection."""
import json
from pathlib import Path
from run_second_window import launch

root = Path(__file__).resolve().parent
suite = root/'normalized-session2'
assert not list((suite/'normalized-replay').glob('results-*')), 'A coding run already exists'
record = {
    'initial_launch': 'second-window-launches/normalized-session2.log',
    'initial_failure': 'billing_or_routing_override_present',
    'model_requests_before_rejection': 0,
    'change': 'Remove the outer CLAUDE_CODE_MAX_RETRIES environment override only. '
              'The normalized guard is unchanged; the frozen SDK worker configures its own retries.',
    'tasks_source_grader_and_deadline_unchanged': True,
}
with (suite/'startup-retry.json').open('x', encoding='utf-8') as file:
    json.dump(record, file, indent=2)
plan = json.loads((suite/'replay-plan.json').read_text(encoding='utf-8'))
result = launch('normalized-session2-retry', plan['command'], suite)
raise SystemExit(result['exit'])
