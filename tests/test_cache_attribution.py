"""A cache miss caused by the system block changing is named, not "unexplained"."""
import json
import os

from harness import compaction
from harness.providers import Completion, ToolCall, Usage


class _HonestCache:
    """Reports as cached exactly the character prefix shared with the previous request."""
    name = "honest-cache"
    model = "deepseek-chat"
    reports_cache = True
    max_tokens = 4096

    def __init__(self):
        self.n = 0
        self._prev = ""

    def complete(self, system, messages, tool_schemas, on_text=None):
        self.n += 1
        request = system + json.dumps(messages, default=str)
        common = len(os.path.commonprefix([request, self._prev]))
        self._prev = request
        full = compaction.estimate_text(request)
        read = compaction.estimate_text(request[:common])
        usage = Usage(input_tokens=max(0, full - read), output_tokens=5, cache_read=read)
        if self.n < 5:
            return Completion(text="", stop_reason="tool_use", usage=usage,
                              tool_calls=[ToolCall("t%d" % self.n, "read_file", {"path": "big.txt"})])
        return Completion(text="done", stop_reason="end_turn", usage=usage)


def test_a_changed_system_block_is_named_as_the_cause(tmp_path, monkeypatch):
    from harness.cli import make_harness
    monkeypatch.chdir(tmp_path)
    (tmp_path / "big.txt").write_text("\n".join("row %d of a large file" % i for i in range(3000)))
    h = make_harness(str(tmp_path), provider="mock", project="cache-cause", embed="hash")
    h.provider = _HonestCache()
    h.max_turns = 8
    real = h.composer.build
    builds = [0]

    def build(*a, **k):
        system, msgs, meta = real(*a, **k)
        builds[0] += 1
        if builds[0] >= 4:                     # as if the run had updated a core memory block
            system += "\n\nCORE MEMORY:\n- [notes] the build now uses ninja"
        return system, msgs, meta

    h.composer.build = build
    seen = []
    h.emit = lambda kind, d: seen.append((kind, d))
    res = h.run("cache-cause", "read big.txt a few times")
    assert res.answer == "done", (res.error, res.answer)
    causes = [d.get("cause") for kind, d in seen if kind == "cache_miss"]
    assert any(c and "system" in c for c in causes), causes
    assert not any(c == "unexplained" for c in causes), causes
