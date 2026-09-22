"""Verify a local wheel in a separate install and state directory; zero model calls."""
import argparse
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--wheel', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--source-commit', required=True)
args = parser.parse_args()
out = args.output.resolve()
out.mkdir(exist_ok=False)
target = out/'installed'
state = out/'state'
state.mkdir()
env = {k: v for k, v in os.environ.items() if not k.startswith(('COLLIE_', 'ANTHROPIC_'))}
env.update(COLLIE_STATE_DIR=str(state), COLLIE_DATA_DIR=str(state/'data'),
           COLLIE_SETTINGS_PATH=str(state/'settings.json'), COLLIE_SESSIONS_DIR=str(state/'sessions'),
           COLLIE_MCP_CONFIG=str(state/'mcp.json'), COLLIE_EMBED='hash', COLLIE_LANG='en',
           COLLIE_PROVIDER='mock', COLLIE_RUNNER='collie', COLLIE_SKIP_NET='1',
           COLLIE_BROWSER_BRIDGE='0', COLLIE_REMOTE='off', PYTHONIOENCODING='utf-8')
(state/'settings.json').write_text('{}')
(state/'mcp.json').write_text('{"mcpServers":{}}')
with (out/'install.log').open('w', encoding='utf-8') as log:
    install = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-index',
                              '--target', str(target), str(args.wheel.resolve())],
                             env=env, stdout=log, stderr=subprocess.STDOUT)
assert install.returncode == 0
with zipfile.ZipFile(args.wheel) as archive:
    names = set(archive.namelist())
    for asset in ['harness/webui/index.html', 'harness/webui/logo.svg',
                  'harness/browser_ext/background.js', 'harness/browser_ext/manifest.json',
                  'harness/wallpaper/Program.cs']:
        assert asset in names, asset
    assert not {'harness/browser_ext/token.txt', 'harness/browser_ext/auth.js'} & names
    assert not any(name.startswith('harness/experimental_session') for name in names)
    metadata = archive.read('collie_harness-0.26.0.dist-info/METADATA').decode('utf-8')
    assert Parser().parsestr(metadata)['Version'] == '0.26.0'
script = f'''
import sys, importlib.metadata, pathlib
sys.path.insert(0, {str(target)!r})
import harness
from harness import sessions
assert pathlib.Path(harness.__file__).resolve().is_relative_to(pathlib.Path({str(target)!r}))
assert harness.__version__ == importlib.metadata.version('collie-harness') == '0.26.0'
session_id = 'wheel-roundtrip'
messages = [{{'role': 'user', 'content': 'Keep this exact request.'}},
            {{'role': 'assistant', 'content': 'Recorded.'}}]
sessions.save(session_id, messages, cwd={str(out)!r}, answer='Recorded.')
restored = sessions.load(session_id)
assert restored['messages'] == messages
from harness.providers import make_provider
provider = make_provider('mock')
assert provider is not None
print('isolated package import, metadata, mock provider and session roundtrip passed')
'''
checks = []
for name, code in [('package', script), ('cli-help',
        f'import sys, runpy; sys.path.insert(0, {str(target)!r}); '
        'sys.argv=["collie", "--help"]; runpy.run_module("harness.cli", run_name="__main__")')]:
    with (out/(name+'.log')).open('w', encoding='utf-8') as log:
        result = subprocess.run([sys.executable, '-I', '-c', code], cwd=out, env=env,
                                stdout=log, stderr=subprocess.STDOUT, timeout=60)
    checks.append({'name': name, 'exit': result.returncode})
record = {'source_commit': args.source_commit, 'wheel': args.wheel.name,
          'wheel_sha256': hashlib.sha256(args.wheel.read_bytes()).hexdigest(),
          'version': '0.26.0', 'install_exit': install.returncode, 'checks': checks,
          'required_assets_present': True, 'device_credentials_excluded': True,
          'experimental_session_not_shipped': True, 'model_calls': 0,
          'passed': all(row['exit'] == 0 for row in checks)}
(out/'result.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
print(json.dumps(record))
raise SystemExit(0 if record['passed'] else 1)
