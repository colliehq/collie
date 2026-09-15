"""The post-edit verification state must reach the model BEFORE it composes a final answer.

Every test here drives the real ``Harness`` loop with a scripted provider whose script items
are callables, so each one can assert on the exact ``messages`` list the NEXT request carries.
That is the whole claim under test: not that a reminder exists somewhere, but that the state
arrives on the request following a landed edit, with a truthful outcome, without becoming
durable history, without an extra model call, and without weakening the finish gate.
"""
import pytest

from harness import cli
from harness.preflight import HINT_KIND
from harness.providers import Completion, ToolCall
from _util import _ScriptProvider


def _hints(messages):
    return [m for m in messages if m.get("kind") == HINT_KIND]


@pytest.fixture
def make_workspace(tmp_path, monkeypatch):
    """Build a harness over a workspace whose files the test chooses."""
    monkeypatch.setattr(cli, "DATA", str(tmp_path / "data"))
    live = []

    def _make(files, sub=""):
        root = (tmp_path / sub) if sub else tmp_path
        root.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text, encoding="utf-8")
        h = cli.make_harness(str(root), provider="mock", project="preflight", embed="hash")
        live.append(h)
        return h

    try:
        yield _make
    finally:
        for h in live:
            h.memory.close()
            h.recorder.close()


@pytest.fixture
def workspace(make_workspace):
    """A workspace with a real detectable check and a file worth editing."""
    return make_workspace({
        "value.txt": "old",
        "pyproject.toml": "[tool.pytest.ini_options]\ntestpaths = ['tests']\n",
        "tests/test_x.py": "def test_x():\n    assert True\n",
    })


def _edit(call_id="e", old="old", new="new", path="value.txt"):
    return Completion(tool_calls=[ToolCall(call_id, "edit_file", {
        "path": path, "old_string": old, "new_string": new})])


def _write(call_id, path, content):
    return Completion(tool_calls=[ToolCall(call_id, "write_file", {
        "path": path, "content": content})])


def _bash(call_id, command):
    return Completion(tool_calls=[ToolCall(call_id, "bash", {"command": command})])


def test_state_reaches_the_request_right_after_an_edit(workspace):
    """The turn after a landed edit carries the state, before any answer is composed."""
    seen = {}

    def first(messages):
        seen["before_edit"] = _hints(messages)
        return _edit()

    def after_edit(messages):
        seen["after_edit"] = _hints(messages)
        return Completion(text="Done: the value is now new.")

    workspace.provider = _ScriptProvider([first, after_edit, Completion(text="done")])
    result = workspace.run("t", "Update the value and reply in one sentence.",
                           consolidate=False)

    assert seen["before_edit"] == []          # no edit yet -> nothing to say
    hint = seen["after_edit"]
    assert len(hint) == 1
    body = hint[0]["content"]
    assert hint[0]["role"] == "user" and hint[0]["source"] == "harness"
    assert "NOT RUN yet" in body
    assert "value.txt" in body
    assert "python -m pytest" in body        # the workspace's own detected check
    assert "ORIGINAL request" in body and "original language, format and length" in body
    assert "not a new request from the user" in body
    # The gate still owns the verdict; the hint changed no outcome.
    assert result.verified is False


def test_state_is_never_written_into_durable_history(workspace):
    """Request-scoped only: no transcript duplication, and none of it survives the run."""
    turns = []

    def rec(tag, nxt):
        def _f(messages):
            turns.append((tag, len(_hints(messages))))
            return nxt
        return _f

    workspace.provider = _ScriptProvider([
        rec("t0", _edit()),
        rec("t1", _bash("v", "python -m pytest -q")),
        rec("t2", Completion(text="Updated the value.")),
        Completion(text="Updated the value."),
    ])
    workspace.registry.get("bash").run = lambda args, ctx: "[exit 1]\n1 failed"
    result = workspace.run("t", "Update the value.", consolidate=False)

    # Each post-edit request carries exactly one hint — never an accumulating stack.
    assert [n for _tag, n in turns] == [0, 1, 1]
    assert _hints(result.messages) == []
    assert all(HINT_KIND not in str(m.get("kind", "")) for m in result.messages)


def test_a_failed_check_is_reported_as_failed(workspace):
    """A non-zero check on the latest edit must never read as a pass."""
    seen = []
    workspace.registry.get("bash").run = lambda args, ctx: "[exit 1]\n1 failed"

    def capture(messages):
        seen.append(_hints(messages))
        return Completion(text="Updated.")

    workspace.provider = _ScriptProvider([
        _edit(), _bash("v", "python -m pytest -q"), capture, Completion(text="Updated.")])
    result = workspace.run("t", "Update the value.", consolidate=False)

    body = seen[0][0]["content"]
    assert "DID NOT PASS" in body
    assert "passed" not in body
    assert result.verified is False


