"""Build and verify Collie 0.26 locally, with isolated installation and mock GUI state."""
import hashlib
import argparse
import json
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent
repo = Path('C:/workspace/collie')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--name', default='package026-final')
name = parser.parse_args().name
assert name and Path(name).name == name
source = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
out = root/name
out.mkdir(exist_ok=False)
with (out/'build.log').open('w', encoding='utf-8') as log:
    subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-index',
                    '--no-build-isolation', '--wheel-dir', str(out), str(repo)],
                   cwd=repo, stdout=log, stderr=subprocess.STDOUT, check=True)
wheels = list(out.glob('*.whl'))
assert len(wheels) == 1
wheel = wheels[0]
(out/'build.json').write_text(json.dumps({'source_commit': source, 'wheel': wheel.name,
    'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(), 'network': False}, indent=2), encoding='utf-8')
subprocess.run([sys.executable, str(root/'verify_product_wheel.py'), '--wheel', str(wheel),
    '--output', str(root/(name+'-verification')), '--source-commit', source], check=True)
with (out/'gui.log').open('w', encoding='utf-8') as log:
    subprocess.run([sys.executable, str(root/'wheel_gui_check.py'),
        '--output', str(root/(name+'-gui')),
        '--installed', str(root/(name+'-verification')/'installed'),
        '--tests-repo', str(repo), '--source-commit', source],
        stdout=log, stderr=subprocess.STDOUT, check=True)
print(json.dumps({'source_commit': source, 'wheel': str(wheel), 'passed': True}))
