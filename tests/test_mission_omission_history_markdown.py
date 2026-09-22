"""The report's Markdown stays inside its bound and says when it is shortened.

Text truncation is a different fact from omission-history paging: the JSON rows
remain authoritative and complete for the page, so a cut Markdown body must
announce itself instead of listing a subset under "All N ... are listed."
"""
import re

import pytest

from harness.missionweb import MissionService, REPORT_MARKDOWN_LIMIT


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.setenv('COLLIE_STATE_DIR', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    service = MissionService(base=str(tmp_path/'mission'), state_dir=str(tmp_path),
                             provider='mock', model='mock')
    yield service
    service.close()


def seed(service, mission_id, count, summary_words=1, goal='Summarize the run'):
    """Record ``count`` real journalled omissions through the store API."""
    service.store.create(mission_id, goal)
    for index in range(count):
        row = {'id': 'skip-%03d' % index,
               'summary': 'Route-%03d ' % index + 'context ' * summary_words,
               'branch': 'route-%03d' % index,
               'reason': 'Optional route unavailable'}
        service.store.record_event(mission_id, 'intervention', 'optional_skipped',
                                   nonce=row['id'], payload=row)
    return service.report(mission_id)


def route_lines(markdown):
    return [line for line in markdown.splitlines() if line.startswith('- Route-')]


def test_long_summaries_shorten_the_text_and_never_claim_a_complete_listing(service):
    report = seed(service, 'long', 100, summary_words=110)

    # The JSON rows stay authoritative: every recorded omission is still there.
    assert report['skipped_steps_total'] == 100
    assert len(report['skipped_steps']) == 100
    assert report['skipped_steps_truncated'] is False   # nothing was paged away
    assert report['markdown_truncated'] is True         # only the text was cut

    markdown = report['markdown']
    assert len(markdown) <= REPORT_MARKDOWN_LIMIT
    assert 'All 100 recorded omissions are listed.' not in markdown
    assert 'recorded omissions are listed' not in markdown


def test_a_shortened_text_states_its_cut_up_front_and_counts_what_it_shows(service):
    report = seed(service, 'long', 100, summary_words=110)
    markdown = report['markdown']

    # Prominent: the banner sits directly under the report title.
    assert 'Shortened text.' in markdown[:1000]
    assert str(REPORT_MARKDOWN_LIMIT) in markdown[:1000]
    assert 'markdown_truncated' in markdown[:1000]

    shown = int(re.search(r'lists (\d+) of 100 recorded omissions',
                          markdown).group(1))
    listed = route_lines(markdown)
    assert shown == len(listed) < 100
    # The claimed count matches both the section heading and the closing note.
    assert 'spells out %d of those 100 entries' % shown in markdown
    assert '%d further omission lines are left out of this text' % (100 - shown) \
        in markdown
    assert '`skipped_steps` rows' in markdown
    # What survives is the newest contiguous prefix of the listed page.
    assert listed == ['- ' + row['summary'] + ': ' + row['reason']
                      for row in report['skipped_steps'][:shown]]


def test_a_shortened_text_keeps_its_closing_sections(service):
    service.store.create('tail', 'Summarize the run')
    for index in range(60):
        row = {'id': 'skip-%03d' % index,
               'summary': 'Route-%03d ' % index + 'context ' * 200,
               'branch': 'route-%03d' % index, 'reason': 'Optional route unavailable'}
        service.store.record_event('tail', 'intervention', 'optional_skipped',
                                   nonce=row['id'], payload=row)
    service.store.record_event('tail', 'followup', 'scheduled', nonce='wake-1',
                               payload={'summary': 'Recheck the optional routes'})
    report = service.report('tail')

    markdown = report['markdown']
    assert report['markdown_truncated'] is True
    assert len(markdown) <= REPORT_MARKDOWN_LIMIT
    assert '## Recent activity' in markdown
    assert 'Recheck the optional routes' in markdown
    assert markdown.index('## Optional steps skipped') < markdown.index('## Recent activity')


def test_a_small_report_lists_every_omission_and_is_not_marked_shortened(service):
    report = seed(service, 'small', 3)

    assert report['markdown_truncated'] is False
    assert report['skipped_steps_truncated'] is False
    markdown = report['markdown']
    assert len(markdown) <= REPORT_MARKDOWN_LIMIT
    assert 'All 3 recorded omissions are listed.' in markdown
    assert 'Shortened text.' not in markdown
    assert len(route_lines(markdown)) == 3
    for row in report['skipped_steps']:
        assert row['summary'] in markdown


def test_a_report_without_omissions_has_no_omission_section_or_banner(service):
    service.store.create('none', 'Summarize the run')
    report = service.report('none')

    assert report['skipped_steps'] == []
    assert report['skipped_steps_total'] == 0
    assert report['markdown_truncated'] is False
    assert '## Optional steps skipped' not in report['markdown']
    assert 'Shortened text.' not in report['markdown']


def test_page_truncation_and_text_truncation_are_reported_separately(service):
    report = seed(service, 'paged', 260)

    # 260 short omissions: the history pages, the text still fits whole.
    assert report['skipped_steps_total'] == 260
    assert report['skipped_steps_shown'] == 200
    assert report['skipped_steps_truncated'] is True
    assert report['markdown_truncated'] is False

    markdown = report['markdown']
    assert len(markdown) <= REPORT_MARKDOWN_LIMIT
    assert 'Showing the 200 most recent of 260 recorded omissions' in markdown
    assert 'recorded omissions are listed' not in markdown
    assert 'Shortened text.' not in markdown
    assert len(route_lines(markdown)) == 200
    assert service.omissions('paged', limit=60)['total'] == 260