def test_a_passing_check_on_the_latest_edit_drops_the_hint(workspace):
    """Pending state is recomputed, so real fresh evidence removes it with no bookkeeping."""
    seen = []
    workspace.registry.get("bash").run = lambda args, ctx: "3 passed"

    def capture(messages):
        seen.append(_hints(messages))
        return Completion(text="Updated the value; pytest passes.")

    workspace.provider = _ScriptProvider([
        _edit(), _bash("v", "python -m pytest -q"), capture,
        Completion(text="Updated the value; pytest passes.")])
    result = workspace.run("t", "Update the value.", consolidate=False)

    assert seen[0] == []
    assert result.verified is True
    # and the long reminder did not fire either — this is the sequence we wanted
    assert [m for m in result.messages if m.get("kind") == "verification_reminder"] == []


def test_a_second_edit_after_a_pass_brings_the_state_back(workspace):
    """Freshness is per-edit: editing again invalidates the pass and the hint returns."""
    seen = []
    workspace.registry.get("bash").run = lambda args, ctx: "3 passed"

    def capture(messages):
        seen.append(_hints(messages))
        return Completion(text="Updated twice.")

    workspace.provider = _ScriptProvider([
        _edit("e1"), _bash("v", "python -m pytest -q"),
        _edit("e2", old="new", new="newer"), capture, Completion(text="Updated twice.")])
    workspace.run("t", "Update the value twice.", consolidate=False)

    assert len(seen[0]) == 1
    body = seen[0][0]["content"]
    assert "NOT RUN yet" in body
    # the command it already watched succeed here is what it names, not a guess from markers
    assert "already ran successfully in this workspace" in body
    assert "python -m pytest -q" in body


def test_a_runner_created_mid_run_reaches_the_very_next_request(make_workspace):
    """The create-a-project workflow: no suite at the first edit, one by the second.

    Detection is memoized, and memoizing it for the whole run is exactly wrong here — the
    workspace the host described is not the workspace the model is now working in.
    """
    ws = make_workspace({"README.md": "# demo\n"}, sub="fresh")
    seen = []

    def after_first_edit(messages):
        seen.append(_hints(messages))
        return _write("p", "package.json",
                      '{"name": "demo", "scripts": {"test": "node test.js"}}\n')

    def after_package(messages):
        seen.append(_hints(messages))
        return Completion(text="Added the package and its test script.")

    ws.provider = _ScriptProvider([
        _write("r", "README.md", "# demo\n\nUsage.\n"),
        after_first_edit, after_package, Completion(text="Added.")])
    ws.run("t", "Start the project.", consolidate=False)

    first = seen[0][0]["content"]
    assert "NOT RUN yet" in first
    assert "run this project's applicable check" in first   # nothing to name yet, truthfully
    second = seen[1][0]["content"]
    assert "npm run test" in second
    assert "package.json#scripts.test" in second


def test_a_changed_test_script_is_not_named_from_the_stale_detection(make_workspace):
    """A previously detected script that the model renames must not be quoted back."""
    ws = make_workspace({
        "README.md": "# demo\n",
        "package.json": '{"name": "demo", "scripts": {"test:ci": "node ci.js"}}\n',
    }, sub="renamed")
    seen = []

    def after_first_edit(messages):
        seen.append(_hints(messages))
        return _edit("s", old="test:ci", new="test", path="package.json")

    def after_rename(messages):
        seen.append(_hints(messages))
        return Completion(text="Renamed the script.")

    ws.provider = _ScriptProvider([
        _write("r", "README.md", "# demo\n\nUsage.\n"),
        after_first_edit, after_rename, Completion(text="Renamed.")])
    ws.run("t", "Rename the test script.", consolidate=False)

    assert "npm run test:ci" in seen[0][0]["content"]
    after = seen[1][0]["content"]
    assert "npm run test" in after and "test:ci" not in after


