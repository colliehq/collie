"""Remove a grader-only return-container restriction absent from the user contract."""
import hashlib, json, subprocess
from pathlib import Path
import run_normalized as launch
from native_batch import grade, write_json

ROOT=launch.ROOT;module=launch.module
helpers=launch.load_helpers(Path(r'C:\workspace\collie-bench-new'))
task=json.loads((ROOT/'patch-task.json').read_text(encoding='utf-8'))[0]
original_grader=task['hidden_grader']
assert original_grader.count('report.paths()')==1
task={**task,'hidden_grader':original_grader.replace('report.paths()','tuple(report.paths())')}
write_json(ROOT/'patch-task-corrected.json',[task])
correction={'reason':'The prompt specifies paths in applied order but no list/tuple return type. Compare the ordered contents.',
    'old_grader_sha256':hashlib.sha256(original_grader.encode()).hexdigest(),
    'new_grader_sha256':hashlib.sha256(task['hidden_grader'].encode()).hexdigest(),
    'prompt_changed':False,'fixture_changed':False,'candidate_changed':False,'model_rerun':False}
write_json(ROOT/'patch-grader-correction.json',correction)
rows=[]
for p in sorted((ROOT/'native-complex-patch').glob('attempts/*/result.json')):
    out=p.with_name('result-regraded.json')
    if out.exists():continue
    original=json.loads(p.read_text(encoding='utf-8'))
    if original['status'] not in ('resolved','unresolved'):continue
    evaluated=grade(task,p.parent/'workspace',p.parent,'hidden-grade-corrected')
    updated={**original,'grader':evaluated,'status':'resolved' if evaluated['passed'] else 'unresolved',
             'original_status':original['status'],'scoring_correction':correction}
    write_json(out,updated);rows.append({'run':p.parent.name,'status':updated['status']})
for p in sorted((ROOT/'normalized-patch').glob('results-*/runs/*/result.json')):
    out=p.with_name('result-regraded.json')
    if out.exists():continue
    original=json.loads(p.read_text(encoding='utf-8'))
    if original['status'] not in ('valid_resolved','valid_unresolved'):continue
    work=ROOT/'regrades'/('patch-'+p.parent.name);work.mkdir(parents=True,exist_ok=False)
    helpers.materialize_task(task,work)
    patch=p.with_name('patch.diff')
    for args in (['git','init','--quiet'],['git','apply','--check',str(patch)],['git','apply',str(patch)]):
        result=subprocess.run(args,cwd=work,capture_output=True,timeout=30)
        if result.returncode:raise RuntimeError('patch reconstruction failed')
    evaluated=module.grade(task,work,original['patch_sha256'],helpers)
    updated={**original,'grader':evaluated,'status':'valid_resolved' if evaluated['resolved'] else 'valid_unresolved',
             'resolved':evaluated['resolved'],'error_code':'' if evaluated['resolved'] else evaluated['failure_detail'],
             'original_status':original['status'],'scoring_correction':correction}
    write_json(out,updated);rows.append({'run':p.parent.name,'status':updated['status']})
print(json.dumps(rows,indent=2))
