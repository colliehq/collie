"""Shared fixtures and the standalone runner for the split-out core suites.

These files run two ways — under pytest, and directly (`python tests/test_loop.py`) so a machine
with no pytest can still check itself. The runner exists for the second path.
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _ctx(cwd):
    """The minimal ToolCtx a tool actually reads. Used by 22 tests across three of the split
    files, which is why it lives here rather than in any one of them."""
    return types.SimpleNamespace(cwd=cwd, project="t")


class _Skip(Exception):
    """Raise to skip a test that a given OS genuinely cannot exercise (e.g. creating a
    symlink without privilege on Windows). Reported as SKIP — visible, not a silent pass
    and not a failure — so the suite stays green cross-platform without hiding coverage."""


class _ScriptProvider:
    """Drives the loop with a fixed list of Completions (or callables(messages)->Completion).
    name != 'mock' so memory-consolidation is exercised; records complete() call count."""
    reports_cache = False

    def __init__(self, script, name="deepseek", model="deepseek-chat"):
        self.name = name
        self.model = model
        self.max_tokens = 4096
        self._script = list(script)
        self._i = 0
        self.calls = 0

    def complete(self, system, messages, tool_schemas, on_text=None):
        self.calls += 1
        item = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return item(messages) if callable(item) else item


class _RecordingMemory:
    def __init__(self):
        self.remembered = []

    def remember(self, text, keys=None, project=None):
        self.remembered.append(text)

    def set_block(self, *a, **k):
        pass

    def close(self):
        pass


def run_module(ns, label):
    """Run every test_* in `ns`. Call this from the LAST line of the file.

    test_core.py used to place its `if __name__ == "__main__"` in the middle, so the 14 tests
    defined after it — every checkpoint test among them — were never run by the standalone path.
    They passed under pytest and were invisible here, which is the worst of both.
    """
    tests = [(n, f) for n, f in sorted(ns.items()) if n.startswith("test_") and callable(f)]
    passed, failed, skipped = 0, [], []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print("  PASS %s" % name)
        except _Skip as s:
            skipped.append(name)
            print("  SKIP %s :: %s" % (name, s))
        except Exception as e:
            failed.append(name)
            print("  FAIL %s :: %s" % (name, e))
            if os.environ.get("V"):
                import traceback
                traceback.print_exc()
    tail = "" if not failed else " FAILS: " + ", ".join(failed)
    tail += "" if not skipped else " SKIPPED: " + ", ".join(skipped)
    print("\n== %s: %d/%d passed ==%s" % (label, passed, len(tests), tail))
    return 1 if failed else 0
