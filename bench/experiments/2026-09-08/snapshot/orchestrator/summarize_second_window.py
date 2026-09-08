"""Keep second-window cells, planned work and incomplete receipts distinct."""
import collections
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parent
CELLS = {
    'product-native': [('native-session2-product', 12)],
    'session-default': [('native-session2-session-default', 6), ('native-session2-stateless-default', 6)],
    'session-full-history': [('native-session2-session-full-history', 6), ('native-session2-stateless-full-history', 6)],
    'exploratory-recovery': [('native-session2-recovery-baseline', 6), ('native-session2-recovery-recovered', 6)],
    'exploratory-reminder': [('native-session2-reminder-stateless', 6),
                             ('native-session2-reminder-session', 6),
                             ('native-session2-reminder-reminded', 6)],
}


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def main():
    all_rows = read(ROOT/'evening-analysis.json')['rows']
    output = {'cohorts': {}, 'planned_coding_attempts': 52,
              'additional_exploratory_attempts': 30,
              'claim': 'Small, local, exploratory tasks. Keep source/context cells and native/adapted tracks separate.'}
    for cohort, suites in CELLS.items():
        cells = []
        for suite, planned in suites:
            rows = [row for row in all_rows if row['suite'] == suite]
            receipt_path = ROOT/suite/'native-replay/results.json'
            controller = read(receipt_path) if receipt_path.exists() else []
            represented = collections.Counter(row.get('status', 'unknown') for row in controller)
            groups = collections.defaultdict(list)
            for row in rows:
                groups[(row['task'], row['arm'])].append(row)
            metrics = []
            for (task, arm), group in sorted(groups.items()):
                metrics.append({'task': task, 'arm': arm, 'n': len(group),
                    'correct': sum(row['correct'] for row in group),
                    'clean': sum(row['clean'] for row in group),
                    'failures': dict(collections.Counter(row['failure'] for row in group if row['failure'])),
                    **{'median_'+key: statistics.median(row[key] for row in group)
                       for key in ('seconds', 'cache_write', 'cache_read', 'output')},
                    'session_evidence': [row['session_transport']['totals'] for row in group
                        if row.get('session_transport') and row['session_transport'].get('valid')]})
            cells.append({'suite': suite, 'planned': planned, 'candidate_receipts': len(rows),
                          'controller_receipts': len(controller), 'controller_statuses': dict(represented),
                          'not_yet_represented': max(0, planned-len(controller)), 'metrics': metrics})
        output['cohorts'][cohort] = cells
    normalized = [read(path) for path in ROOT.glob('normalized-session2/normalized-replay/results-*/runs/*/result.json')]
    output['normalized'] = {'planned': 16, 'receipts': len(normalized),
        'statuses': dict(collections.Counter(row['status'] for row in normalized)),
        'not_yet_represented': max(0, 16-len(normalized)),
        'arms': [{ 'arm': arm, 'n': len(group),
            'valid': sum(row['status'] in ('valid_resolved', 'valid_unresolved') for row in group),
            'reported_correct': sum(row.get('grader', {}).get('resolved') is True for row in group)}
            for arm in ('collie', 'pi', 'hermes', 'prime')
            if (group := [row for row in normalized if row['arm'] == arm])]}
    (ROOT/'second-window-summary.json').write_text(json.dumps(output, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(output))


if __name__ == '__main__':
    main()
