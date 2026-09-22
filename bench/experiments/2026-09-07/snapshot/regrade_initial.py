"""Correct the success-marker adapter without another model call or overwriting results."""
import hashlib, json, subprocess
from pathlib import Path
import run_normalized as launch

ROOT=launch.ROOT;module=launch.module
helpers=launch.load_helpers(Path(r'C:\workspace\collie-bench-new'))
tasks,_=module.load_tasks(ROOT/'normalized-initial-tasks.json',helpers)
by_id={t['task_id']:t for t in tasks}
results=[]
for result_path in sorted((ROOT/'normalized-initial').glob('results-*/runs/*/result.json')):
    original=json.loads(result_path.read_text(encoding='utf-8'))
    task=by_id[original['task_id']]
    if 'sys.exit(main())' not in task['hidden_grader']:continue
    if original['status'] not in ('valid_unresolved','valid_resolved'):continue
    corrected_path=result_path.with_name('result-regraded.json')
    if corrected_path.exists():continue
    work=ROOT/'regrades'/result_path.parent.name;work.mkdir(parents=True,exist_ok=False)
    helpers.materialize_task(task,work)
    patch=result_path.with_name('patch.diff')
    for args in (['git','init','--quiet'],['git','apply','--check',str(patch)],['git','apply',str(patch)]):
        result=subprocess.run(args,cwd=work,capture_output=True,timeout=30)
        if result.returncode:raise RuntimeError('patch reconstruction failed: '+result.stderr.decode(errors='replace')[-500:])
    grade=module.grade(task,work,original['patch_sha256'],helpers)
    corrected={**original,'status':'valid_resolved' if grade['resolved'] else 'valid_unresolved',
        'resolved':grade['resolved'],'grader':grade,'original_status':original['status'],
        'error_code':'' if grade['resolved'] else grade['failure_detail'],
        'scoring_correction':{'reason':'Allow trusted grader main() exit=0 to reach the success marker; contract and candidate unchanged.',
           'original_result_sha256':hashlib.sha256(result_path.read_bytes()).hexdigest(),
           'evaluator_sha256':hashlib.sha256((ROOT/'normalized_batch.py').read_bytes()).hexdigest(),
           'model_rerun':False}}
    corrected_path.write_text(json.dumps(corrected,indent=2),encoding='utf-8')
    results.append({'run':result_path.parent.name,'original':original['status'],'corrected':corrected['status']})
print(json.dumps(results,indent=2))
