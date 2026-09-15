"""What the host does with an expanded read batch.

The transport turns one ``{"reads":[...]}`` response into several ORDINARY
``read_file`` tool calls and hands them to the loop that already exists.  The
point of these tests is that nothing about that boundary is special-cased:
every read is authorized on its own, receipted on its own, emitted as its own
tool event, and stopped by cancellation like any other call in a multi-call
turn.  One model round trip is saved; no permission, receipt or stop is.
"""
import pytest

from harness import cli
from harness.gate import Gate, Mode
from harness.settings import RunLimits
from harness.providers import Completion, _parse_response_envelope
from harness.risk import RiskClass
from _util import _ScriptProvider


def _harness(tmp_path, monkeypatch, turns=4, **kw):
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    # A small turn cap keeps these offline tests bounded: _ScriptProvider repeats
    # its last Completion forever, and an uncapped run would spend the ambient
    # default re-proposing the same batch.
    return cli.make_harness(str(tmp_path), provider="mock", project="readbatch",
                            embed="hash", limits=RunLimits(max_turns=turns), **kw)


def _gated(tmp_path, monkeypatch, approve):
    """A harness whose gate treats read_file as consequential.

    ``read_file`` is READ risk and is normally never asked about.  An explicit
    override is exactly the case this test needs: it proves the batch does not
    bypass whatever decision the gate reaches for each individual read.
    """
    harness = _harness(tmp_path, monkeypatch,
                       gate=Gate(cwd=tmp_path, mode=Mode.INTERACTIVE))
    harness.gate.risk_overrides = (
        lambda name: RiskClass.WRITE_LOCAL if name == "read_file" else None)
    harness.approve = approve
    return harness


def _files(tmp_path, count=3):
    for index in range(count):
        (tmp_path / ("f%d.py" % index)).write_text(
            "line one in f%d\nline two in f%d\n" % (index, index), encoding="utf-8")


def _expanded(paths):
    """Exactly what the SDK adapter hands the loop for this batch."""
    text = '{"reads":[%s]}' % ",".join('{"path":"%s"}' % path for path in paths)
    kind, calls = _parse_response_envelope(
        text, allowed_tools=["read_file", "write_file"], read_batch=True)
    assert kind == "reads"
    return calls


def _results(messages):
    return [m for m in messages if m.get("role") == "tool"]


def _unpaired(messages):
    pending = {}
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                cid = getattr(call, "id", None) or (
                    call.get("id") if isinstance(call, dict) else None)
                if cid:
                    pending[cid] = True
        elif message.get("role") == "tool":
            pending.pop(message.get("tool_call_id"), None)
    return pending


def test_each_read_in_a_batch_is_executed_and_receipted_separately(tmp_path, monkeypatch):
    _files(tmp_path)
    h = _harness(tmp_path, monkeypatch)
    calls = _expanded(["f0.py", "f1.py", "f2.py"])
    h.provider = _ScriptProvider([
        Completion(tool_calls=calls, stop_reason="tool_use"),
        Completion(text="read them", stop_reason="end_turn"),
    ])
    events = []
    h.emit = lambda kind, data: events.append((kind, data))
    res = h.run("batch", "read the three files")

    results = _results(res.messages)
    assert len(results) == 3
    # Each result is paired to its own call id, so no read can be mistaken for
    # another read's receipt.
    assert [m["tool_call_id"] for m in results] == [call.id for call in calls]
    assert all(m["name"] == "read_file" for m in results)
    for index, message in enumerate(results):
        assert ("line one in f%d" % index) in message["content"]
    assert not _unpaired(res.messages)
    # One model round trip produced three reads; the reads themselves are still
    # three separate, individually recorded tool executions.
    tool_events = [data for kind, data in events if kind == "tool"]
    assert len([e for e in tool_events if e.get("name") == "read_file"]) == 3
    assert res.tool_calls >= 3


