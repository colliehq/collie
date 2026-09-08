import dataclasses, datetime, json, os, sys, time, uuid
from pathlib import Path

root=Path(sys.argv[1]).resolve()
state=root/'state';state.mkdir(exist_ok=True)
for key in list(os.environ):
    if key.startswith(('ANTHROPIC_','CLAUDE_CODE_USE_','COLLIE_')) or key in ('CLAUDECODE','CLAUDE_CODE_OAUTH_TOKEN'):
        os.environ.pop(key,None)
os.environ.update(COLLIE_STATE_DIR=str(state),COLLIE_DATA_DIR=str(state/'data'),
    COLLIE_SETTINGS_PATH=str(state/'settings.json'),COLLIE_SESSIONS_DIR=str(state/'sessions'),
    COLLIE_MCP_CONFIG=str(state/'mcp.json'),COLLIE_EMBED='hash',COLLIE_LANG='en',
    COLLIE_PROVIDER='claude-agent-sdk',COLLIE_MODEL='claude-opus-5',COLLIE_RUNNER='collie',
    COLLIE_BROWSER_BRIDGE='0',COLLIE_REMOTE='off')
(state/'settings.json').write_text('{}',encoding='utf-8')
(state/'mcp.json').write_text('{"mcpServers":{}}',encoding='utf-8')
sys.path.insert(0,'C:\\workspace\\collie-benchmark-2026-09-07-evening\\structured-pin')
from harness import swe

requests=0
def record(event):
    with (root/'request-ledger.jsonl').open('a',encoding='utf-8') as output:
        output.write(json.dumps(event)+'\n');output.flush();os.fsync(output.fileno())

def reserve(kind):
    global requests
    if requests>=48 or time.time()>=1788836934.413416-15:
        return ''
    request_id=uuid.uuid4().hex
    record({'event':'reserved','id':request_id,'kind':kind,'at':datetime.datetime.now(datetime.timezone.utc).isoformat()})
    requests+=1
    return request_id

def settle(request_id,status):
    record({'event':'settled','id':request_id,'status':status})


from harness.claude_agent_sdk import ClaudeAgentSdkProvider
import hashlib
_original_worker = ClaudeAgentSdkProvider._run_worker
_call_index = 0
def _observed_worker(self, request, *args, **kwargs):
    global _call_index
    number = _call_index; _call_index += 1
    folder = root/'provider-evidence'; folder.mkdir(exist_ok=True)
    (folder/('%03d-request.json' % number)).write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
    try:
        data = _original_worker(self, request, *args, **kwargs)
    except Exception as exc:
        (folder/('%03d-failure.json' % number)).write_text(json.dumps({'error_type':type(exc).__name__}),encoding='utf-8')
        raise
    (folder/('%03d-result.json' % number)).write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    return data
ClaudeAgentSdkProvider._run_worker = _observed_worker

result=swe.predict_collie(str(root/'workspace'),(root/'prompt.txt').read_text(encoding='utf-8'),
    provider='claude-agent-sdk',model='claude-opus-5',max_turns=48,
    benchmark_safe=True,benchmark_effort='high',
    complete_prompt=(root/'prompt.txt').read_text(encoding='utf-8'),
    request_gate=reserve,request_complete=settle,request_scope=root.name)
data=dataclasses.asdict(result) if dataclasses.is_dataclass(result) else vars(result)
(root/'worker-result.json').write_text(json.dumps(data,default=str,indent=2),encoding='utf-8')
