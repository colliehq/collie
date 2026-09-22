"""Retain prompt-free request receipts even when evaluator validation fails.

Unknown or malformed values are represented by source hashes, never copied into
the reviewed evidence. This capture does not change validity or grading policy.
"""
import hashlib
import json
import re
from pathlib import Path

STRINGS = {
    'event': r'(?:reserved|settled|budget_exhausted)',
    'request_id': r'[A-Za-z0-9_.-]{1,80}',
    'created_at_utc': r'[0-9TZ:+. -]{1,40}',
    'model': r'claude-opus-5',
    'request_sha256': r'[0-9a-f]{64}',
    'prompt_sha256': r'[0-9a-f]{64}',
    'outcome': r'(?:completed|error|cancelled|timeout)',
    'error_code': r'[a-z][a-z0-9_]{0,79}',
}
NUMBERS = {'schema_version', 'request_bytes', 'duration_ms', 'max_requests'}
USAGE = {'input_tokens', 'output_tokens', 'cache_read_input_tokens',
         'cache_creation_input_tokens'}


def capture_ledger(directory, destination):
    directory, destination = Path(directory), Path(destination)
    evidence = {'schema_version': 1, 'present': directory.is_dir(), 'rows': []}
    if directory.is_dir():
        entries = sorted(directory.iterdir())
        evidence['entry_count'] = len(entries)
        evidence['truncated'] = len(entries) > 10000
        for index, path in enumerate(entries[:10000]):
            item = {'index': index}
            evidence['rows'].append(item)
            if not path.is_file() or path.is_symlink():
                item['capture_status'] = 'non_regular_file'
                continue
            raw = path.read_bytes()
            item.update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
            if len(raw) > 32768:
                item['capture_status'] = 'oversized'
                continue
            try:
                row = json.loads(raw)
            except (ValueError, UnicodeError):
                item['capture_status'] = 'invalid_json'
                continue
            if not isinstance(row, dict):
                item['capture_status'] = 'invalid_shape'
                continue
            safe = {}
            for key, pattern in STRINGS.items():
                value = row.get(key)
                if isinstance(value, str) and re.fullmatch(pattern, value):
                    safe[key] = value
            for key in NUMBERS:
                value = row.get(key)
                if type(value) is int and value >= 0:
                    safe[key] = value
            if isinstance(row.get('usage'), dict):
                safe['usage'] = {key: value for key, value in row['usage'].items()
                                 if key in USAGE and type(value) is int and value >= 0}
            item.update(capture_status='filtered', receipt=safe)
    destination.write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    return evidence