def test_one_batch_is_one_logical_model_request(tmp_path, monkeypatch):
    _files(tmp_path)
    h = _harness(tmp_path, monkeypatch)
    provider = _ScriptProvider([
        Completion(tool_calls=_expanded(["f0.py", "f1.py", "f2.py"]),
                   stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ])
    h.provider = provider
    h.run("batch", "read the three files")
    # Three reads, two completions: the round-trip saving this feature exists
    # for.  It says nothing about how fast the disk reads are.
    assert provider.calls == 2


def test_cancellation_between_reads_stops_the_rest_and_still_receipts_them(
        tmp_path, monkeypatch):
    _files(tmp_path)
    h = _harness(tmp_path, monkeypatch)
    calls = _expanded(["f0.py", "f1.py", "f2.py"])
    h.provider = _ScriptProvider([Completion(tool_calls=calls, stop_reason="tool_use")])

    done = []
    stopped = []
    h.cancelled = lambda: bool(stopped)
    tool = h.registry.get("read_file")
    original = type(tool).run

    def _run(self, args, ctx):
        done.append(args["path"])
        if len(done) == 1:
            stopped.append(True)   # a stop arriving between two reads of one batch
        return original(self, args, ctx)

    monkeypatch.setattr(type(tool), "run", _run)
    res = h.run("batch", "read the three files")

    assert done == ["f0.py"], "no read may start after the run was stopped"
    results = _results(res.messages)
    assert len(results) == 3, "every tool_use still needs a result"
    assert "line one in f0" in results[0]["content"]
    assert all("CANCELED" in m["content"] for m in results[1:])
    assert not _unpaired(res.messages)


def test_every_read_in_a_batch_is_authorized_on_its_own(tmp_path, monkeypatch):
    """An explicit risk override still applies per read, not per batch."""
    _files(tmp_path)
    asked = []

    def approve(name, args, decision):
        asked.append(args["path"])
        # Deny the middle read only: a batch must not be an all-or-nothing
        # permission decision once it reaches the gate.
        return "reject_once" if args["path"] == "f1.py" else "allow_once"

    h = _gated(tmp_path, monkeypatch, approve)
    h.provider = _ScriptProvider([
        Completion(tool_calls=_expanded(["f0.py", "f1.py", "f2.py"]),
                   stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ])
    res = h.run("batch", "read the three files")

    assert asked == ["f0.py", "f1.py", "f2.py"], "each read asks separately"
    results = _results(res.messages)
    assert len(results) == 3
    assert "line one in f0" in results[0]["content"]
    assert "line one in f1" not in results[1]["content"], "the denied read did not run"
    assert "line one in f2" in results[2]["content"]


def test_a_headless_denial_is_honored_for_every_read(tmp_path, monkeypatch):
    _files(tmp_path)
    h = _gated(tmp_path, monkeypatch, None)   # nobody to ask
    h.provider = _ScriptProvider([
        Completion(tool_calls=_expanded(["f0.py", "f1.py"]), stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ])
    res = h.run("batch", "read the two files")

    results = _results(res.messages)
    assert len(results) == 2
    assert all("line one" not in m["content"] for m in results)


def test_batch_reads_honor_the_normal_read_file_arguments(tmp_path, monkeypatch):
    (tmp_path / "long.py").write_text(
        "\n".join("line %d" % n for n in range(1, 41)), encoding="utf-8")
    h = _harness(tmp_path, monkeypatch)
    text = '{"reads":[{"path":"long.py","offset":10,"limit":2}]}'
    kind, calls = _parse_response_envelope(text, allowed_tools=["read_file"],
                                           read_batch=True)
    assert kind == "reads"
    h.provider = _ScriptProvider([
        Completion(tool_calls=calls, stop_reason="tool_use"),
        Completion(text="done", stop_reason="end_turn"),
    ])
    res = h.run("batch", "page into the file")
    body = _results(res.messages)[0]["content"]
    assert "line 10" in body and "line 11" in body and "line 12" not in body
