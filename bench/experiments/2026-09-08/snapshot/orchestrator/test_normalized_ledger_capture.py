import json
from normalized_ledger_capture import capture_ledger


def test_failed_receipt_survives_without_unknown_contents(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    receipt = {'schema_version': 1, 'event': 'settled', 'request_id': 'req-1',
               'model': 'claude-opus-5', 'outcome': 'error',
               'error_code': 'provider_transport_error', 'duration_ms': 123,
               'usage': {'input_tokens': 10, 'output_tokens': 0,
                         'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 1,
                         'secret': 'sensitive-original'},
               'prompt': 'sensitive-original'}
    (source / '001.json').write_text(json.dumps(receipt), encoding='utf-8')
    target = tmp_path / 'capture.json'
    evidence = capture_ledger(source, target)
    row = evidence['rows'][0]
    assert row['receipt']['outcome'] == 'error'
    assert row['receipt']['error_code'] == 'provider_transport_error'
    assert row['receipt']['usage']['input_tokens'] == 10
    assert 'sensitive-original' not in target.read_text(encoding='utf-8')
    assert len(row['sha256']) == 64


def test_partial_and_bad_receipts_keep_hashes_without_leaking_values(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / '001.partial').write_text('private-broken-json', encoding='utf-8')
    (source / '002.json').write_text(json.dumps(
        {'event': 'private-value', 'duration_ms': True,
         'usage': {'input_tokens': False}, 'error_code': 'contains private prose'}))
    target = tmp_path / 'capture.json'
    result = capture_ledger(source, target)
    assert result['rows'][0]['capture_status'] == 'invalid_json'
    assert result['rows'][1]['receipt'] == {'usage': {}}
    assert 'private' not in target.read_text(encoding='utf-8')
