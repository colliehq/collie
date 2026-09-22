"""A Pack stop reaches the candidate's actual objective check."""
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from harness import catalog, cli, pack, scratch


def test_stop_during_objective_check_cannot_apply_a_candidate(monkeypatch, tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    marker = tmp_path / "checking"
    late = tmp_path / "late"
    (workspace / "check.py").write_text(
        "from pathlib import Path\nimport time\n"
        "Path(%r).write_text('started')\n"
        "print('candidate check started', flush=True)\n"
        "time.sleep(3)\nPath(%r).write_text('not stopped')\n" % (str(marker), str(late)),
        encoding="utf-8")
    seen = []

    class Candidate:
        memory = recorder = SimpleNamespace(close=lambda: None)

        def run(self, *args, **kwargs):
            seen.append(args[0])
            return SimpleNamespace(answer="candidate ready", verified=True, turns=1,
                                   error="", cost_usd=0)

    monkeypatch.setattr(cli, "make_harness", lambda *a, **kw: Candidate())
    monkeypatch.setattr(cli, "configure_run_options", lambda *a, **kw: None)
    monkeypatch.setattr(scratch, "isolate_harness", lambda *a, **kw: None)
    monkeypatch.setattr(catalog, "preflight", lambda *a: [])
    stop = threading.Event()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pack.run_pack, "implement", str(workspace), n=2,
                             check="python check.py", apply=True, cancel=stop.is_set)
        deadline = time.monotonic() + 15
        while not marker.exists() and time.monotonic() < deadline and not future.done():
            time.sleep(.02)
        assert marker.exists(), future.result() if future.done() else "check never started"
        started = time.monotonic()
        stop.set()
        result = future.result(timeout=5)
    assert time.monotonic() - started < 2
    assert result["canceled"] and not result["applied"] and result["winner"] is None
    evidence = result["attempts"][0]["verification_evidence"]
    assert evidence["cancelled"] and not evidence["passed"]
    assert evidence["process_tree_terminated"] is True
    assert "candidate check started" in evidence["output"]
    assert seen == ["pack0"], "stopped Pack must not launch the next candidate"
    time.sleep(3)
    assert not late.exists()
