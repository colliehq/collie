"""Run the existing GUI suite against the isolated installed wheel."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import socket
import time

ROOT = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output', type=Path, default=ROOT/'package026-presecond-gui')
parser.add_argument('--installed', type=Path, default=ROOT/'package026-presecond-verification-v2/installed')
parser.add_argument('--tests-repo', type=Path, default=ROOT/'product026-session2-pin')
parser.add_argument('--source-commit', default='5eae2cff216ac551b4261c5c3aa2c942eb381417')
args = parser.parse_args()
out = args.output.resolve()
out.mkdir(exist_ok=False)
for key in list(os.environ):
    if key.startswith(('COLLIE_', 'ANTHROPIC_')):
        os.environ.pop(key)
os.environ.update(COLLIE_EMBED='hash', COLLIE_LANG='en', COLLIE_SKIP_NET='1',
                  COLLIE_BROWSER_BRIDGE='0', COLLIE_REMOTE='off',
                  COLLIE_DATA_DIR=str(out/'data'), PYTHONIOENCODING='utf-8')
spec = importlib.util.spec_from_file_location('installed_gui_suite',
    args.tests_repo/'tests/gui_test.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
module.ROOT = str(args.installed.resolve())
with socket.socket() as server:
    server.bind(('127.0.0.1', 0))
    module.PORT = server.getsockname()[1]
started = time.monotonic()
code = module.main()
record = {'source_commit': args.source_commit,
          'checks': [{'name': name, 'passed': passed} for name, passed in module.results],
          'exit': code, 'seconds': round(time.monotonic()-started, 3),
          'installed_wheel': any(args.installed.glob('collie_harness-*.dist-info')),
          'model_calls': 0,
          'method': 'Product GUI checks; override only module ROOT to the supplied runtime and choose an unused port.'}
(out/'result.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
raise SystemExit(code)
