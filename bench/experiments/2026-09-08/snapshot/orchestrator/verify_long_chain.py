"""Check actual read receipts against the generated chain, not the model's claim."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parent/'long-chain-real-smoke'
fixtures = {path.name:path.read_text() for path in (root/'collie/work').glob('*.txt')}
assert len(fixtures) == 45
checks = []
for arm in ('collie-retry','claude-code'):
    raw = json.loads((root/arm/'raw-result.json').read_text())
    calls = []
    if arm == 'collie-retry':
        for message in raw['messages']:
            for call in message.get('tool_calls',[]):
                if call['name']=='read_file':
                    calls.append(call['args'].get('path') or call['args'].get('file_path'))
    else:
        for event in raw['events']:
            content = (event['payload'].get('message') or {}).get('content') or []
            if isinstance(content,list):
                for call in content:
                    if isinstance(call,dict) and call.get('type')=='tool_use' and call['name']=='Read':
                        calls.append(call['input']['file_path'])
    work = (root/arm/'work').resolve()
    paths = [(work/path).resolve() for path in calls]
    assert all(path.parent==work for path in paths)
    names = [path.name for path in paths]
    assert len(names)==45 and set(names)==set(fixtures)
    for index,name in enumerate(names[:-1]):
        assert fixtures[name]=='next: '+names[index+1]
    assert fixtures[names[-1]]=='END: CHAIN-COMPLETE-9f24'
    checks.append({'arm':arm, 'verified_reads':len(names), 'read_order':names,
                   'raw_result_sha256':hashlib.sha256((root/arm/'raw-result.json').read_bytes()).hexdigest()})
ledger = [json.loads(line) for line in (root/'collie-retry/requests.jsonl').read_text().splitlines()]
issued = {row['id'] for row in ledger if row['event']=='reserved'}
settled = {row['id'] for row in ledger if row['event']=='settled' and row['status']=='completed'}
assert len(issued)==46 and issued==settled
record = {'passed':True,'checks':checks,'unique_collie_requests':46,'all_requests_settled':True,
          'model_calls_for_verification':0}
(root/'checked.json').write_text(json.dumps(record,indent=2))
(root/'fixture.json').write_text(json.dumps(fixtures,indent=2))
print(json.dumps({'passed':True,'reads_per_arm':45,'collie_requests':46}))
