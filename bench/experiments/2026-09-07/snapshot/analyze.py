"""Aggregate allowlisted evidence; keep task correctness separate from clean execution."""
import collections, datetime, hashlib, json, statistics
from pathlib import Path

ROOT=Path(__file__).resolve().parent

def read(path):return json.loads(path.read_text(encoding='utf-8'))
def sha(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def median(values):return round(statistics.median(values),3) if values else None
def canonical_usage(usage):
    cache_write=usage.get('cache_creation')
    if not isinstance(cache_write,(int,float)):
        cache_write=usage.get('cache_creation_input_tokens')
    return {
        'input':usage.get('input_tokens'), 'output':usage.get('output_tokens'),
        'cache_read':usage.get('cache_read',usage.get('cache_read_input_tokens')),
        'cache_write':cache_write,
    }

def native_rows():
    rows=[]
    for original in sorted(ROOT.glob('native-*/attempts/*/result.json')):
        path=original.with_name('result-regraded.json')
        if not path.exists():path=original
        data=read(path);error=data.get('error','');worker={}
        wp=original.with_name('worker-result.json')
        if wp.exists():worker=read(wp)
        detail=str(worker.get('error','')).lower()
        protocol_error=any(s in detail for s in ('response_contract_error','structured-response repair','protocol:'))
        infrastructure=data['status']=='invalid_infrastructure' and not protocol_error
        turns=worker.get('model_calls')
        if data['arm']=='claude-code':
            for line in original.with_name('trace.jsonl').read_text(encoding='utf-8',errors='replace').splitlines():
                try:event=json.loads(line)
                except ValueError:continue
                if event.get('type')=='result':turns=event.get('num_turns')
        rows.append({
            'suite':original.parents[2].name,'run':data['job'],'task':data['task'],
            'arm':data['arm'],'rep':data['repetition'],'original_status':read(original)['status'],
            'correct':data['grader']['passed'],'clean_execution':not error,
            'infrastructure_invalid':infrastructure,'protocol_error':protocol_error,
            'error':error,'seconds':data['elapsed_seconds'],'usage':canonical_usage(data['usage']),
            'reported_turns':turns,'turn_measure':'physical calls' if data['arm']=='collie' else 'native CLI turns',
            'started':data['started'],'ended':data['ended'],
            'patch_sha256':data['patch_sha256'],'task_sha256':data['task_sha256'],
            'original_result_sha256':sha(original),
            'scoring_correction':data.get('scoring_correction'),
            'adjudication':('Structured-response failure is a harness/protocol error, not shared infrastructure. '
                            'Its original candidate still passed the external grader.') if protocol_error else None,
        })
    return rows

def normalized_rows():
    rows=[]
    for original in sorted(ROOT.glob('normalized-*/results-*/runs/*/result.json')):
        path=original.with_name('result-regraded.json')
        if not path.exists():path=original
        data=read(path);led=data.get('sidecar_request_evidence',{})
        rows.append({
            'suite':original.parents[3].name,'run':data['run_id'],'task':data['task_id'],
            'arm':data['arm'],'rep':data['repetition'],'original_status':read(original)['status'],
            'status':data['status'],'correct':data['resolved'],
            'clean_execution':data.get('worker_outcome')=='candidate' and not data.get('worker_error_code'),
            'infrastructure_invalid':data['status'].startswith('invalid_'),
            'seconds':round(data['duration_ms']/1000,3),'usage':canonical_usage(data.get('usage',{})),
            'request_evidence':{k:v for k,v in led.items() if isinstance(v,(int,float,bool))},
            'error':data.get('error_code'),'ended':data['completed_at_utc'],
            'patch_sha256':data['patch_sha256'],'task_sha256':data['task_sha256'],
            'original_result_sha256':sha(original),'scoring_correction':data.get('scoring_correction'),
        })
    return rows

def summaries(rows):
    result=[]
    groups=collections.defaultdict(list)
    for row in rows:groups[(row['arm'],row['task'])].append(row)
    for (arm,task),group in sorted(groups.items()):
        usable=[r for r in group if not r['infrastructure_invalid']]
        result.append({'arm':arm,'task':task,'attempts':len(group),'valid_attempts':len(usable),
            'correct':sum(r['correct'] for r in usable),'clean':sum(r['clean_execution'] for r in usable),
            'protocol_errors':sum(r.get('protocol_error',False) for r in usable),
            'median_seconds':median([r['seconds'] for r in usable]),
            'median_cache_write_tokens':median([r['usage']['cache_write'] for r in usable if r['usage']['cache_write'] is not None]),
            'median_cache_read_tokens':median([r['usage']['cache_read'] for r in usable if r['usage']['cache_read'] is not None]),
            'median_output_tokens':median([r['usage']['output'] for r in usable if r['usage']['output'] is not None])})
    return result

native=native_rows();normalized=normalized_rows()
primary_native=[r for r in native if r['suite'].startswith('native-complex-')]
primary_normalized=[r for r in normalized if r['suite'] in ('normalized-initial','normalized-patch','normalized-cache-inbox-v2')]
quota=[json.loads(line) for line in (ROOT/'quota.jsonl').read_text(encoding='utf-8').splitlines()]
audits=[]
for path in sorted(ROOT.glob('design/*/result.json')):
    data=read(path);audits.append({'job':data['job'],'exit':data['exit'],'seconds':data['seconds'],
        'usage':canonical_usage(data['usage']) if isinstance(data.get('usage'),dict) else None,
        'trace_sha256':sha(path.with_name('trace.jsonl'))})
workflows=read(ROOT/'workflow-results.json')
output={
    'schema_version':1,'created_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat(),
    'source_commit':'8577c10a33370bf84ae9cf953db64354be92a30d','collie_version':'0.25.0',
    'requested_model':'claude-opus-5','reasoning_effort':'high','native_claude_version':'2.1.221',
    'normalized_versions':{'pi':'0.84.1','prime':'0.7.2','hermes':'0.15.2','claude_agent_sdk':'0.2.136'},
    'quota':quota,'design_and_source_audits':audits,
    'task_admission':read(ROOT/'task-validation.json'),
    'native':native,'normalized':normalized,'workflows':workflows,
    'primary_native_summary':summaries(primary_native),
    'primary_normalized_summary':summaries(primary_normalized),
    'shell_ablation_summary':summaries([r for r in normalized if r['suite']=='normalized-collie-shell']),
    'native_coverage_diagnostic_summary':summaries([r for r in native if 'coverage' in r['suite']]),
    'normalized_coverage_diagnostic_summary':summaries([r for r in normalized if 'coverage' in r['suite']]),
    'counts':{'native_coding_attempts':len(native),'normalized_coding_attempts':len(normalized),
        'primary_native_attempts':len(primary_native),'primary_normalized_attempts':len(primary_normalized),
        'workflow_groups':len(workflows),'workflow_phases':sum(len(r['phases']) for r in workflows),
        'workflow_independent_assertions':sum(p['independent_checks'] for r in workflows for p in r['phases']),
        'source_audits':sum(r['job'].startswith('audit-') for r in audits),
        'design_jobs':len(audits),'design_timeouts':sum(r['exit']=='timeout' for r in audits)},
    'limitations':[
        'Exploratory taskset, not SWE-bench or a universal harness ranking; small per-task samples.',
        'Native product and normalized adapter results are separate tracks; system prompts/tool surfaces differ.',
        'Same requested model ID and effort do not freeze provider weights or all sampling settings.',
        'Normalized opponents use available pinned image versions, not a claim about their latest releases.',
        'Native Windows arms use restricted file tools; normalized agents are container-isolated and have differing local tools.',
        'The shell ablation changes only Collie tool availability, not the shipped default product.',
        'The coverage diagnostic is post hoc, after observing a shared failure; it is not confirmatory evidence.',
        'Two scoring defects were corrected uniformly on unchanged candidates; original results remain intact.',
        'Account utilization may include other activity; missing usage on timed-out design jobs is unknown, not zero.',
        'API-equivalent dollar estimates are not subscription charges and are not used to rank efficiency.',
    ],
}
(ROOT/'results-summary.json').write_text(json.dumps(output,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'counts':output['counts'],'native':output['primary_native_summary'],
                  'normalized':output['primary_normalized_summary'],'shell':output['shell_ablation_summary']},indent=2))
