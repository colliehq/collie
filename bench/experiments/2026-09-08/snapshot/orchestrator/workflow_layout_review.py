"""Inspect shipped UI resources with synthetic unconfirmed-send states, no model calls."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer

from playwright.sync_api import sync_playwright, expect

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--repo', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
args.output.mkdir(exist_ok=False)
sys.path.insert(0, str(args.repo/'tests'))
import test_web_ui_run_status as fixture
from test_web_busy_send_ui import ASK, NEWER, attach, open_read_thread, send, retained_rows

httpd = ThreadingHTTPServer(('127.0.0.1', 0), fixture._Fixture)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
base = 'http://127.0.0.1:%d' % httpd.server_address[1]
results = []
try:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, lang in [(1280, 'en'), (390, 'zh')]:
            fixture._Fixture.lang = lang
            holder = fixture.ui.__wrapped__(base, browser)
            ui = next(holder)
            page = ui.page
            try:
                fixture._Fixture.busy_delay = 1.0
                fixture._Fixture.queue_fail_all = True
                open_read_thread(page)
                page.set_viewport_size({'width': width, 'height': 900 if width == 1280 else 844})
                attach(page)
                send(page, ASK if lang == 'en' else 'Busy fixture — 请检查安装流程，并保留这张附件。')
                page.wait_for_timeout(150)
                page.fill('#input', NEWER if lang == 'en' else '下一条草稿：先检查 Windows 安装。')
                expect(retained_rows(page)).to_have_count(1)
                page.locator('#taskQueue').scroll_into_view_if_needed()
                state = page.evaluate('''() => ({
                  width: document.documentElement.clientWidth,
                  overflow: document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
                  rows: Array.from(document.querySelectorAll('.task-queue-row.retained')).map(row => ({
                    text: row.innerText, overflow: row.scrollWidth > row.clientWidth + 1,
                    bounds: row.getBoundingClientRect().toJSON()})),
                  composer: document.getElementById('input').value
                })''')
                shot = args.output / f'retained-{width}-{lang}.png'
                page.screenshot(path=str(shot), full_page=True)
                state.update(lang=lang, errors=list(ui.errors), screenshot=shot.name,
                             screenshot_sha256=hashlib.sha256(shot.read_bytes()).hexdigest())
                assert not state['overflow'] and not any(row['overflow'] for row in state['rows'])
                assert not ui.errors
                results.append(state)
            finally:
                try:
                    next(holder)
                except StopIteration:
                    pass
        browser.close()
finally:
    httpd.shutdown()
record = {'source_commit': subprocess.check_output(['git', '-C', str(args.repo), 'rev-parse', 'HEAD'], text=True).strip(),
          'ui_sha256': hashlib.sha256((args.repo/'harness/webui/index.html').read_bytes()).hexdigest(),
          'method': 'Real UI resources, synthetic local server refusal; isolated Playwright browser, no model calls.',
          'checks': results, 'passed': True, 'model_calls': 0}
(args.output/'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'passed': True, 'views': len(results)}))
