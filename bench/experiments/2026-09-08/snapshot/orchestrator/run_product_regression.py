import datetime as dt
import argparse
import json
import os
import re
import hashlib
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parent
REPO = Path(r'C:\workspace\collie')
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, default=ROOT/'product026-regression')
OUT = parser.parse_args().output.resolve()
OUT.mkdir(exist_ok=False)
env = {k: v for k, v in os.environ.items() if not k.startswith('COLLIE_')}
state = OUT/'state'
state.mkdir()
env.update(COLLIE_STATE_DIR=str(state), COLLIE_DATA_DIR=str(state/'data'),
           COLLIE_SETTINGS_PATH=str(state/'settings.json'), COLLIE_SESSIONS_DIR=str(state/'sessions'),
           COLLIE_MCP_CONFIG=str(state/'mcp.json'), COLLIE_EMBED='hash', COLLIE_LANG='en',
           COLLIE_PROVIDER='mock', COLLIE_RUNNER='collie', COLLIE_SKIP_NET='1',
           COLLIE_BROWSER_BRIDGE='0', COLLIE_REMOTE='off', PYTHONIOENCODING='utf-8')
(state/'settings.json').write_text('{}')
(state/'mcp.json').write_text('{"mcpServers":{}}')
commit = subprocess.check_output(['git','-C',str(REPO),'rev-parse','HEAD'], text=True).strip()
started = dt.datetime.now(dt.timezone.utc).isoformat()
t = time.monotonic()
with (OUT/'run-all.log').open('w', encoding='utf-8') as log:
    result = subprocess.run([r'C:\Program Files\Git\bin\bash.exe', 'tests/run_all.sh'],
                            cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT,
                            creationflags=subprocess.CREATE_NO_WINDOW)
record = {'source_commit': commit, 'exit': result.returncode, 'started_at': started,
          'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(),
          'seconds': round(time.monotonic()-t,3), 'network_skipped': True}
log_text = (OUT/'run-all.log').read_text(encoding='utf-8')
record['log_sha256'] = hashlib.sha256((OUT/'run-all.log').read_bytes()).hexdigest()
record['suite_summaries'] = [line.strip() for line in log_text.splitlines()
    if re.search(r'^== .*passed ==|^\d+ passed.* in |^\s+\d+/\d+ tasks passed', line)]
(OUT/'result.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
print(json.dumps(record))
raise SystemExit(result.returncode)