def test_detection_is_redone_once_per_landed_edit_and_not_otherwise(workspace, monkeypatch):
    """Bounded: a landed edit invalidates the memo; nothing else pays for a scan.

    This replaces the draft's once-per-run expectation. The goal is unchanged — discovery
    must not be repeated turn after turn — but the unit is the edit generation, because that
    is what can change the answer.
    """
    from harness import verification

    calls = []
    real = verification.detect_verification_commands
    monkeypatch.setattr(verification, "detect_verification_commands",
                        lambda cwd: (calls.append(cwd), real(cwd))[1])
    seen = []

    def capture(nxt):
        def _f(messages):
            hints = _hints(messages)
            seen.append((len(hints), len(calls),
                         hints[0]["content"] if hints else ""))
            return nxt
        return _f

    workspace.provider = _ScriptProvider([
        _edit("e1"),                                            # lands -> generation 1
        capture(Completion(tool_calls=[ToolCall("r", "read_file", {"path": "value.txt"})])),
        capture(_edit("bad", old="not-in-the-file", new="x")),   # ERROR: never written
        capture(_edit("e2", old="new", new="newer")),            # lands -> generation 2
        capture(Completion(text="Updated.")),
        Completion(text="Updated."),
    ])
    workspace.run("t", "Update the value.", consolidate=False)

    # one hint every post-edit turn, but only two scans: one per edit that actually landed
    assert [n for n, _c, _b in seen] == [1, 1, 1, 1]
    assert [c for _n, c, _b in seen] == [1, 1, 1, 2]
    assert all("python -m pytest" in b for _n, _c, b in seen)


@pytest.mark.parametrize("output,command", [
    ("3 passed", "python -m pytest -q | tail -5"),          # exit status masked by a pipeline
    ("ERROR: command timed out", "python -m pytest -q"),    # never finished
    ("[exit 2]\nImportError", "python -m pytest -q"),       # errored
])
def test_a_masked_or_uncertain_run_is_not_a_pass(workspace, output, command):
    """A result that proves nothing leaves the state pending and is never called a pass."""
    seen = []
    workspace.registry.get("bash").run = lambda args, ctx: output

    def capture(messages):
        seen.append(_hints(messages))
        return Completion(text="Updated.")

    workspace.provider = _ScriptProvider([
        _edit(), _bash("v", command), capture, Completion(text="Updated.")])
    result = workspace.run("t", "Update the value.", consolidate=False)

    assert len(seen[0]) == 1, "a result that proves nothing must leave the state pending"
    assert "passed" not in seen[0][0]["content"]
    assert result.verified is False
    # it must also not be quoted back as a check that "already ran successfully"
    assert "already ran successfully in this workspace" not in seen[0][0]["content"]


def test_a_zero_test_run_is_never_quoted_as_a_check_that_worked(workspace):
    """Boundary this lane does NOT move: a clean-exit run that collected nothing.

    ``_repro_failed`` reads the host's exit marker, and a runner that collected zero tests and
    exited zero carries none — so the pre-existing repro gate already counts it as fresh
    evidence and the preflight (which only ever mirrors that verdict) correctly produces no
    pending hint rather than inventing a stricter one. What this lane owns is that the state is
    never UPGRADED: the zero-test run is not remembered as a check that worked, so a later edit
    gets the workspace's detected command rather than a quotation of the empty run. Tightening
    the gate itself belongs to the separate verification-evidence review.
    """
    seen = []
    workspace.registry.get("bash").run = lambda args, ctx: "collected 0 items\n\nno tests ran"

    def capture(messages):
        seen.append(_hints(messages))
        return Completion(text="Updated twice.")

    workspace.provider = _ScriptProvider([
        _edit("e1"), _bash("v", "python -m pytest -q"),
        _edit("e2", old="new", new="newer"), capture, Completion(text="Updated twice.")])
    workspace.run("t", "Update the value twice.", consolidate=False)

    assert len(seen[0]) == 1
    assert "already ran successfully in this workspace" not in seen[0][0]["content"]
    assert "detected from" in seen[0][0]["content"]


def test_the_users_own_request_and_steering_are_untouched(workspace):
    """The hint is additive: the original wording and a boundary steer both survive it."""
    seen, drains = {}, {"n": 0}

    def steering():
        drains["n"] += 1
        # fire at the request boundary of the turn that also carries the preflight state
        return ["Answer in Spanish, one sentence."] if drains["n"] == 2 else []

    workspace.steering = steering

    def after_edit(messages):
        seen["msgs"] = list(messages)
        return Completion(text="Listo.")

    workspace.provider = _ScriptProvider([_edit(), after_edit, Completion(text="Listo.")])
    result = workspace.run("t", "Update the value. Reply in one sentence.", consolidate=False)

    msgs = seen["msgs"]
    users = [m for m in msgs if m.get("role") == "user" and not m.get("kind")]
    assert any("Reply in one sentence." in str(m.get("content", "")) for m in users)
    assert any("Answer in Spanish" in str(m.get("content", "")) for m in msgs)
    # the hint rides LAST, after the durable history, so it disturbs no cached prefix
    assert msgs[-1]["kind"] == HINT_KIND
    assert len([m for m in msgs if m.get("kind") == HINT_KIND]) == 1
    # the steer is durable history; the hint is not
    assert any("Answer in Spanish" in str(m.get("content", "")) for m in result.messages)
    assert _hints(result.messages) == []


