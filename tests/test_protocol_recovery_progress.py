"""A successful response restores a later reply's bounded format repair."""
from harness.cli import make_harness
from harness.providers import Completion, ToolCall
from test_loop import _contract_error, _RecordingMemory, _ScriptProvider


def setup(tmp_path, replies):
    (tmp_path/'first.txt').write_text('first independent result')
    (tmp_path/'second.txt').write_text('second independent result')
    harness = make_harness(str(tmp_path), provider='mock', project='protocol-progress', embed='hash')
    harness.max_turns = 0
    harness.max_retries = 0
    harness.memory = _RecordingMemory()
    harness.provider = _ScriptProvider(replies)
    return harness


def read(name):
    return Completion(tool_calls=[ToolCall('read-'+name, 'read_file', {'path': name+'.txt'})],
                      stop_reason='tool_use')


def test_independent_repaired_errors_do_not_end_a_progressing_run(tmp_path):
    harness = setup(tmp_path, [_contract_error(), read('first'), _contract_error(), read('second'),
                               Completion(text='Both files were inspected.')])
    events = []
    harness.emit = lambda kind, data: events.append((kind, data))
    result = harness.run('progress', 'Inspect both files.', consolidate=False)
    assert result.answer == 'Both files were inspected.' and not result.error
    assert result.model_calls == 5 and result.contract_repairs == 2
    assert sum(message['role'] == 'tool' for message in result.messages) == 2
    assert all('previous response could not be parsed' not in str(message) for message in result.messages)
    repairs = [data for kind, data in events if kind == 'format_repair']
    assert [data['attempt'] for data in repairs] == [1, 1]
    assert [data['total_repairs'] for data in repairs] == [1, 2]


def test_a_later_repair_still_cannot_exceed_the_shared_request_limit(tmp_path):
    harness = setup(tmp_path, [_contract_error(), read('first'), _contract_error(), read('second')])
    harness.max_model_calls = 3
    result = harness.run('capped', 'Inspect both files.', consolidate=False)
    assert harness.provider.calls == result.model_calls == 3
    assert result.contract_repairs == 1 and result.error.startswith('protocol:')


def test_transport_retry_does_not_restore_a_failed_format_repair(tmp_path):
    overloaded = Completion(stop_reason='error', error_status=529, error_detail='overloaded_error')
    harness = setup(tmp_path, [_contract_error(), overloaded, _contract_error(),
                               Completion(text='must not be reached')])
    harness.max_retries = 1
    harness.retry_base = 0
    result = harness.run('consecutive', 'Inspect both files.', consolidate=False)
    assert harness.provider.calls == result.model_calls == 3
    assert result.contract_repairs == 1 and result.error.startswith('protocol:')
