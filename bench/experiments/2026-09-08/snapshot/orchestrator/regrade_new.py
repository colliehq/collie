"""Rebuild every candidate from its frozen initial files; no model calls."""
import concurrent.futures
import hashlib
import importlib.util
import json
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FROZEN = Path('C:/workspace/collie/bench/experiments/2026-09-07')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    helpers = load('task_helpers', FROZEN/'adapter-source/bench/subscription_rank_tasks.py')
    evaluator = load('normalized_evaluator', FROZEN/'snapshot/normalized_batch.py')
    paths = sorted(ROOT.glob('native-*/native-replay/attempts/*/result.json'))
    paths += sorted(ROOT.glob('normalized-*/normalized-replay/results-*/runs/*/result.json'))

    def check(path):
        result = read(path)
        suite = path.relative_to(ROOT).parts[0]
        tasks = {t['task_id']: t for t in read(ROOT/suite/'tasks.json')}
        task_id = result.get('task') or result['task_id']
        task = tasks[task_id]
        patch = path.with_name('patch.diff')
        digest = hashlib.sha256(patch.read_bytes()).hexdigest()
        assert result['patch_sha256'] == digest, str(path)
        with tempfile.TemporaryDirectory(prefix='collie-independent-regrade-') as directory:
            workspace = Path(directory)/'work'
            helpers.materialize_task(task, workspace)
            subprocess.run(['git', 'init', '--quiet', str(workspace)], check=True, capture_output=True)
            if patch.stat().st_size:
                subprocess.run(['git', '-C', str(workspace), 'apply', '--binary', str(patch)],
                               check=True, capture_output=True)
            graded = evaluator.grade(task, workspace, digest, helpers)
        expected = result.get('grader', {}).get('passed', result.get('grader', {}).get('resolved'))
        if expected is not None:
            assert graded['resolved'] == expected, (suite, path.parent.name, graded)
        return {'suite': suite, 'job': path.parent.name, 'task': task_id,
                'patch_sha256': digest, 'artifact_correct': graded['resolved'],
                'original_grader_correct': expected, 'original_status': result['status'],
                'claim': 'Offline artifact check; does not change experiment eligibility.'}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(check, paths))
    output = {'model_calls': 0, 'candidates': len(results), 'rows': results}
    (ROOT/'candidate-regrade.json').write_text(json.dumps(output, indent=2), encoding='utf-8')
    print(json.dumps({'candidates': len(results), 'model_calls': 0,
                      'newly_graded': [r for r in results if r['original_grader_correct'] is None]}))


if __name__ == '__main__':
    main()
