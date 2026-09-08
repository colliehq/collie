"""Real read-only dependency chain exceeding forty tool turns; no coding rank claim."""
import concurrent.futures
import dataclasses
import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
REPO = Path('C:/workspace/collie')
OUT = ROOT/'long-chain-real-smoke'
CLI = Path('C:/Users/Sining Xu/AppData/Local/Python/pythoncore-3.14-64/Lib/site-packages/claude_agent_sdk/_bundled/claude.exe')


def worker(arm):
    folder = OUT/arm
    work = folder/'work'
    for key in list(os.environ):
        if key.startswith('COLLIE_'):
            del os.environ[key]
    state = folder/'state'
    state.mkdir()
    (state/'settings.json').write_text('{}')
    (state/'mcp.json').write_text('{"mcpServers":{}}')
    os.environ.update(COLLIE_STATE_DIR=str(state), COLLIE_DATA_DIR=str(state/'data'),
        COLLIE_SETTINGS_PATH=str(state/'settings.json'), COLLIE_SESSIONS_DIR=str(state/'sessions'),
        COLLIE_MCP_CONFIG=str(state/'mcp.json'), COLLIE_EMBED='hash', COLLIE_LANG='en',
        COLLIE_BROWSER_BRIDGE='0', COLLIE_REMOTE='off', COLLIE_WEBSEARCH='0')
    sys.path.insert(0, str(REPO))
    prompt = (OUT/'prompt.txt').read_text()
    start = time.monotonic()
    if arm.startswith('collie'):
        from harness.cli import make_harness
        harness = make_harness(str(work), provider='claude-agent-sdk', model='claude-opus-5',
                               effort='high', embed='hash', subscription_only=True)
        assert harness.max_turns == 0 and harness._max_turns_hard_cap is None
        harness.registry.retain(['read_file'])
        harness.composer.auto_prefetch = False
        harness.composer.include_project_rules = False
        harness.composer.include_skills = False
        harness.max_model_calls = 64  # explicit experiment resource bound, above forty
        harness.max_retries = 0
        harness.self_verify = False
        harness.force_edit = False
        deadline = time.monotonic()+780
        harness.cancelled = lambda: time.monotonic() >= deadline
        issued = 0
        def ledger(event):
            with (folder/'requests.jsonl').open('a',encoding='utf-8') as log:
                log.write(json.dumps(event)+'\n'); log.flush(); os.fsync(log.fileno())
        def reserve(kind):
            nonlocal issued
            if issued >= 64 or time.monotonic() >= deadline:
                return ''
            request_id = uuid.uuid4().hex
            issued += 1
            ledger({'event':'reserved','id':request_id,'kind':kind})
            return request_id
        def settle(request_id,status):
            ledger({'event':'settled','id':request_id,'status':status})
        with harness.provider.request_authority(reserve,settle,request_scope=arm):
            result = harness.run('chain45', prompt, consolidate=False)
        record = dataclasses.asdict(result)
        (folder/'raw-result.json').write_text(json.dumps(record,default=str,indent=2),encoding='utf-8')
        calls = [m for m in result.messages or [] if m.get('role') == 'tool']
        row = {'arm':arm, 'answer':result.answer, 'error':result.error,
               'stop_reason':result.stop_reason, 'turns':result.turns,
               'model_calls':result.model_calls, 'tool_results':len(calls),
               'default_turn_limit':harness.max_turns, 'experiment_model_call_limit':64,
               'cache_write':result.cache_creation, 'cache_read':result.cache_read,
               'passed':not result.error and result.stop_reason=='completed' and
                        result.turns>40 and len(calls)>=45 and 'CHAIN-COMPLETE-9f24' in result.answer}
    else:
        from harness.claude_code_runner import ClaudeCodeRunner
        runner = ClaudeCodeRunner(executable=str(CLI), model='claude-opus-5', effort='high',
                                  tools=('Read',), default_timeout_s=780)
        result = runner.start(prompt, str(work))
        (folder/'raw-result.json').write_text(json.dumps(result.to_dict(),indent=2),encoding='utf-8')
        calls = []
        for event in result.events:
            message = event.payload.get('message') or {}
            content = message.get('content') or []
            if isinstance(content, list):
                calls.extend(c for c in content if isinstance(c,dict) and c.get('type')=='tool_use')
        row = {'arm':arm, 'answer':result.final_output, 'error':result.error,
               'settled':result.settled, 'tool_calls':len(calls), 'usage':result.usage,
               'passed':result.settled and not result.error and not result.recovery_required and
                        len(calls)>=45 and 'CHAIN-COMPLETE-9f24' in result.final_output}
    row['seconds'] = round(time.monotonic()-start,3)
    (folder/'result.json').write_text(json.dumps(row,indent=2),encoding='utf-8')
    print(json.dumps(row))


if len(sys.argv)>1:
    worker(sys.argv[1])
else:
    from quota import snapshot
    from runtime_inventory import describe
    OUT.mkdir(exist_ok=False)
    quota = snapshot()
    (OUT/'quota.json').write_text(json.dumps(quota,indent=2))
    assert quota.get('ok') and quota['extra_usage']['is_enabled'] is False
    assert quota['five_hour']['utilization'] < 92
    names = [uuid.uuid4().hex+'.txt' for _ in range(45)]
    prompt = ('This is a read-only workflow continuity check. Begin with '+names[0]+'. '
              'Each file names the next file; follow the chain until END and report the terminal marker. '
              'Read only the named chain files in this directory, one dependency at a time. '
              'There are 45 steps. Do not stop at an arbitrary count. Do not edit files, use network, '
              'access credentials, or delegate. Give the terminal marker only after reading END.')
    (OUT/'prompt.txt').write_text(prompt)
    for arm in ('collie','claude-code'):
        work = OUT/arm/'work'
        work.mkdir(parents=True)
        for index,name in enumerate(names):
            (work/name).write_text('next: '+names[index+1] if index<44 else 'END: CHAIN-COMPLETE-9f24')
    plan = {'registered_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip(),
            'runtime':describe(CLI), 'steps':45, 'arms':['collie','claude-code'],
            'concurrency':2, 'claim':'Synthetic dependent-read continuity check, not coding benchmark or latency rank.',
            'tool_scope':'Only the read_file/Read tool; project prefetch and skills disabled in Collie.'}
    (OUT/'plan.json').write_text(json.dumps(plan,indent=2))
    def launch(arm):
        with (OUT/arm/'run.log').open('w',encoding='utf-8') as log:
            p = subprocess.run([sys.executable,str(Path(__file__).resolve()),arm], stdout=log,
                               stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
        return {'arm':arm,'exit':p.returncode}
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        launched = list(pool.map(launch, ('collie','claude-code')))
    record = {'plan':plan, 'controllers':launched,
              'results':[json.loads((OUT/arm/'result.json').read_text()) for arm in ('collie','claude-code')
                         if (OUT/arm/'result.json').exists()]}
    (OUT/'result.json').write_text(json.dumps(record,indent=2))
    print(json.dumps(record))
