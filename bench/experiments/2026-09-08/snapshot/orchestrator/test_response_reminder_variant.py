"""Real provider planning keeps first/reset prompts unchanged; only plain deltas change."""
from pathlib import Path
import subprocess
import sys

import pytest
from response_reminder_variant import INSTRUMENT, REMINDER

ROOT = Path(__file__).resolve().parent

@pytest.mark.parametrize('structured', [False, True])
def test_reminder_respects_real_provider_projection_and_reset_boundaries(structured):
    script = f'''
import sys
sys.path[:0] = [{str(ROOT/'session2-recovery-prototype')!r}, {str(ROOT/'session2-recovery-prototype/tests')!r}]
from test_experimental_session_sdk import (_Provider, _Ledger, READ, EDIT, DONE,
    call, assistant, observation, prompts, plain_payload, structured_payload)
from harness.experimental_session_sdk import ExperimentalSessionSdkProvider as ClaudeAgentSdkProvider
{INSTRUMENT}
payload = structured_payload if {structured!r} else plain_payload
provider = _Provider([payload(READ), payload(EDIT), payload(DONE)], structured_output={structured!r})
ledger = _Ledger()
messages = [{{'role': 'user', 'content': 'inspect the module'}}]
first = call(provider, ledger, messages)
messages += [assistant(first), observation(first, 'original body')]
second = call(provider, ledger, messages)
assert not prompts(provider)[0].endswith({REMINDER!r})
assert prompts(provider)[1].endswith({REMINDER!r}) is {not structured!r}
third = call(provider, ledger, [{{'role': 'user', 'content': 'host summary: inspection is complete'}}])
assert third.stop_reason == 'end_turn'
assert len(provider.channels) == 2
assert not prompts(provider, 1)[0].endswith({REMINDER!r})
assert 'original body' not in prompts(provider, 1)[0]
assert len(ledger.reserved) == len(ledger.settled) == 3
assert all(outcome == 'completed' for _, outcome in ledger.settled)
assert provider.close_session('test complete')
assert provider._session is None and not provider._unresolved and not provider._active_runs
'''
    result = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
