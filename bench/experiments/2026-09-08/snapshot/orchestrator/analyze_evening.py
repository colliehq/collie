"""Adjudicate all candidates, retaining harness/budget failures in the denominator."""
import collections, hashlib, json, statistics
from pathlib import Path
from session_receipts import summarize as summarize_session
ROOT=Path(__file__).resolve().parent
def read(path):return json.loads(path.read_text(encoding='utf-8'))
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
rows=[]
for path in sorted(ROOT.glob('native-*/native-replay/attempts/*/result.json')):
 data=read(path);folder=path.parent;worker={};detail='';ledger=[]
 experiment = read(path.parents[3]/'experiment.json')
 if (folder/'worker-result.json').exists():worker=read(folder/'worker-result.json');detail=str(worker.get('error') or '')
 elif data.get('arm')=='claude-code':
  for line in (folder/'trace.jsonl').read_text(encoding='utf-8').splitlines():
   try:event=json.loads(line)
   except ValueError:continue
   if event.get('type')=='result':worker=event
  detail=str(worker.get('result') or '') if worker.get('is_error') else ''
 text=(detail+' '+str(data.get('error') or '')).lower()
 if 'response_contract_error' in text:failure='response_protocol'
 elif 'sdk structured formatter' in text or 'sdk emitted a tool result before the structured formatter' in text or 'sdk assistant attempted foreign tool use' in text:failure='structured_transport'
 elif 'reservation denied' in text:failure='request_budget'
 elif worker.get('turns_exhausted') or worker.get('subtype')=='error_max_turns':failure='turn_budget'
 elif data.get('error')=='provider_capacity':failure='provider_capacity'
 elif 'provider rejected the request: server_error' in text:failure='provider_server'
 elif data.get('error') or worker.get('is_error'):failure='other_error'
 else:failure=None
 session = None
 if data.get('arm') == 'collie' and experiment.get('experimental_session_mode'):
  receipt_path = folder/'provider-session-final.json'
  session = summarize_session(read(receipt_path) if receipt_path.exists() else None,
                              experiment['experimental_session_mode'] == 'session')
  if not session['clean'] and failure is None:
   failure = session['failure']
 wp=folder/'request-ledger.jsonl'
 if wp.exists():
  ledger=[json.loads(line) for line in wp.read_text(encoding='utf-8').splitlines()]
 reserved=sum(x.get('event')=='reserved' for x in ledger)
 released=sum(x.get('event')=='settled' and x.get('status')=='released' for x in ledger)
 usage=data.get('usage',{});cache_write=usage.get('cache_creation')
 if not isinstance(cache_write,(int,float)):cache_write=usage.get('cache_creation_input_tokens',0)
 rows.append({'suite':path.parents[3].name,'job':data['job'],'task':data['task'],
  'arm':data['arm'],'rep':data['repetition'],'correct':data['grader']['passed'],'clean':failure is None,
  'failure':failure,'original_status':data['status'],'seconds':data['elapsed_seconds'],
  'cache_write':cache_write,'cache_read':usage.get('cache_read',usage.get('cache_read_input_tokens',0)),
  'input':usage.get('input_tokens',0),'output':usage.get('output_tokens',0),
  'reported_calls':worker.get('model_calls',worker.get('num_turns')),
  'calls_unit':'physical_model_requests' if data['arm']=='collie' else 'native_cli_turns',
  'failure_classification':'post_run_error_text_and_structured_terminal_fields',
  'reserved_not_released':reserved-released if ledger else None,
  'worker_invocations_observed':len(list((folder/'provider-evidence').glob('*-request.json'))) if ledger else None,
  'session_transport': session,
  'detail':detail if data['arm']=='collie' else (worker.get('subtype') if failure else ''),
  'patch_sha256':digest(folder/'patch.diff'),'result_sha256':digest(path)})
groups=collections.defaultdict(list)
for row in rows:groups[(row['suite'],row['task'],row['arm'])].append(row)
summary=[]
for (suite,task,arm),group in sorted(groups.items()):
 summary.append({'suite':suite,'task':task,'arm':arm,'n':len(group),'correct':sum(r['correct'] for r in group),
  'clean':sum(r['clean'] for r in group),'failures':dict(collections.Counter(r['failure'] for r in group if r['failure'])),
  **{'median_'+key:round(statistics.median(r[key] for r in group),1) for key in ('seconds','cache_write','cache_read','output')}})
out={'rows':rows,'summary':summary,'classification':'All candidates retained. Request/turn caps and protocol failures are harness outcomes, not invalid infrastructure. No provider-capacity failures excluded silently.'}
(ROOT/'evening-analysis.json').write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
for row in summary:print(json.dumps(row))
