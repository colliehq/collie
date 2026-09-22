"""What a person actually typed must reach the agent, exactly, or be refused.

Every test here drives a REAL ingress path — ``MissionStore.continue_handoff``,
the driver's control boundary, ``MissionService`` — and then reads the goal that
the code capability was actually dispatched with.  Seeding ``case`` directly
would prove only that the composer works; it is the admission and storage path
in between that used to shorten instructions, and that is what is under test.

No provider or tool ever runs: the code capability is a local function that
records the goal it was handed.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from harness.actions import ActionStore
from harness.jobs import Capability, DONE_VERIFIED, NEEDS_YOU, QUEUED, RUNNING
from harness.mission import (HUMAN_LEDGER_MAX_NOTES, HUMAN_NOTE_MAX_CHARS,
                             MissionDriver, MissionStore, STEER_NOTE_MAX_CHARS,
                             _note_digest, admit_human_note, code_mission_goal,
                             create_mission, world_leash)
from harness.primitives import _code_verify, _real_code
from harness.verifier import MissionGoalVerifier

GOAL = ("为 collie 的 CLI 增加 --category 过滤：可重复指定、大小写敏感、"
        "多个参数取并集，并与 --since、--total、--json 正确组合。")


def _dedicated_case(workspace):
    return {
        "_isolated_workspace": str(workspace),
        "code_profile": {
            "durable": True, "direct_dispatch": True, "overnight": False,
            "verify_command": "python -m unittest -q", "slice_turns": 0,
            "verify_timeout_seconds": 300, "max_session_storage_bytes": 0,
            "session_id": "mission-human-notes-test",
        },
    }


def _driver(tmp_path, runner, control=None, db="missions.db"):
    store = MissionStore(str(tmp_path / db))
    actions = ActionStore(str(tmp_path / "actions.db"))

    def refuse(*_args, **_kwargs):
        raise AssertionError("a dedicated code Mission spent a planner call")

    cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                     reversible=True, risk="code")
    driver = MissionDriver(store, actions, refuse, [cap], control=control,
                           goal_verifier=MissionGoalVerifier(store, actions))
    return store, actions, driver


def _handoff_runner(seen, **extra):
    """A slice that patches but never verifies, so the Mission hands off."""

    def runner(goal, **_context):
        seen.append(goal)
        return {"answer": "slice %d" % len(seen), "verified": False,
                "session_id": "mission-human-notes-test",
                "slice_mutated": True, "patch_attributed": True, "turns": 3,
                **extra}

    return runner


def _create(store, mission_id, workspace):
    create_mission(store, mission_id, GOAL, case=_dedicated_case(workspace),
                   leash=world_leash(may=["code"], autonomous=True,
                                     workspace_mode="isolated"))


# ── admission policy ────────────────────────────────────────────────────────
def test_admission_accepts_exact_text_and_refuses_oversize_with_a_reason():
    text = "  保留前后空白，以及\n换行。  "
    admitted, error = admit_human_note(text)
    assert admitted == text and not error       # byte-for-byte, not stripped
    assert admit_human_note("   \n ")[1] == "the instruction is empty"
    over = "字" * (HUMAN_NOTE_MAX_CHARS + 1)
    admitted, error = admit_human_note(over)
    assert admitted == ""                        # nothing is accepted in part
    assert str(HUMAN_NOTE_MAX_CHARS) in error and str(len(over)) in error
    assert "several messages" in error           # tells the caller what to do


# ── the real continue_handoff path ──────────────────────────────────────────
def test_a_two_thousand_character_handoff_note_reaches_the_coding_loop_whole(
        tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    # Deliberately past the old 500-character slice, and past 2000.
    note = ("再补充一点：" +
            "".join("第%d条要求必须逐字保留；" % index for index in range(220)) +
            "最后一句是最重要的：不要删除任何现有测试。")
    assert len(note) > 2000
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "long-note", workspace)
        assert driver.advance("long-note") == NEEDS_YOU

        accepted = store.continue_handoff_result("long-note", note)
        assert accepted["ok"] and accepted["note_ids"]
        driver.advance("long-note")

        assert len(seen) == 2
        # The whole instruction, exactly, including its very last sentence.
        assert note in seen[1]
        assert seen[1].startswith(GOAL)
        assert "不要删除任何现有测试" in seen[1]
        # The case keeps only a labelled projection; it is not the source.
        projection = store.get("long-note").case["human_updates"][-1]
        assert projection["projection_only"] is True
        assert projection["note_chars"] == len(note)
        assert len(projection["note"]) < len(note)
        # ...and the ledger is what the dispatch actually read.
        assert store.human_notes("long-note")[-1]["note"] == note
    finally:
        store.close()
        actions.close()


def test_an_oversized_handoff_note_is_refused_and_the_mission_does_not_move(
        tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "too-big", workspace)
        assert driver.advance("too-big") == NEEDS_YOU

        refused = store.continue_handoff_result("too-big", "字" * (HUMAN_NOTE_MAX_CHARS + 5))
        assert refused["ok"] is False
        assert str(HUMAN_NOTE_MAX_CHARS) in refused["error"]
        # Refusing means refusing: no note, no state change, no half-acceptance.
        assert store.human_notes("too-big") == []
        assert store.get("too-big").state == NEEDS_YOU
        assert not store.get("too-big").case.get("human_updates")
        assert len(seen) == 1
        # The legacy boolean API reports the same refusal.
        assert store.continue_handoff("too-big", "字" * (HUMAN_NOTE_MAX_CHARS + 5)) is False
    finally:
        store.close()
        actions.close()


def test_twenty_one_instructions_all_reach_dispatch_in_the_order_given(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "many", workspace)
        assert driver.advance("many") == NEEDS_YOU
        notes = ["第 %02d 条修改意见：请把第 %02d 个参数也纳入过滤。" % (n, n)
                 for n in range(1, 22)]
        for note in notes:
            assert store.continue_handoff_result("many", note)["ok"] is True
            assert driver.advance("many") == NEEDS_YOU

        goal = seen[-1]
        # All 21 survive the old 20-entry rolling window, oldest first.
        positions = [goal.index(note) for note in notes]
        assert all(note in goal for note in notes)
        assert positions == sorted(positions)
        assert goal.index(GOAL) < positions[0]
        assert len(store.human_notes("many")) == 21
        # The case projection is still bounded; it just is not the source.
        assert len(store.get("many").case["human_updates"]) == 20
    finally:
        store.close()
        actions.close()


def test_case_compaction_and_a_restart_never_lose_an_instruction(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    first = "第一条：必须支持大小写敏感的精确匹配。" * 40
    later = "最后一条：新增 --category-file 从文件读取类别列表。" * 40
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "compacted", workspace)
        assert driver.advance("compacted") == NEEDS_YOU
        assert store.continue_handoff_result("compacted", first)["ok"] is True

        # A research-sized blob forces real case compaction, which is what used
        # to be able to evict an instruction along with the blob.
        case = dict(store.get("compacted").case)
        case["huge_research_result"] = "巨大的调研结果。" * 12000
        store.set_case("compacted", case)
        assert "huge_research_result" not in store.get("compacted").case
        assert driver.advance("compacted") == NEEDS_YOU
        assert store.continue_handoff_result("compacted", later)["ok"] is True
    finally:
        store.close()
        actions.close()

    # A different process opens the same database and dispatches.
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        driver.advance("compacted")
        goal = seen[-1]
        assert first in goal and later in goal
        assert goal.index(first) < goal.index(later)
        assert [x["note"] for x in store.human_notes("compacted")] == [first, later]
    finally:
        store.close()
        actions.close()


# ── the real control-boundary steer path ────────────────────────────────────
def test_a_long_live_steer_is_stored_verbatim_at_the_control_boundary(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    # Past the old 1000-character control-boundary slice.
    steer = "现在改主意了：" + "".join("另外还要处理第%d种边界情况；" % n for n in range(120))
    assert len(steer) > 1000
    pending = [steer]
    acked = []

    def control(_mission_id):
        messages = [{"text": text, "id": index}
                    for index, text in enumerate(pending)]
        pending.clear()
        return {"cancel": False, "steers": messages,
                "ack": lambda ok, bad: acked.append((list(ok), list(bad)))}

    store, actions, driver = _driver(tmp_path, _handoff_runner(seen), control=control)
    try:
        _create(store, "steered", workspace)
        driver.advance("steered")
        for _ in range(4):
            if len(seen) >= 2 or store.get("steered").state not in (QUEUED, RUNNING):
                break
            driver.advance("steered")

        assert store.human_notes("steered")[-1]["note"] == steer
        assert store.human_notes("steered")[-1]["source"] == "steer"
        # The transport is told to stop redelivering only after it is durable.
        assert acked and acked[-1][0] == [0]
        assert code_mission_goal(store.get("steered"),
                                 notes=store.human_notes("steered")).endswith(steer)
    finally:
        store.close()
        actions.close()


def test_a_steer_is_not_acknowledged_when_the_mission_cannot_store_it(tmp_path):
    """A message dropped on a failed save is an instruction the user lost."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen, acked, delivered = [], [], []

    def control(_mission_id):
        if delivered:
            return {"cancel": False, "steers": []}
        delivered.append(1)
        return {"cancel": False,
                "steers": [{"text": "顺便把 README 也更新了", "id": 7}],
                "ack": lambda ok, bad: acked.append((list(ok), list(bad)))}

    store, actions, driver = _driver(tmp_path, _handoff_runner(seen), control=control)
    try:
        _create(store, "lost", workspace)
        # The save fails the way a stolen lease fails: ownership is gone.
        original = store.add_human_notes_owned
        store.add_human_notes_owned = lambda *_a, **_k: {
            "ok": False, "error": "this run no longer owns the Mission",
            "lost_ownership": True}
        driver.advance("lost")
        store.add_human_notes_owned = original

        # Nothing was stored, and crucially nothing was acknowledged, so the
        # durable mailbox still holds the instruction for the next owner.
        assert store.human_notes("lost") == []
        assert acked == []
    finally:
        store.close()
        actions.close()


