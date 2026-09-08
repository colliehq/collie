"""The ablation retains normal tool history without bypassing forced recovery."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from context_variant import INSTRUMENT

ROOT = Path(__file__).resolve().parent

@pytest.mark.parametrize('mode', ['normal', 'overflow', 'image'])
def test_context_ablation_preserves_the_host_recovery_and_image_policy(mode):
    script = f'''
import sys
sys.path.insert(0, {str(ROOT/'session2-transport-worktree')!r})
from harness.context import ContextComposer
from types import SimpleNamespace
from unittest.mock import patch
registry = SimpleNamespace(always_on=lambda: [], deferred_names=lambda: [], active_schemas=lambda: [])
composer = ContextComposer(SimpleNamespace(core_blocks=lambda *_: []), registry, auto_prefetch=False)
composer.include_skills = composer.include_project_rules = False
messages = [{{'role': 'user', 'content': 'Preserve this exact requirement.'}}]
for i in range(10):
    messages.extend([{{'role': 'assistant', 'content': 'inspect'}},
                     {{'role': 'tool', 'content': str(i)+'x'*8000}}])
if {mode!r} == 'image':
    messages[0]['content'] = [{{'type': 'text', 'text': 'Keep the requirement.'}},
                              {{'type': 'image', 'data': 'placeholder'}}]
session = {{'messages': messages, '_overflow_shrink': {mode == 'overflow'!r}}}
with patch('harness.live_copilot.model_context', return_value=''):
    _, before, _ = composer.build(session, 'inspect', '.', 'test')
{INSTRUMENT}
with patch('harness.live_copilot.model_context', return_value=''):
    _, after, meta = composer.build(session, 'inspect', '.', 'test')
if {mode!r} == 'normal':
    assert before != after and after == messages
    assert meta.elide_from == 0
else:
    assert before == after
assert len(session['messages'][2]['content']) == 8001
'''
    result = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
