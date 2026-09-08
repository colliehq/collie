"""Wait for the registered window, then run its independently gated first phase."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import time

root = Path(__file__).resolve().parent
plan = json.loads((root/'second-window-plan.json').read_text(encoding='utf-8'))
target = dt.datetime.fromisoformat(plan['not_before_utc'].replace('Z', '+00:00'))
print(json.dumps({'event': 'waiting_for_second_window', 'not_before': target.isoformat()}), flush=True)
while True:
    remaining = (target-dt.datetime.now(dt.timezone.utc)).total_seconds()
    if remaining <= 0:
        break
    time.sleep(min(30, remaining))
raise SystemExit(subprocess.call([sys.executable, str(root/'run_second_window.py'),
                                 '--phase', 'a', '--i-will-spend'], cwd=root))
