"""Run a named local regression in isolated mock-only product state."""
import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument('--repo', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('tests', nargs='+')
a = p.parse_args()
repo, out = a.repo.resolve(), a.output.resolve()
out.mkdir(parents=True, exist_ok=False)
state = out/'state'
state.mkdir()
(state/'settings.json').write_text('{"PROVIDER":"mock","MODEL":"mock"}')
(state/'mcp.json').write_text('{"mcpServers":{}}')
env = {k:v for k,v in os.environ.items() if not k.startswith('COLLIE_')}
env.update(COLLIE_STATE_DIR=str(state), COLLIE_DATA_DIR=str(state/'data'),
    COLLIE_SETTINGS_PATH=str(state/'settings.json'), COLLIE_SESSIONS_DIR=str(state/'sessions'),
    COLLIE_MCP_CONFIG=str(state/'mcp.json'), COLLIE_EMBED='hash', COLLIE_LANG='en',
    COLLIE_PROVIDER='mock', COLLIE_MODEL='mock', COLLIE_RUNNER='collie', COLLIE_SKIP_NET='1',
    COLLIE_BROWSER_BRIDGE='0', COLLIE_REMOTE='off', PYTHONIOENCODING='utf-8')
command = [sys.executable, '-m', 'pytest', '-q', *a.tests]
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repo, text=True).strip()
diff = subprocess.check_output(['git', 'diff', '--binary'], cwd=repo)
start = time.monotonic()
with (out/'pytest.log').open('w', encoding='utf-8') as log:
    process = subprocess.run(command, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW)
record = {'source_commit':commit, 'tracked_diff_sha256':hashlib.sha256(diff).hexdigest(),
          'tests':a.tests, 'exit':process.returncode, 'seconds':round(time.monotonic()-start,3),
          'finished_at':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'model_calls':0}
(out/'result.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps(record))
raise SystemExit(process.returncode)
