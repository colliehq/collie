"""Create an immutable, allowlisted evidence stage for review before committing."""
import argparse
import ast
import datetime
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sessions-completed', type=int, choices=(1, 2), required=True)
    parser.add_argument('--second-session-status', choices=('pending', 'in_progress', 'complete'),
                        default='pending')
    args = parser.parse_args()
    if args.second_session_status == 'complete':
        assert args.sessions_completed == 2
        regression = read(ROOT/'product026-regression-final-v2/result.json')
        wheel = read(ROOT/'package026-final-v2-verification/result.json')
        gui = read(ROOT/'package026-final-v2-gui/result.json')
        workflow = read(ROOT/'workflow-independent-validation-final/result.json')
        assert regression['exit'] == 0 and wheel['passed'] and gui['exit'] == 0
        assert regression['source_commit'] == wheel['source_commit'] == gui['source_commit']
        assert all(check['passed'] for check in gui['checks']) and workflow['exit'] == 0
        assert read(ROOT/'long-chain-real-smoke/checked.json')['passed']
        assert read(ROOT/'workflow-layout-final-v3/result.json')['passed']
        assert read(ROOT/'workflow-image-only-green/result.json')['exit'] == 0
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=False)

    def write(name, value):
        path = destination/name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

    def copy(source, name):
        path = destination/name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, path)

    analysis = read(ROOT/'evening-analysis.json')
    native = [{key: value for key, value in row.items() if key != 'detail'}
              for row in analysis['rows']]
    regraded = read(ROOT/'candidate-regrade.json')
    artifact = {(row['suite'], row['job']): row for row in regraded['rows']}
    normalized = []
    suites = set(row['suite'] for row in native)
    for path in sorted(ROOT.glob('normalized-*/normalized-replay/results-*/runs/*/result.json')):
        result = read(path)
        suite, job = path.relative_to(ROOT).parts[0], path.parent.name
        suites.add(suite)
        normalized.append({
            'suite': suite, 'job': job, 'task': result['task_id'], 'arm': result['arm'],
            'repetition': result['repetition'], 'original_status': result['status'],
            'valid': result['status'] in ('valid_resolved', 'valid_unresolved'),
            'artifact_correct': artifact[(suite, job)]['artifact_correct'],
            'original_grader_correct': result.get('grader', {}).get('resolved'),
            'clean_execution': result.get('worker_outcome') == 'candidate' and result.get('agent_exit_code') == 0,
            'error_code': result.get('error_code'), 'worker_error_code': result.get('worker_error_code'),
            'seconds': result['duration_ms']/1000, 'usage': result.get('usage'),
            'physical_requests': result.get('sidecar_request_evidence', {}).get('physical_requests'),
            'patch_sha256': result['patch_sha256'], 'result_sha256': digest(path)})
        copy(path.with_name('patch.diff'), f'candidates/{suite}/{job}.diff')
        receipts = path.parent/'raw/request-receipts.json'
        if receipts.exists():
            copy(receipts, f'receipts/{suite}/{job}.json')
    for row in native:
        source = ROOT/row['suite']/'native-replay/attempts'/row['job']/'patch.diff'
        assert digest(source) == row['patch_sha256']
        copy(source, f'candidates/{row["suite"]}/{row["job"]}.diff')

    tasks = {}
    experiments = {}
    for suite in sorted(suites):
        experiments[suite] = read(ROOT/suite/'experiment.json')
        for task in read(ROOT/suite/'tasks.json'):
            if task['task_id'] in tasks:
                assert tasks[task['task_id']] == task
            tasks[task['task_id']] = task
        for name in ('native_attempt.py', 'native_batch.py', 'native_jobs.py', 'normalized_batch.py',
                     'normalized_ledger_capture.py', 'run_normalized.py', 'replay-plan.json',
                     'startup-retry.json'):
            source = ROOT/suite/name
            if source.exists():
                copy(source, f'snapshot/{suite}/{name}')
    write('tasks.json', list(tasks.values()))
    write('experiments.json', experiments)
    reconciliation = []
    for suite, experiment in sorted(experiments.items()):
        if 'tasks' in experiment:
            task_ids, arms, repetitions = experiment['tasks'], experiment['arms'], experiment['repetitions']
            basis = 'experiment.json'
        else:
            # The two initial native manifests predate the uniform schema. Recover
            # their declared grid from saved inputs and the snapshotted launcher,
            # never from the observed result counts or by rewriting old manifests.
            assert suite in ('native-pagination', 'native-eventview')
            task_ids = [task['task_id'] for task in read(ROOT/suite/'tasks.json')]
            command = read(ROOT/suite/'replay-plan.json')['command']
            repetitions = int(command[command.index('--repetitions')+1])
            tree = ast.parse((ROOT/suite/'native_batch.py').read_text(encoding='utf-8'))
            declared = [ast.literal_eval(node.iter) for node in ast.walk(tree)
                        if isinstance(node, ast.comprehension) and isinstance(node.target, ast.Name)
                        and node.target.id == 'arm']
            assert len(declared) == 1
            arms = list(declared[0])
            basis = 'tasks.json + replay-plan.json command + snapshotted native_batch.py arm grid'
        planned = len(task_ids)*len(arms)*repetitions
        observed = [row for row in native+normalized if row['suite'] == suite]
        start = 1 if suite.startswith('normalized-') else 0
        expected = {(task, arm, rep) for task in task_ids for arm in arms for rep in range(start, start+repetitions)}
        actual = {(row['task'], row['arm'], row['rep'] if 'rep' in row else row['repetition']) for row in observed}
        assert actual == expected, (suite, expected-actual, actual-expected)
        received = len(observed)
        reconciliation.append({'suite':suite, 'planned':planned, 'received':received,
                               'missing':max(0,planned-received), 'extra':max(0,received-planned),
                               'tasks':task_ids, 'arms':arms, 'repetitions':repetitions,
                               'repetition_start':start, 'basis':basis, 'exact_grid_matches':True})
        assert planned == received, suite
    write('planned-received.json', {'suites':reconciliation,
        'scope':'Represented coding suites, including every first-window suite. '
                'Unused prepare-only configurations and source reviews are not completed attempts.'})
    # Some controlled treatments live on isolated branches, not in the product
    # ancestry. Preserve reconstructible full-tree diffs from a public source pin.
    repo = Path('C:/workspace/collie')
    base = '5eae2cff216ac551b4261c5c3aa2c942eb381417'
    source_patches = []
    for commit in sorted({experiment['source_commit'] for experiment in experiments.values()}):
        patch = subprocess.check_output(['git', 'diff', '--binary', '--full-index', base, commit], cwd=repo)
        name = 'source-patches/'+commit+'.diff'
        target = destination/name
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(patch)
        source_patches.append({'base_commit':base, 'target_commit':commit,
            'target_tree':subprocess.check_output(['git', 'rev-parse', commit+'^{tree}'], cwd=repo, text=True).strip(),
            'patch':name, 'patch_sha256':digest(target)})
    write('source-patches.json', source_patches)
    write('results.json', {'native': native, 'native_summary': analysis['summary'],
                          'normalized': normalized,
                          'claim': 'Exploratory local tasks. Native and adapted tracks remain separate. '
                                   'Protocol and budget failures remain product outcomes. '
                                   'Native original invalid_infrastructure labels remain included as harness outcomes; '
                                   'normalized validity separately requires its request ledger. '
                                   'Offline artifact correctness never changes eligibility.'})
    write('candidate-regrade.json', regraded)
    write('cache-dialogue-summary.json', read(ROOT/'cache-dialogue-summary.json'))
    for path in sorted((ROOT/'cache-real-dialogue').glob('*/result.json')):
        row = read(path)
        allowed = {'mode', 'rep', 'steps', 'completed', 'seconds', 'turns', 'claim'}
        assert set(row) <= allowed
        write('cache-dialogue/'+path.parent.name+'.json', row)
    copy(ROOT/'quota.jsonl', 'quota.jsonl')
    copy(ROOT/'task-admission.json', 'task-admission.json')
    for source, name in (
        ('first-window-runtime-audit.json', 'runtime-audit/first-window.json'),
        ('session-runtime-windows.json', 'runtime-audit/windows-sdk.json'),
        ('session-runtime-linux/result.json', 'runtime-audit/linux-sdk.json'),
        ('normalized-runtime-postrun.json', 'runtime-audit/normalized-postrun.json'),
        ('real-container-verification-v2/result.json', 'product-validation/container-verification.json'),
        ('product026-regression/result.json', 'product-validation/full-regression-initial.json'),
        ('product026-regression-v2/result.json', 'product-validation/full-regression-v2.json'),
        ('product026-regression-v3/result.json', 'product-validation/full-regression-v3.json'),
        ('product026-regression-v3/run-all.log', 'product-validation/full-regression-v3.log'),
        ('effort-independent-validation/result.json', 'product-validation/effort-regression.json'),
        ('effort-independent-validation/pytest.log', 'product-validation/effort-regression.log'),
        ('storage-independent-validation/result.json', 'product-validation/storage-regression.json'),
        ('storage-independent-validation/pytest.log', 'product-validation/storage-regression.log'),
        ('storage-profile-final.json', 'product-validation/storage-profile.json'),
        ('storage-reviewed.md', 'product-validation/storage-review.md'),
        ('storage-linux-validation-utf8.log', 'product-validation/storage-linux.log'),
        ('storage-linux-validation.json', 'product-validation/storage-linux.json'),
        ('workflow-reviewed.md', 'product-validation/workflow-review.md'),
        ('final-ui-audit-reviewed.md', 'product-validation/workflow-final-audit.md'),
        ('final-ui-audit/result.json', 'product-validation/workflow-final-audit-result.json'),
        ('final-ui-audit/runtime.json', 'runtime-audit/workflow-final-audit.json'),
        ('evidence-reviewed.md', 'evidence-review.md'),
        ('evidence-linux-diagnostic/result.json', 'evidence-linux-before.json'),
        ('evidence-linux-diagnostic/verify.log', 'evidence-linux-before.log'),
        ('evidence-linux-validation-v2/result.json', 'evidence-linux.json'),
        ('evidence-linux-validation-v2/verify.log', 'evidence-linux.log'),
        ('verified-linux-checker.py', 'snapshot/verified-linux-checker.py'),
        ('product026-regression-final/result.json', 'product-validation/full-regression-integration-before.json'),
        ('product026-regression-final/run-all.log', 'product-validation/full-regression-integration-before.log'),
        ('product026-regression-final-v2/result.json', 'product-validation/full-regression-final.json'),
        ('product026-regression-final-v2/run-all.log', 'product-validation/full-regression-final.log'),
        ('package026-final-v2-verification/result.json', 'product-validation/wheel-final.json'),
        ('package026-final-v2/build.json', 'product-validation/wheel-build.json'),
        ('package026-final-v2/gui.log', 'product-validation/wheel-gui-final.log'),
        ('package026-final-v2-gui/result.json', 'product-validation/wheel-gui-final.json'),
        ('workflow-independent-validation-final/result.json', 'product-validation/workflow-regression.json'),
        ('workflow-independent-validation-final/pytest.log', 'product-validation/workflow-regression.log'),
        ('workflow-queue-boundary-red/result.json', 'product-validation/queue-boundary-before.json'),
        ('workflow-queue-boundary-red/pytest.log', 'product-validation/queue-boundary-before.log'),
        ('workflow-image-only-red/result.json', 'product-validation/image-only-before.json'),
        ('workflow-image-only-red/pytest.log', 'product-validation/image-only-before.log'),
        ('workflow-image-only-green/result.json', 'product-validation/image-only-after.json'),
        ('workflow-image-only-green/pytest.log', 'product-validation/image-only-after.log'),
        ('workflow-legacy-contract-validation/result.json', 'product-validation/ui-site-contracts.json'),
        ('workflow-legacy-contract-validation/pytest.log', 'product-validation/ui-site-contracts.log'),
        ('workflow-layout-final-v3/result.json', 'product-validation/workflow-layout.json'),
        ('workflow-layout-final-v3/retained-1280-en.png', 'product-validation/retained-1280-en.png'),
        ('workflow-layout-final-v3/retained-390-zh.png', 'product-validation/retained-390-zh.png'),
        ('long-chain-real-smoke/plan.json', 'product-validation/long-chain/plan.json'),
        ('long-chain-real-smoke/result.json', 'product-validation/long-chain/initial.json'),
        ('long-chain-real-smoke/collie-retry/retry.json', 'product-validation/long-chain/retry.json'),
        ('long-chain-real-smoke/collie-retry/result.json', 'product-validation/long-chain/collie-retry.json'),
        ('long-chain-real-smoke/collie-retry/requests.jsonl', 'product-validation/long-chain/requests.jsonl'),
        ('long-chain-real-smoke/checked.json', 'product-validation/long-chain/checked.json'),
        ('long-chain-real-smoke/fixture.json', 'product-validation/long-chain/fixture.json'),
        ('long-chain-real-smoke/prompt.txt', 'product-validation/long-chain/prompt.txt'),
        ('package026-presecond-verification-v2/result.json', 'product-validation/wheel-pre-pack-fix.json'),
        ('package026-presecond-gui/result.json', 'product-validation/wheel-gui-initial.json'),
        ('package026-pack-fix-verification/result.json', 'product-validation/wheel-pack-fix.json'),
        ('package026-pack-fix-gui/result.json', 'product-validation/wheel-gui-pack-fix.json'),
        ('second-window-plan.json', 'second-window-plan.json'),
        ('second-window-plan-v1.json', 'second-window-plan-v1.json'),
        ('second-window-summary.json', 'second-window-summary.json'),
        ('recovery-comparison-plan.json', 'recovery-comparison-plan.json'),
        ('reminder-comparison-plan.json', 'reminder-comparison-plan.json'),
        ('native-effort-real-smoke/result.json', 'product-validation/native-effort-start-resume.json'),
        ('native-effort-real-smoke/runtime.json', 'runtime-audit/native-effort-start-resume.json'),
        ('session-transport-smoke-v2/smoke.json', 'session-transport-smoke.json'),
        ('second-window-phase-a.json', 'launches/phase-a.json'),
        ('second-window-phase-b.json', 'launches/phase-b.json'),
        ('second-window-launches/normalized-session2.json', 'launches/normalized-initial.json'),
        ('second-window-launches/normalized-session2-retry.json', 'launches/normalized-retry.json'),
        ('session-projection-diagnostic/result.json', 'session-projection-diagnostic.json'),
        ('quota-wait-ui-review-v2/result.json', 'product-validation/quota-wait-ui.json'),
    ):
        if (ROOT/source).exists():
            copy(ROOT/source, name)
    copy(ROOT/'verify_session_evidence.py', 'verify.py')
    copy(ROOT/'session-evidence-README.md', 'README.md')
    frozen = Path('C:/workspace/collie/bench/experiments/2026-09-07')
    copy(frozen/'adapter-source/bench/subscription_rank_tasks.py', 'evaluator/frozen_tasks.py')
    copy(frozen/'snapshot/normalized_batch.py', 'evaluator/frozen_evaluator.py')
    write('validation.json', {'created_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                             'candidate_hashes': len(native)+len(normalized),
                             'independent_candidates_regraded': regraded['candidates'], 'model_calls': 0,
                             'sessions_completed': args.sessions_completed,
                             'second_session_status': args.second_session_status,
                             'meaning_of_session_complete': 'Planned and exploratory work finished in the two allowance windows; not a claim that both allowances were exhausted.',
                             'product_validation_source_commit': regression['source_commit'] if args.second_session_status == 'complete' else None,
                             'held_out_tasks': ['ordered-pathrule-priority-engine', 'capacity-ledger-reservation-audit']})
    for name in ('export_session_evidence.py', 'analyze_evening.py', 'regrade_new.py',
                 'cache_dialogue_summary.py', 'start_variant.py', 'start_normalized.py',
                 'normalized_ledger_capture.py', 'test_normalized_ledger_capture.py',
                 'runtime_inventory.py', 'session_variant.py', 'test_session_variant.py',
                 'session_receipts.py', 'test_session_receipts.py', 'context_variant.py',
                 'test_context_variant.py', 'probe_session_projection.py',
                 'run_second_window.py', 'wait_second_window.py', 'test_second_window_gate.py',
                 'summarize_second_window.py', 'run_recovery_comparison.py', 'retry_normalized_session2.py',
                 'run_reminder_comparison.py', 'response_reminder_variant.py', 'test_response_reminder_variant.py',
                 'native_effort_smoke.py', 'run_focused.py', 'run_product_regression.py',
                 'long_chain_smoke.py', 'verify_long_chain.py',
                 'verify_product_wheel.py', 'wheel_gui_check.py', 'workflow_layout_review.py',
                 'final_package_check.py', 'verify_bundle_linux.py'):
        copy(ROOT/name, 'snapshot/orchestrator/'+name)

    if (ROOT/'evidence-linux-validation-v2/result.json').exists():
        linux = read(ROOT/'evidence-linux-validation-v2/result.json')
        assert linux['exit'] == 0
        checked = read(ROOT/'reviewed-evidence-stage4/results.json')
        def identities(rows):
            return sorted((row['suite'], row['job'], row['patch_sha256'],
                           row['correct'] if 'correct' in row else row['artifact_correct']) for row in rows)
        assert identities(checked['native'] + checked['normalized']) == identities(native + normalized)
        write('evidence-linux-reconciliation.json', {
            'candidate_identities_patch_hashes_and_grades_match': True,
            'candidates': len(native) + len(normalized),
            'linux_stage': linux['source_stage'],
            'linux_source_manifest_sha256': linux['source_manifest_sha256'],
            'claim': 'Same candidate set; later product validation files were not part of the Linux stage.'})

    manifest = {}
    for path in sorted(destination.rglob('*')):
        if not path.is_file():
            continue
        if path.suffix.lower() == '.png':
            assert path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'), path.name
            manifest[path.relative_to(destination).as_posix()] = digest(path)
            continue
        text = path.read_text(encoding='utf-8')
        assert not re.search(r'sk-ant-[A-Za-z0-9_-]{20,}', text), path.name
        assert not re.search(r'eyJ[A-Za-z0-9_-]{30,}\.[A-Za-z0-9_-]{20,}\.', text), path.name
        manifest[path.relative_to(destination).as_posix()] = digest(path)
    write('SHA256SUMS.json', manifest)
    print(json.dumps({'files': len(manifest), 'native': len(native),
                      'normalized': len(normalized), 'output': str(destination)}))


if __name__ == '__main__':
    main()
