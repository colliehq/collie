"""Output-truncation recovery must not change the next task's provider settings."""
import pytest

from harness.providers import Completion
from test_request_accounting import _harness


@pytest.mark.parametrize('second_fails', [False, True])
def test_output_ceiling_increases_only_inside_the_truncated_run(tmp_path, second_fails):
    class Provider:
        name, model, reports_cache = 'mock', 'mock', False
        max_tokens = 1024
        seen = []

        def complete(self, *args, **kwargs):
            self.seen.append(self.max_tokens)
            if len(self.seen) == 1:
                return Completion(text='partial', stop_reason='length')
            if len(self.seen) == 2 and second_fails:
                raise RuntimeError('transport failed after truncation')
            return Completion(text='done')

    provider = Provider()
    harness = _harness(str(tmp_path), 'generation-state', provider, turns=5)
    harness.max_retries = 0
    harness.run('first', 'explain the fixture', consolidate=False)
    assert provider.seen[:2] == [1024, 2048]
    assert provider.max_tokens == 1024
    harness.run('second', 'answer the next question', consolidate=False)
    assert provider.seen[-1] == 1024


def test_a_provider_with_a_readonly_unchanged_limit_still_completes(tmp_path):
    class Provider:
        name, model, reports_cache = 'mock', 'mock', False
        @property
        def max_tokens(self):
            return 1024
        def complete(self, *args, **kwargs):
            return Completion(text='done')
    harness = _harness(str(tmp_path), 'readonly-limit', Provider(), turns=2)
    assert harness.run('readonly-limit', 'answer', consolidate=False).answer == 'done'


def test_truncation_never_reduces_a_large_explicit_generation_limit(tmp_path):
    class Provider:
        name, model, reports_cache = 'mock', 'mock', False
        max_tokens = 65536
        seen = []
        def complete(self, *args, **kwargs):
            self.seen.append(self.max_tokens)
            return Completion(text='partial' if len(self.seen) == 1 else 'done',
                              stop_reason='length' if len(self.seen) == 1 else 'end_turn')
    provider = Provider()
    harness = _harness(str(tmp_path), 'large-limit', provider, turns=3)
    harness.run('large-limit', 'answer', consolidate=False)
    assert provider.seen == [65536, 65536]
    assert provider.max_tokens == 65536