# ── new scope retires old completion evidence ───────────────────────────────
def test_a_new_instruction_after_a_green_but_cut_off_slice_is_worked_not_closed(
        tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "ledger.py"
    target.write_text("x = 1\n", encoding="utf-8")
    from harness.verification import workspace_snapshot
    baseline = workspace_snapshot(str(workspace))["tree_digest"]
    seen = []

    def runner(goal, *, workspace=None, **_context):
        seen.append(goal)
        target.write_text("x = %d\n" % len(seen), encoding="utf-8")
        post = workspace_snapshot(workspace)
        return {"answer": "cut off mid-sentence", "verified": True,
                "session_id": "mission-human-notes-test",
                "slice_mutated": True, "patch_attributed": True, "turns": 24,
                # Green suite, but the run never got to finish or report.
                "turns_exhausted": True, "stop_reason": "turn_limit",
                "baseline_tree_digest": baseline,
                "post_tree_digest": post["tree_digest"],
                "verification": {"verified": True, "evidence": {
                    "timestamp": time.time(), "passed": True,
                    "command_passed": True, "ran_after_last_edit": True,
                    "executed": True, "patch_attributed": True,
                    "post_tree_digest": post["tree_digest"],
                    "post_snapshot_complete": post["snapshot_complete"],
                    "command": "python -m unittest -q",
                    "source": "mission_code_profile"}}}

    store, actions, driver = _driver(tmp_path, runner)
    try:
        _create(store, "green", workspace)
        for _ in range(6):
            if store.get("green").state == NEEDS_YOU:
                break
            driver.advance("green")
        mission = store.get("green")
        assert mission.state == NEEDS_YOU
        assert mission.case["code_verified"] is True    # green against the OLD goal
        before = len(seen)

        feature = "新需求：再加一个 --exclude-category 参数，与现有过滤取差集。"
        assert store.continue_handoff_result("green", feature)["ok"] is True

        after = store.get("green").case
        # The old check is history, not a certificate for the new request.
        assert after.get("code_verified") is False
        assert "code_verification" not in after
        archived = after["code_verification_history"][-1]
        assert archived["verified"] is True
        assert archived["verification"]["evidence"]["command"] == "python -m unittest -q"

        driver.advance("green")
        # It went back to work on the new instruction instead of finishing on
        # the strength of a check that ran before anyone asked for it.
        assert len(seen) > before
        assert feature in seen[before]
        assert seen[before].startswith(GOAL)
        assert store.get("green").state != DONE_VERIFIED
    finally:
        store.close()
        actions.close()


def test_new_input_is_detected_by_revision_not_by_a_rolling_list_length(tmp_path):
    """Past the 20-entry window, every further correction still counts as new."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "revisions", workspace)
        assert driver.advance("revisions") == NEEDS_YOU
        revisions = []
        for n in range(24):
            assert store.continue_handoff_result(
                "revisions", "修改意见 %02d：改一下第 %02d 项。" % (n, n))["ok"] is True
            assert driver.advance("revisions") == NEEDS_YOU
            state = store.get("revisions").case["code_dispatch"]
            revisions.append((state["revision"], state["max_note_id"]))
        # Both signals move on every single instruction, including the ones
        # after the bounded case projection stopped growing.
        assert len({r for r, _i in revisions}) == 24
        assert [i for _r, i in revisions] == sorted(i for _r, i in revisions)
        assert len(store.get("revisions").case["human_updates"]) == 20
        # The digest is content-addressed: it is stable when nothing was said.
        notes = store.human_notes("revisions")
        assert _note_digest(notes) == _note_digest(list(notes))
        assert _note_digest(notes) != _note_digest(notes[:-1])
    finally:
        store.close()
        actions.close()


# ── compatibility with Missions saved before the ledger existed ─────────────
def test_a_mission_saved_before_the_ledger_keeps_and_extends_its_updates(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "legacy", workspace)
        # Exactly what the old writer left behind: bounded notes, no ledger.
        case = dict(store.get("legacy").case)
        case["human_updates"] = [
            {"at": 100, "note": "旧版本写下的第一条意见"},
            {"at": 200, "note": "旧版本写下的第二条意见", "steer": True},
        ]
        store.set_case("legacy", case)
        assert store.human_notes("legacy")[0]["note"] == "旧版本写下的第一条意见"

        driver.advance("legacy")
        assert "旧版本写下的第一条意见" in seen[0]
        assert "旧版本写下的第二条意见" in seen[0]

        assert store.continue_handoff_result("legacy", "升级之后的新意见")["ok"] is True
        # Migration keeps the old chronology and appends, rather than starting over.
        assert [x["note"] for x in store.human_notes("legacy")] == [
            "旧版本写下的第一条意见", "旧版本写下的第二条意见", "升级之后的新意见"]
        driver.advance("legacy")
        assert seen[-1].index("旧版本写下的第一条意见") < seen[-1].index("升级之后的新意见")
    finally:
        store.close()
        actions.close()


def test_the_ledger_refuses_a_new_note_rather_than_evicting_an_accepted_one(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = MissionStore(str(tmp_path / "missions.db"))
    try:
        _create(store, "full", workspace)
        now = int(time.time())
        with store._lock:
            for n in range(HUMAN_LEDGER_MAX_NOTES):
                store.db.execute(
                    "INSERT INTO mission_human_notes(mission_id,at,source,host,note) "
                    "VALUES(?,?,?,?,?)", ("full", now, "handoff", 0, "第 %d 条" % n))
            store.db.commit()
        case = dict(store.get("full").case)
        ok, error, _info = store._admit_notes_locked(
            "full", case, [("再来一条", "handoff", False)], now)
        assert ok is False and str(HUMAN_LEDGER_MAX_NOTES) in error
        assert len(store.human_notes("full")) == HUMAN_LEDGER_MAX_NOTES
    finally:
        store.close()


# ── the HTTP-facing service surfaces ────────────────────────────────────────
def _stub_service(tmp_path, name="svc"):
    from harness.missionweb import MissionService

    class _Decider:
        def __call__(self, *_args, **_kwargs):
            return {"action": "needs_human", "args": {"summary": "unused"}}

    return MissionService(base=str(tmp_path / name), decider=_Decider(), stub=True)


def test_the_service_returns_an_actionable_rejection_for_an_oversized_note(tmp_path):
    """The person who typed it learns what to change, not "cannot continue"."""
    service = _stub_service(tmp_path)
    try:
        workspace = tmp_path / "repo"
        workspace.mkdir()
        seen = []
        _create(service.store, "svc", workspace)
        cap = Capability("code", execute=_real_code(_handoff_runner(seen)),
                         verify=_code_verify, reversible=True, risk="code")
        driver = MissionDriver(
            service.store, service.actions, lambda *_a, **_k: None, [cap],
            goal_verifier=MissionGoalVerifier(service.store, service.actions))
        assert driver.advance("svc") == NEEDS_YOU

        refused = service.continue_after_human("svc", "字" * (HUMAN_NOTE_MAX_CHARS + 1))
        assert refused["note_rejected"] is True
        assert refused["note_limit"] == HUMAN_NOTE_MAX_CHARS
        assert str(HUMAN_NOTE_MAX_CHARS) in refused["error"]
        assert service.store.get("svc").state == NEEDS_YOU
        assert service.store.human_notes("svc") == []

        accepted = service.continue_after_human("svc", "字" * (HUMAN_NOTE_MAX_CHARS - 1))
        assert not accepted.get("error") and accepted["state"] == "queued"
        assert len(service.store.human_notes("svc")[0]["note"]) == HUMAN_NOTE_MAX_CHARS - 1
    finally:
        service.close()


def test_an_oversized_live_steer_is_refused_at_the_surface_not_truncated(tmp_path):
    """The mailbox payload is bounded, so admission uses the transport's limit."""
    service = _stub_service(tmp_path, "steer-svc")
    try:
        out = service.steer_specialist("run_missing", "字" * (STEER_NOTE_MAX_CHARS + 1))
        assert out["note_rejected"] is True and out["queued"] is False
        assert str(STEER_NOTE_MAX_CHARS) in out["error"]
        # Refused before the run is even looked up: the caller's input is wrong
        # whatever the run's state, and the message names the number to fix.
        assert out["note_chars"] == STEER_NOTE_MAX_CHARS + 1
        assert out["note_limit"] == STEER_NOTE_MAX_CHARS
    finally:
        service.close()