def test_the_hint_does_not_age_out_the_users_just_attached_image(workspace, monkeypatch):
    """End to end: loop messages -> the real SDK payload, with pixels intact.

    The hint rides last and wears ``role: "user"``, which is what a role-only scan in the
    multimodal transport mistook for a new turn — demoting the caller's own attachment to
    history, the one class of image the budget may drop silently. This drives the real loop,
    then hands the exact message list it produced to the real provider payload builder.
    """
    from tests.test_claude_sdk_images import _Provider, image_block, images, texts

    seen = {}

    def after_edit(messages):
        seen["msgs"] = list(messages)
        return Completion(text="Done.")

    block = image_block()
    workspace.provider = _ScriptProvider([_edit(), after_edit, Completion(text="Done.")])
    workspace.run("t", [{"type": "text", "text": "Fix what this screenshot shows."},
                        {"type": "image", "media_type": block["media_type"],
                         "data": block["data"]}], consolidate=False)

    msgs = seen["msgs"]
    assert msgs[-1]["kind"] == HINT_KIND                    # the hint really is last
    sdk = _Provider()
    sdk.complete("COLLIE SYSTEM", msgs, [{"name": "bash", "description": "run",
                                          "input_schema": {"type": "object"}}])

    assert sdk.request["protocol"] == 2
    assert images(sdk.request) == [{"type": "image", "media_type": "image/png",
                                    "data": block["data"]}]
    prose = "\n".join(texts(sdk.request))
    assert "was omitted" not in prose
    assert "check on the current edit: NOT RUN yet" in prose   # the hint still arrives
    assert block["data"] not in prose

    # And over budget it refuses before the worker instead of quietly dropping the image.
    from harness import claude_agent_sdk as transport
    monkeypatch.setattr(transport, "_MAX_REQUEST_IMAGES", 0)
    refused = _Provider()
    completion = refused.complete("COLLIE SYSTEM", msgs, [])
    assert completion.stop_reason == "error"
    assert "nothing was truncated" in completion.error_detail
    assert refused.spawned == 0


def test_no_hint_without_an_edit_or_with_self_verify_off(workspace):
    """Read-only work and explicitly disabled self-verification are never nudged."""
    seen = []

    def capture(messages):
        seen.append(_hints(messages))
        return Completion(text="The file says 'old'.")

    workspace.provider = _ScriptProvider([
        Completion(tool_calls=[ToolCall("r", "read_file", {"path": "value.txt"})]),
        capture, Completion(text="The file says 'old'.")])
    workspace.run("t", "What does value.txt say?", consolidate=False)
    assert seen[0] == []

    seen2 = []

    def capture2(messages):
        seen2.append(_hints(messages))
        return Completion(text="Updated.")

    workspace.self_verify = False
    workspace.provider = _ScriptProvider([_edit(), capture2, Completion(text="Updated.")])
    workspace.run("t2", "Update the value.", consolidate=False)
    assert seen2[0] == []


def test_the_hint_costs_no_extra_model_call(workspace):
    """It rides a request the loop was making anyway."""
    workspace.provider = _ScriptProvider([
        _edit(), _bash("v", "python -m pytest -q"), Completion(text="Updated; pytest passes.")])
    workspace.registry.get("bash").run = lambda args, ctx: "3 passed"
    result = workspace.run("t", "Update the value.", consolidate=False)
    assert result.model_calls == 3


def test_an_ignoring_provider_still_meets_the_unchanged_required_gate(workspace):
    """Fallback protection: the hint is not a substitute for the finish gate."""
    workspace.verify_gate = True
    workspace.require_assert = True
    workspace.max_turns = 6
    hints_seen = []

    def answer(messages):
        hints_seen.append(len(_hints(messages)))
        return Completion(text="All done, I updated the value.", stop_reason="end_turn")

    workspace.provider = _ScriptProvider([_edit(), answer, answer, answer, answer])
    result = workspace.run("t", "Update the value.", consolidate=False)

    assert result.verified is False
    reminders = [m for m in result.messages if m.get("kind") == "verification_reminder"]
    assert len(reminders) == workspace.verify_max     # bounded exactly as before
    assert hints_seen and all(n == 1 for n in hints_seen)


def test_an_explicit_required_verification_wording_stays_authoritative(workspace):
    """With a task-owned verify contract the host names no command of its own."""
    seen = []
    workspace.verify_nudge = "Re-run your reproduction script; it must print OK."

    def capture(messages):
        seen.append(_hints(messages))
        return Completion(text="Updated.")

    workspace.provider = _ScriptProvider([_edit(), capture, Completion(text="Updated.")])
    workspace.run("t", "Fix the bug.", consolidate=False)

    body = seen[0][0]["content"]
    assert "pytest" not in body
    assert "the verification this task requires" in body
