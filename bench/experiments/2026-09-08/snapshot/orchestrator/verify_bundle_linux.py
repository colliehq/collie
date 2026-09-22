"""Verify the frozen 166-candidate stage under Linux without network or model credentials."""
import datetime
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import time

root = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--name', default='evidence-linux-validation')
name = parser.parse_args().name
assert name and Path(name).name == name
out = root/name
out.mkdir(exist_ok=False)
stage = root/'reviewed-evidence-stage4'
image = subprocess.check_output(['docker', 'image', 'inspect', 'collie-normalized-harness:local',
    '--format', '{{.Id}}'], text=True).strip()
args = ['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'python3',
    '-e', 'PYTHONDONTWRITEBYTECODE=1', '-e', 'GIT_CONFIG_COUNT=1',
    '-e', 'GIT_CONFIG_KEY_0=safe.directory', '-e', 'GIT_CONFIG_VALUE_0=/source',
    '-v', str(stage)+':/evidence:ro',
    '-v', str(root/'verify_session_evidence.py')+':/verify.py:ro',
    '-v', 'C:/workspace/collie:/source:ro', image,
    '/verify.py', '--bundle', '/evidence', '--source-repo', '/source', '--regrade-all']
started = time.monotonic()
with (out/'verify.log').open('w', encoding='utf-8') as log:
    result = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT)
text = (out/'verify.log').read_text(encoding='utf-8')
checks = json.loads(text.splitlines()[-1]) if result.returncode == 0 else None
record = {'exit': result.returncode, 'seconds': round(time.monotonic()-started, 3),
    'at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'image': image,
    'source_stage': stage.name, 'source_manifest_sha256': hashlib.sha256((stage/'SHA256SUMS.json').read_bytes()).hexdigest(),
    'results_sha256': hashlib.sha256((stage/'results.json').read_bytes()).hexdigest(),
    'verifier_sha256': hashlib.sha256((root/'verify_session_evidence.py').read_bytes()).hexdigest(),
    'checks': checks, 'network_disabled': True, 'model_calls': 0,
    'claim': 'Cross-platform check of the unchanged candidate set and source patches. Later product-validation files are not in this older stage.'}
(out/'result.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
print(json.dumps(record))
raise SystemExit(result.returncode)
