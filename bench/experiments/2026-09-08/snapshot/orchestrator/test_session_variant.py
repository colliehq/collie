import ast
from pathlib import Path
import subprocess
import sys

import pytest
from session_variant import instrument

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'session2-transport-worktree'

@pytest.mark.parametrize('enabled', [False, True])
def test_real_provider_factory_selects_the_requested_experimental_transport(enabled):
    tree = ast.parse((ROOT/'start_native.py').read_text(encoding='utf-8'))
    base = next(ast.literal_eval(n.value) for n in ast.walk(tree)
                if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == 'instrument' for t in n.targets))
    script = ('import sys, json\n'
              f'sys.path.insert(0, {str(SOURCE)!r})\n'
              'from harness import swe\n' + instrument(base, enabled) +
              '\nfrom harness.providers import make_provider\n'
              'provider = make_provider("claude-agent-sdk", model="claude-opus-5", effort="high")\n'
              f'assert provider.session_enabled is {enabled!r}\n'
              'assert provider.subscription_only is True\n'
              'assert provider.structured_output is False\n'
              'assert provider._session is None\n'
              'assert provider.session_evidence["sessions_opened"] == 0\n')
    result = subprocess.run([sys.executable, '-I', '-c', script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
