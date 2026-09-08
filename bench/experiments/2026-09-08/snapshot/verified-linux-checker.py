"""Verify the exported benchmark bundle offline; optionally regrade every patch."""
import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import sys

sys.dont_write_bytecode = True


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--regrade-all', action='store_true')
    parser.add_argument('--source-repo', type=Path,
                        help='Optional Collie checkout containing the recorded base commit; verify source treatment trees.')
    args = parser.parse_args()
    root = args.bundle.resolve()
    manifest = read(root/'SHA256SUMS.json')
    present = {path.relative_to(root).as_posix() for path in root.rglob('*')
               if path.is_file() and '__pycache__' not in path.parts and path.name != 'SHA256SUMS.json'}
    assert present == set(manifest), 'files missing from or extra to the manifest'
    for name, expected in manifest.items():
        path = (root/name).resolve()
        assert path.is_relative_to(root), name
        assert digest(path) == expected, name

    results = read(root/'results.json')
    rows = results['native'] + results['normalized']
    validation = read(root/'validation.json')
    assert len(rows) == validation['candidate_hashes']
    identities = {(row['suite'], row['job']) for row in rows}
    assert len(identities) == len(rows), 'duplicate candidate identity'
    regraded = read(root/'candidate-regrade.json')
    assert regraded['candidates'] == len(rows)
    by_identity = {(row['suite'], row['job']): row for row in regraded['rows']}
    assert set(by_identity) == identities
    for row in rows:
        path = root/'candidates'/row['suite']/(row['job']+'.diff')
        assert path.resolve().is_relative_to(root)
        assert digest(path) == row['patch_sha256']
        previous = by_identity[(row['suite'], row['job'])]
        assert row['patch_sha256'] == previous['patch_sha256']
        correct = row['correct'] if 'correct' in row else row['artifact_correct']
        assert type(correct) is bool and correct == previous['artifact_correct']
    for row in results['normalized']:
        assert row['valid'] == (row['original_status'] in ('valid_resolved', 'valid_unresolved'))

    source_checked = 0
    if (root/'source-patches.json').exists():
        patches = read(root/'source-patches.json')
        experiments = read(root/'experiments.json')
        assert {entry['target_commit'] for entry in patches} == {
            experiment['source_commit'] for experiment in experiments.values()}
        for entry in patches:
            patch = (root/entry['patch']).resolve()
            assert patch.is_relative_to(root)
            assert digest(patch) == entry['patch_sha256']
            if args.source_repo:
                # Both the index and newly reconstructed objects live in the temporary
                # directory. The source object database can therefore be read-only.
                with tempfile.TemporaryDirectory(prefix='collie-source-check-') as directory:
                    git = ['git', '-C', str(args.source_repo.resolve())]
                    common = Path(subprocess.check_output(git+['rev-parse', '--git-common-dir'], text=True).strip())
                    if not common.is_absolute():
                        common = args.source_repo.resolve()/common
                    objects = Path(directory)/'objects'
                    objects.mkdir()
                    env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory)/'index'),
                               GIT_OBJECT_DIRECTORY=str(objects),
                               GIT_ALTERNATE_OBJECT_DIRECTORIES=str((common/'objects').resolve()))
                    subprocess.run(git+['read-tree', entry['base_commit']], env=env,
                                   check=True, capture_output=True)
                    if patch.stat().st_size:
                        try:
                            subprocess.run(git+['apply', '--cached', '--binary', str(patch)],
                                           env=env, check=True, capture_output=True)
                        except subprocess.CalledProcessError as exc:
                            raise RuntimeError('Source reconstruction failed for '+entry['target_commit']+
                                               ': '+exc.stderr.decode('utf-8', errors='replace')) from None
                    tree = subprocess.check_output(git+['write-tree'], env=env, text=True).strip()
                    assert tree == entry['target_tree'], entry['target_commit']
                    source_checked += 1

    helpers = load('frozen_tasks', root/'evaluator/frozen_tasks.py')
    evaluator = load('frozen_evaluator', root/'evaluator/frozen_evaluator.py')
    tasks = read(root/'tasks.json')
    indexed = {task['task_id']: task for task in tasks}
    assert len(indexed) == len(tasks)
    assert all(row['task'] in indexed for row in rows)

    with tempfile.TemporaryDirectory(prefix='collie-evidence-contracts-') as directory:
        temporary = Path(directory)
        for index, task in enumerate(tasks):
            for gold in (False, True):
                work = temporary/str(index)/('gold' if gold else 'baseline')
                helpers.materialize_task(task, work, gold=gold)
                graded = evaluator.grade(task, work, 'offline-contract-check', helpers)
                assert graded['resolved'] is gold, (task['task_id'], gold, graded)

    def regrade(row):
        with tempfile.TemporaryDirectory(prefix='collie-evidence-candidate-') as directory:
            work = Path(directory)/'work'
            task = indexed[row['task']]
            helpers.materialize_task(task, work)
            subprocess.run(['git', 'init', '--quiet', str(work)], check=True, capture_output=True)
            patch = root/'candidates'/row['suite']/(row['job']+'.diff')
            if patch.stat().st_size:
                subprocess.run(['git', '-C', str(work), 'apply', '--binary', str(patch)],
                               check=True, capture_output=True)
            graded = evaluator.grade(task, work, row['patch_sha256'], helpers)
            expected = row['correct'] if 'correct' in row else row['artifact_correct']
            assert graded['resolved'] is expected, (row['suite'], row['job'], graded)
        return True

    checked = 0
    if args.regrade_all:
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            checked = sum(pool.map(regrade, rows))
    print(json.dumps({'hashed_files': len(manifest), 'candidate_hashes': len(rows),
                      'grader_baseline_gold_checks': len(tasks)*2,
                      'candidates_regraded': checked, 'source_trees_reconstructed': source_checked,
                      'model_calls': 0}))


if __name__ == '__main__':
    main()
