"""Exercise the real context projection with scripted tool replies, without a model."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT/'session2-transport-worktree'
sys.path[:0] = [str(SOURCE), str(SOURCE/'tests')]
from harness.context import ContextComposer
from test_experimental_session_sdk import (_Provider, _Ledger, SCHEMAS, assistant,
                                          observation, call, plain_payload)

output = ROOT/'session-projection-diagnostic'
output.mkdir(exist_ok=False)
rows = []
with tempfile.TemporaryDirectory(prefix='collie-projection-probe-') as directory:
    for projected in (True, False):
        script = [plain_payload(json.dumps({'tool': 'read_file', 'args': {'path': f'file-{i:02d}.py'}}))
                  for i in range(30)]
        provider = _Provider(script, structured_output=False, subscription_only=True)
        assert provider.session_enabled
        ledger = _Ledger()
        registry = SimpleNamespace(always_on=lambda: [SimpleNamespace(name=s['name']) for s in SCHEMAS],
                                   deferred_names=lambda: [], active_schemas=lambda: SCHEMAS)
        composer = ContextComposer(SimpleNamespace(core_blocks=lambda *_: []), registry,
                                   auto_prefetch=False)
        composer.include_skills = composer.include_project_rules = False
        goal = 'Inspect the source files and preserve their reported observations.'
        messages = [{'role': 'user', 'content': goal}]
        turns = []
        try:
            with patch('harness.live_copilot.model_context', return_value=''):
                for index in range(30):
                    system, selected, meta = composer.build({'messages': messages}, goal, directory, 'probe')
                    supplied = selected if projected else meta.pre_elision
                    result = call(provider, ledger, supplied, system=system)
                    assert result.stop_reason != 'error', result.error_detail
                    messages.append(assistant(result))
                    messages.append(observation(result, f'file-{index:02d}\n'+('x'*4000)))
                    turns.append({'turn': index + 1, 'sessions_opened': len(provider.channels),
                                  'elide_from': meta.elide_from,
                                  'native_user_prompt_chars': len(provider.channels[-1].sent[-1]['prompt'])})
        finally:
            provider.close_session('diagnostic_finished')
        assert not provider._unresolved and not provider._active_runs
        rows.append({'projection': 'production_elision' if projected else 'unelided_mechanism_control',
                     'turns': turns, 'session_evidence': provider.session_evidence,
                     'native_user_prompt_chars': sum(row['native_user_prompt_chars'] for row in turns)})
record = {'source_commit': subprocess.check_output(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'],
                                                   text=True).strip(),
          'model_calls': 0, 'scripted_turns': 60, 'rows': rows,
          'claim': 'Mechanism diagnostic only. Real ContextComposer and session adapter; scripted worker replies. '
                   'Prompt characters are host-to-native-client input, not API bytes, billed tokens or coding quality. '
                   'The unelided control deliberately omits production context elision and is not a product mode.'}
(output/'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps([{'projection': row['projection'], **row['session_evidence'],
                   'native_user_prompt_chars': row['native_user_prompt_chars']} for row in rows]))
