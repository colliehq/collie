"""Validate content-free session ownership receipts independently of patch grading."""

COUNTERS = ('sessions_opened', 'session_turns', 'stateless_turns', 'longest_session_turns',
            'unresolved_cleanups', 'cleanup_retries_confirmed')
FLAGS = ('session_enabled', 'structured_output', 'had_session', 'close_reported', 'session_retired')
OWNERSHIP = ('unresolved_workers', 'active_registrations')


def counter(value):
    return type(value) is int and 0 <= value < 2 ** 53


def summarize(value, expected_enabled):
    failure = {'valid': False, 'clean': False, 'failure': 'session_receipt_invalid'}
    if not isinstance(value, list) or not value or len(value) > 64:
        return failure
    rows = []
    for raw in value:
        if not isinstance(raw, dict) or set(raw) != set(FLAGS + OWNERSHIP + ('evidence',)):
            return failure
        if not all(type(raw.get(key)) is bool for key in FLAGS):
            return failure
        if raw['session_enabled'] is not expected_enabled:
            return failure
        if not all(counter(raw.get(key)) for key in OWNERSHIP):
            return failure
        evidence = raw.get('evidence')
        if not isinstance(evidence, dict) or set(evidence) != set(COUNTERS + ('resets', 'fallbacks')):
            return failure
        if not all(counter(evidence.get(key)) for key in COUNTERS):
            return failure
        for key in ('resets', 'fallbacks'):
            reasons = evidence.get(key)
            if not isinstance(reasons, dict) or not all(
                isinstance(name, str) and len(name) < 160 and counter(count)
                for name, count in reasons.items()):
                return failure
        rows.append(raw)
    extinct = all(row['session_retired'] and not row['unresolved_workers']
                  and not row['active_registrations'] for row in rows)
    totals = {key: sum(row['evidence'][key] for row in rows) for key in COUNTERS
              if key != 'longest_session_turns'}
    totals['longest_session_turns'] = max(row['evidence']['longest_session_turns'] for row in rows)
    error = (None if extinct else 'session_cleanup_unresolved')
    if not error and expected_enabled and not totals['session_turns']:
        error = 'session_not_exercised'
    return {'valid': True, 'clean': error is None, 'failure': error,
            'ownership_extinct': extinct, 'totals': totals, 'providers': rows}
