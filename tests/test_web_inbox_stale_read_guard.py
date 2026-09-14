"""Focused cases around the stale-listing guard: late failures, notices and re-reads."""
import time

import pytest
from playwright.sync_api import expect

from test_web_ui_run_status import _Fixture, _hold_queue, ui, server, browser
from test_web_inbox_poll_race import HeldListing, _queue, _record


class _HeldRead(HeldListing):
    """The same real held listing, with the option of failing its late delivery."""

    def fail(self):
        self.held.abort('connectionfailed')
        self.page.wait_for_timeout(150)
        self.page.evaluate('() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))')


def _edit_to(ui, text):
    panel = ui.page.locator('#taskQueue')
    panel.get_by_role('button', name='Edit', exact=True).click()
    ui.page.get_by_label('Edit pending request').fill(text)
    panel.get_by_role('button', name='Save', exact=True).click()


def _notice(ui):
    return ui.page.locator('#taskQueue').inner_text()


def test_a_late_failing_read_does_not_report_an_error_over_a_confirmed_edit(ui):
    _hold_queue(ui)
    _queue(ui, 'Original pending request')
    held = _HeldRead(ui.page)
    held.wait()
    _edit_to(ui, 'Updated pending request')
    expect(ui.page.locator('.task-queue-text')).to_have_text('Updated pending request')
    _record(ui.page)
    held.fail()
    states = ui.page.evaluate('window.__queuePollStates')
    assert all(state == ['Updated pending request'] for state in states), states
    assert ui.page.locator('#taskQueueList .task-queue-text').all_text_contents() == ['Updated pending request']
    # The failure belongs to a read that was already out of date; it is not the person's problem.
    assert 'Could not load pending requests' not in _notice(ui)
    assert len(_Fixture.stream_requests) == 1 and not _Fixture.queue_starts


def test_a_failed_mutation_keeps_its_notice_and_its_row_against_a_late_listing(ui):
    _hold_queue(ui)
    _queue(ui, 'Original pending request')
    # The listing captured below carries a server notice of its own, which must not land on
    # top of the newer message from the save that was refused.
    _Fixture.queue_status_extra = {'queue_error': {'error': 'stale listing notice'}}
    held = HeldListing(ui.page)
    held.wait()
    assert held.snapshot['queue_error']['error'] == 'stale listing notice'
    _Fixture.queue_status_extra = {}
    ui.page.route('**/api/task-inbox/edit*',
                  lambda route: route.fulfill(status=503, json={'error': 'edit rejected by fixture'}))
    _edit_to(ui, 'Never saved text')
    expect(ui.page.locator('#taskQueue')).to_contain_text('edit rejected by fixture')
    _record(ui.page)
    held.release()
    states = ui.page.evaluate('window.__queuePollStates')
    assert all(state == ['Original pending request'] for state in states), states
    assert 'edit rejected by fixture' in _notice(ui)
    assert 'stale listing notice' not in _notice(ui)
    assert next(iter(_Fixture.queue_entries.values()))['text'] == 'Original pending request'
    assert len(_Fixture.stream_requests) == 1 and not _Fixture.queue_starts


def test_dropping_a_stale_read_is_followed_by_a_fresh_read_of_current_data(ui):
    _hold_queue(ui)
    _queue(ui, 'Original pending request')
    reads = []
    ui.page.on('request', lambda request: request.url.find('/api/task-inbox?') > 0
               and request.method == 'GET' and reads.append(time.monotonic()))
    held = HeldListing(ui.page)
    held.wait()
    _edit_to(ui, 'Updated pending request')
    expect(ui.page.locator('.task-queue-text')).to_have_text('Updated pending request')
    # Another window accepts a request while the old read is still in flight. Only a genuinely
    # current read can show it, so its arrival is evidence the client re-read rather than
    # kept the answer it already had.
    _Fixture.queue_entries['other-window'] = {
        'session': 's-read', 'id': 'other-window', 'text': 'Queued from another window',
        'mode': 'follow_up', 'seq': 9, 'state': 'pending', 'digest': 'v1', 'metadata': {}}
    posts, last = len(_Fixture.queue_posts), reads[-1]
    released = time.monotonic()
    held.release()
    expect(ui.page.locator('#taskQueueList .task-queue-text')).to_have_text(
        ['Updated pending request', 'Queued from another window'], timeout=2000)
    followups = [at for at in reads if at > released]
    assert followups, 'no read followed the dropped one'
    # The 2.5s poll cannot account for this: the first follow-up read lands immediately after
    # the release and well before the next tick could reach the network.
    assert followups[0] - released < 0.4 and followups[0] - last < 2.0
    assert len(_Fixture.queue_posts) == posts, 'nothing was re-sent'
    assert len(_Fixture.stream_requests) == 1 and not _Fixture.queue_starts
