"""Run all four pinned OSS loop adapters on admitted tasks through one SDK sidecar."""
import argparse, datetime, hashlib, importlib.util, json, shutil, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent;BASE=Path('C:/workspace/collie/bench/experiments/2026-09-07')
p=argparse.ArgumentParser();p.add_argument('--repo',type=Path,required=True);p.add_argument('--tasks',required=True);p.add_argument('--name',required=True)
p.add_argument('--repetitions',type=int,default=2);p.add_argument('--concurrency',type=int,default=4);p.add_argument('--prepare-only',action='store_true')
a=p.parse_args();repo=a.repo.resolve();folder=ROOT/a.name
head=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
if subprocess.check_output(['git','-C',str(repo),'status','--porcelain'],text=True).strip():raise SystemExit('Source must be clean')
tasks=[]
for task_id in a.tasks.split(','):tasks.extend(json.loads((ROOT/'task-admission'/task_id/'task.json').read_text(encoding='utf-8')))
spec=importlib.util.spec_from_file_location('frozen_preparer',BASE/'reproduce.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);m.COMMIT=head
deadline=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=100)).isoformat()
sys.argv=['normalized-variant','--track','normalized','--collie-repo',str(repo),'--output',str(folder),'--deadline-utc',deadline,
 '--repetitions',str(a.repetitions),'--concurrency',str(a.concurrency),'--prepare-only'];m.main()
(folder/'tasks.json').write_text(json.dumps(tasks,ensure_ascii=False,indent=2),encoding='utf-8')
batch=folder/'normalized_batch.py';source=batch.read_text(encoding='utf-8')
source=source.replace('PINNED_COLLIE_COMMIT = "8577c10a33370bf84ae9cf953db64354be92a30d"','PINNED_COLLIE_COMMIT = '+repr(head))
anchor='    ledger_summary: dict[str, Any] = {}\n    ledger_error = ""\n'
assert source.count(anchor)==1, 'evaluator ledger capture location changed'
capture='''    # Capture receipts before validation and workspace cleanup, including failed calls.
    from normalized_ledger_capture import capture_ledger
    try:
        capture_ledger(ledger_dir, raw_dir / "request-receipts.json")
    except Exception:
        _atomic_json(raw_dir / "request-receipts.json", {"capture_failed": True})

'''
source=source.replace(anchor,capture+anchor)
batch.write_text(source,encoding='utf-8')
shutil.copyfile(ROOT/'normalized_ledger_capture.py',folder/'normalized_ledger_capture.py')
metadata={'source_commit':head,'tasks':[t['task_id'] for t in tasks],'arms':['collie','prime','pi','hermes'],
 'methodology':'New admitted tasks and explicit source pin using frozen normalized evaluator. Adapted loop comparison with shared native SDK transport, not native product ranking.',
 'repetitions':a.repetitions,'concurrency':a.concurrency,'scripts':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in folder.glob('*.py')}}
(folder/'experiment.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
plan=json.loads((folder/'replay-plan.json').read_text(encoding='utf-8'));plan['methodology']=metadata['methodology'];plan['experiment_manifest']='experiment.json'
(folder/'replay-plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
if not a.prepare_only:raise SystemExit(subprocess.call(plan['command'],cwd=folder))
