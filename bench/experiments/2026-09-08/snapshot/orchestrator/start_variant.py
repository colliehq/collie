"""Explicit source-variant experiment using the frozen evaluator (not historical replay)."""
import argparse, ast, datetime, hashlib, importlib.util, json, subprocess, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parent
BASE=Path('C:/workspace/collie/bench/experiments/2026-09-07')
OLD='8577c10a33370bf84ae9cf953db64354be92a30d'
p=argparse.ArgumentParser();p.add_argument('--repo',type=Path,required=True);p.add_argument('--tasks',required=True);p.add_argument('--name',required=True)
p.add_argument('--repetitions',type=int,default=3);p.add_argument('--concurrency',type=int,default=6);p.add_argument('--arms',default='collie,claude-code')
p.add_argument('--coverage',action='store_true');p.add_argument('--prepare-only',action='store_true')
p.add_argument('--structured-mode', choices=('plain', 'schema'))
p.add_argument('--session-mode', choices=('session', 'stateless'))
p.add_argument('--context-mode', choices=('default', 'full-tool-history'), default='default')
p.add_argument('--response-reminder', action='store_true')
p.add_argument('--native-runtime', type=Path)
a=p.parse_args();repo=a.repo.resolve();folder=ROOT/a.name
if a.response_reminder and a.session_mode != 'session':
 raise SystemExit('A response reminder is only a persistent-session treatment')
head=subprocess.check_output(['git','-C',str(repo),'rev-parse','HEAD'],text=True).strip()
if subprocess.check_output(['git','-C',str(repo),'status','--porcelain'],text=True).strip():raise SystemExit('Source checkout must be clean')
arms=tuple(a.arms.split(','))
if not arms or not set(arms)<={'collie','claude-code'} or len(set(arms))!=len(arms):raise SystemExit('Unknown/duplicate arm')
tasks=[]
for task_id in a.tasks.split(','):
 tasks.extend(json.loads((ROOT/'task-admission'/task_id/'task.json').read_text(encoding='utf-8')))
if a.coverage:
 from start_native import NUDGE
 tasks=[{**t,'task_id':t['task_id']+'-coverage-review','prompt':t['prompt']+NUDGE} for t in tasks]
spec=importlib.util.spec_from_file_location('frozen_preparer',BASE/'reproduce.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
# Explicitly re-pin admission to the measured source variant. This is a new
# experiment, and both the plan and runtime manifest carry its actual commit.
module.COMMIT=head
deadline=(datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=100)).isoformat()
sys.argv=['source-variant','--track','native','--collie-repo',str(repo),'--output',str(folder),'--deadline-utc',deadline,
 '--repetitions',str(a.repetitions),'--concurrency',str(a.concurrency),'--prepare-only']
if a.native_runtime:
 sys.argv += ['--claude-cli', str(a.native_runtime.resolve())]
module.main()
(folder/'tasks.json').write_text(json.dumps(tasks,ensure_ascii=False,indent=2),encoding='utf-8')
batch=folder/'native_batch.py';source=batch.read_text(encoding='utf-8')
source=source.replace(OLD,head).replace("for arm in ('collie','claude-code')",'for arm in '+repr(arms))
env_anchor = 'env=environment(),stdin='
assert source.count(env_anchor) == 1, 'native controller environment location changed'
source = source.replace(env_anchor, 'env={**environment(), "CLAUDE_CODE_MAX_RETRIES": "0"},stdin=')
from runtime_inventory import inventory
native_source = ast.parse((folder/'native_jobs.py').read_text(encoding='utf-8'))
native_path = next(ast.literal_eval(n.value.args[0]) for n in native_source.body
 if isinstance(n, ast.Assign) and any(isinstance(t,ast.Name) and t.id=='CLI' for t in n.targets))
runtimes = inventory(native_path)
source = source.replace("'claude_cli_version':'2.1.221'",
                        "'claude_cli_version':" + repr(runtimes['native_cli']['version']))
batch.write_text(source,encoding='utf-8')
tree=ast.parse((ROOT/'start_native.py').read_text(encoding='utf-8'))
instrument=next(ast.literal_eval(n.value) for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='instrument' for t in n.targets))
if a.session_mode:
 from session_variant import instrument as session_instrument
 instrument = session_instrument(instrument, a.session_mode == 'session')
if a.context_mode == 'full-tool-history':
 from context_variant import INSTRUMENT
 instrument += INSTRUMENT
if a.structured_mode:
 instrument += '\n_original_init = ClaudeAgentSdkProvider.__init__\ndef _configured_init(self, *args, **kwargs):\n    kwargs["structured_output"] = '+repr(a.structured_mode=='schema')+'\n    _original_init(self, *args, **kwargs)\nClaudeAgentSdkProvider.__init__ = _configured_init\n'
if a.response_reminder:
 from response_reminder_variant import INSTRUMENT
 instrument += INSTRUMENT
worker=folder/'native_attempt.py';source=worker.read_text(encoding='utf-8')
source=source.replace('result=swe.predict_collie(',instrument+'\nresult=swe.predict_collie(')
worker.write_text(source,encoding='utf-8')
metadata={'name':a.name,'source_commit':head,'baseline_commit':OLD,'tasks':[t['task_id'] for t in tasks],
 'coverage_nudge':a.coverage,'arms':arms,'repetitions':a.repetitions,'concurrency':a.concurrency,
 'structured_mode_override':a.structured_mode,
 'experimental_session_mode':a.session_mode,
 'experimental_context_mode':a.context_mode,
 'experimental_response_reminder':a.response_reminder,
 'http_retry_override':0,
 'runtime_inventory':runtimes,
 'methodology':'New source-variant experiment. Frozen evaluator; explicit source pin, task set and observation wrapper changes. No historical result overwritten.',
 'scripts':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in folder.glob('*.py')}}
(folder/'experiment.json').write_text(json.dumps(metadata,indent=2),encoding='utf-8')
plan=json.loads((folder/'replay-plan.json').read_text(encoding='utf-8'));plan['methodology']=metadata['methodology'];plan['experiment_manifest']='experiment.json'
(folder/'replay-plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
if not a.prepare_only:raise SystemExit(subprocess.call(plan['command'],cwd=folder))
