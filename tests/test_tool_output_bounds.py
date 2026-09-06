"""Noisy subprocesses must retain useful evidence without unbounded memory."""
import io
import re
import sys
import threading
import time

from harness import tool_process as process
from harness.tools import BashTool, ToolCtx


def test_capture_preserves_small_output_exactly_and_bounds_giant_lines():
    class BoundedReads(io.StringIO):
        def readline(self, size=-1):
            assert 0 < size <= process.PIPE_READ_CHARS
            return super().readline(size)

    for value in ("first\n最后一行", "HEAD" + "汉" * (process.OUTPUT_CAPTURE_CHARS * 3) + "TAIL"):
        reader = process._Reader(BoundedReads(value))
        reader.run()
        captured = reader.text()
        if len(value) <= process.OUTPUT_CAPTURE_CHARS:
            assert captured == value
            assert reader.omitted_chars == 0
        else:
            assert captured.startswith("HEAD") and captured.endswith("TAIL")
            assert reader.omitted_chars == len(value) - process.OUTPUT_CAPTURE_CHARS
            assert str(reader.omitted_chars) in captured
            assert len(captured) <= process.OUTPUT_CAPTURE_CHARS + 100


def test_many_lines_and_concurrent_snapshots_remain_bounded():
    reader = process._Reader(None)
    failures = []

    def emit():
        try:
            for n in range(100_000):
                reader._append("line-%08d\n" % n)
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=emit)
    thread.start()
    while thread.is_alive():
        assert len(reader.text()) <= process.OUTPUT_CAPTURE_CHARS + 100
        time.sleep(.001)
    thread.join()
    assert not failures
    assert reader.text().startswith("line-00000000\n")
    assert reader.text().endswith("line-00099999\n")
    assert reader.omitted_chars == 100_000 * 14 - process.OUTPUT_CAPTURE_CHARS


def test_real_giant_stdout_and_stderr_keep_failure_tail(tmp_path):
    result = process.run_owned(
        [sys.executable, "-c", "import sys; "
         "sys.stdout.write('OUT-BEGIN'+ 'x'*8388608 + 'OUT-END'); "
         "sys.stderr.write('ERR-BEGIN'+ 'y'*8388608 + 'ERR-END'); sys.exit(7)"],
        cwd=str(tmp_path), timeout_s=20, use_shell=False)
    assert result.status == process.OK and result.returncode == 7
    assert result.stdout.startswith("OUT-BEGIN") and result.stdout.endswith("OUT-END")
    assert result.stderr.startswith("ERR-BEGIN") and result.stderr.endswith("ERR-END")
    assert result.stdout_omitted_chars > 7_000_000
    assert result.stderr_omitted_chars > 7_000_000
    assert len(result.stdout) <= process.OUTPUT_CAPTURE_CHARS + 100
    assert len(result.stderr) <= process.OUTPUT_CAPTURE_CHARS + 100


def test_tool_spill_does_not_claim_limited_capture_is_full_output(tmp_path, monkeypatch):
    from harness import tools

    monkeypatch.setattr(tools, "_SPILL_DIR", str(tmp_path / "spill"))
    output = "BEGIN\n" + "x" * 10_000 + "\nEND-ERROR"
    monkeypatch.setattr(process, "run_owned", lambda *a, **k: process.Outcome(
        process.OK, returncode=9, stdout=output, stdout_omitted_chars=4_000_000))
    result = BashTool().run({"command": "no actual command"},
                           ToolCtx(cwd=str(tmp_path), project="output-test", memory=None))
    assert result.startswith("[exit 9]") and result.endswith("END-ERROR")
    assert "4000000 characters omitted" in result
    assert "captured output" in result and "full output" not in result
    path = re.search(r"saved to (.*?); grep", result).group(1)
    with open(path, encoding="utf-8") as stream:
        assert stream.read() == output
