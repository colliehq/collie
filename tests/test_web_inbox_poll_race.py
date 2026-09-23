"""A completed queue mutation must survive an older HTTP poll arriving late."""
import json
import time

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import _Fixture, _hold_queue, ui, server, browser


class HeldListing:
    def __init__(self, page):
        self.page = page
        self.held = None
        self.snapshot = None
        self.request = None
        self.finished = False
        self.hold = True
        page.on('requestfinished', self._finished)
        page.route('**/api/task-inbox?*', self._route)

    def _finished(self, request):
        if request is self.request:
            self.finished = True

    def _route(self, route):
        if route.request.method != 'GET' or not self.hold:
            route.continue_()
            return
        self.hold = False
        # The actual staged HTTP fixture builds this listing before the mutation.
        # Freeze its serialized value, then delay transport delivery to the page.
        response = route.fetch()
        self.snapshot = response.json()
        self.request = route.request
        self.held = route

    def wait(self):
        until = time.monotonic() + 6
        while self.held is None and time.monotonic() < until:
            self.page.wait_for_timeout(25)
        assert self.held is not None, 'No real background inbox poll reached the fixture'

    def release(self):
        self.held.fulfill(json=self.snapshot)
        until = time.monotonic() + 5
        while not self.finished and time.monotonic() < until:
            self.page.wait_for_timeout(20)
        assert self.finished, 'The held response never finished delivery'
        # Observe two actual browser paint boundaries after network delivery. The
        # MutationObserver below also catches a stale state painted only briefly.
        self.page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')


def _queue(ui, text):
    ui.page.fill('#input', text)
    ui.page.press('#input', 'Enter')
    ui.page.wait_for_function("() => document.getElementById('input').value === ''")
    expect(ui.page.locator('.task-queue-text').filter(has_text=text)).to_have_count(1)


# What each row showed at a moment: {'text': ...} for a settled row, {'editing': ...} for a row
# open in its inline editor. A row being edited has no .task-queue-text at all — its words live in
# the textarea as an unsaved draft — so reading only .task-queue-text reports an open editor and a
# row that vanished as the same empty list. Rows are read whole so the two cannot be confused.
def _row(text):
    return {'text': text}


def _editing(text):
    return {'editing': text}


def _record(page):
    page.evaluate('''() => {
      window.__queuePollStates = [];
      window.__queueReadRows = () => Array.from(document.querySelectorAll('#taskQueueList .task-queue-row')).map(row => {
        const editor = row.querySelector('textarea');
        if (editor) return {editing: editor.value};
        const text = row.querySelector('.task-queue-text');
        return {text: text ? text.textContent : null};
      });
      if (window.__queuePollObserver) window.__queuePollObserver.disconnect();
      window.__queuePollObserver = new MutationObserver(() => {
        window.__queuePollStates.push(window.__queueReadRows());
      });
      window.__queuePollObserver.observe(document.getElementById('taskQueueList'), {subtree:true, childList:true, characterData:true});
    }''')


def _rows_now(page):
    return page.evaluate('window.__queueReadRows()')


@pytest.mark.parametrize('operation', ['edit', 'remove', 'accept'])
def test_late_poll_never_undoes_a_confirmed_queue_change(ui, operation):
    _hold_queue(ui)
    _queue(ui, 'Original pending request')
    held = HeldListing(ui.page)
    held.wait()
    assert [row['text'] for row in held.snapshot['entries']] == ['Original pending request']
    panel = ui.page.locator('#taskQueue')
    if operation == 'edit':
        panel.get_by_role('button', name='Edit', exact=True).click()
        ui.page.get_by_label('Edit pending request').fill('Updated pending request')
        panel.get_by_role('button', name='Save', exact=True).click()
        expect(ui.page.locator('.task-queue-text')).to_have_text('Updated pending request')
        assert next(iter(_Fixture.queue_entries.values()))['text'] == 'Updated pending request'
        expected = ['Updated pending request']
    elif operation == 'remove':
        panel.get_by_role('button', name='Remove', exact=True).click()
        expect(ui.page.locator('.task-queue-row')).to_have_count(0)
        assert next(iter(_Fixture.queue_entries.values()))['state'] == 'canceled'
        expected = []
    else:
        _queue(ui, 'Newly accepted request')
        expect(ui.page.locator('.task-queue-row')).to_have_count(2)
        assert len(_Fixture.queue_entries) == 2
        expected = ['Original pending request', 'Newly accepted request']
    _record(ui.page)
    held.release()
    states = ui.page.evaluate('window.__queuePollStates')
    actual = ui.page.locator('#taskQueueList .task-queue-text').all_text_contents()
    rows = [_row(text) for text in expected]
    assert all(state == rows for state in states), 'A confirmed change was rolled back by a stale listing: ' + json.dumps(states)
    assert actual == expected
    assert len(_Fixture.stream_requests) == 1 and not _Fixture.queue_starts
