"""Offline evidence verification. No model, account, browser, or network calls."""
import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--regrade-primary', action='store_true')
    args = parser.parse_args()
    manifest = read(ROOT/'SHA256SUMS.json')
    for name, digest in manifest.items():
        assert hashlib.sha256((ROOT/name).read_bytes()).hexdigest() == digest, name
    evidence = read(ROOT/'results.json')
    rows = evidence['native'] + evidence['normalized']
    assert len(rows) == 90
    assert len({(r['suite'], r['run']) for r in rows}) == 90
    primary_native = [r for r in evidence['native'] if r['suite'].startswith('native-complex-')]
    primary_normalized = [r for r in evidence['normalized'] if r['suite'] in (
        'normalized-initial', 'normalized-patch', 'normalized-cache-inbox-v2')]
    assert len(primary_native) == 24 and len(primary_normalized) == 36
    for arm, correct, clean in [('claude-code', 9, 12), ('collie', 10, 11)]:
        group = [r for r in primary_native if r['arm'] == arm]
        assert len(group) == 12
        assert sum(r['correct'] for r in group) == correct
        assert sum(r['clean_execution'] for r in group) == clean
    for arm in ('collie', 'hermes', 'pi', 'prime'):
        group = [r for r in primary_normalized if r['arm'] == arm]
        assert len(group) == 9 and sum(r['correct'] for r in group) == 7
        assert all(r['clean_execution'] for r in group)
    for r in rows:
        patch = ROOT/'candidates'/r['suite']/(r['run']+'.diff')
        assert hashlib.sha256(patch.read_bytes()).hexdigest() == r['patch_sha256']

    helpers = load('task_helpers', ROOT/'adapter-source/bench/subscription_rank_tasks.py')
    evaluator = load('normalized_evaluator', ROOT/'snapshot/normalized_batch.py')
    tasks = read(ROOT/'tasks.json') + read(ROOT/'tasks-coverage.json')
    indexed = {t['task_id']: t for t in tasks}
    assert len(indexed) == 7
    # Exercise the exact production marker grader, including authored main()
    # exit handling and added gold files, on both sides of each task contract.
    with tempfile.TemporaryDirectory(prefix='collie-benchmark-evidence-') as directory:
        work_root = Path(directory)
        for i, task in enumerate(tasks):
            for gold in (False, True):
                work = work_root/str(i)/('gold' if gold else 'baseline')
                helpers.materialize_task(task, work, gold=gold)
                graded = evaluator.grade(task, work, 'offline-preflight', helpers)
                assert graded['resolved'] == gold, (task['task_id'], gold, graded)

        regraded = 0
        if args.regrade_primary:
            for i, row in enumerate(primary_native + primary_normalized):
                task = indexed[row['task']]
                work = work_root/'candidates'/str(i)
                helpers.materialize_task(task, work)
                subprocess.run(['git', 'init', '--quiet', str(work)], check=True, capture_output=True)
                patch = ROOT/'candidates'/row['suite']/(row['run']+'.diff')
                if patch.stat().st_size:
                    subprocess.run(['git', '-C', str(work), 'apply', '--binary', str(patch)],
                                   check=True, capture_output=True)
                graded = evaluator.grade(task, work, row['patch_sha256'], helpers)
                assert graded['resolved'] == row['correct'], (row['suite'], row['run'], graded)
                regraded += 1
    print(json.dumps({'hashed_files': len(manifest), 'candidate_hashes': 90,
                      'grader_baseline_gold_checks': 14,
                      'primary_candidates_regraded': regraded, 'model_calls': 0}))


if __name__ == '__main__':
    main()
