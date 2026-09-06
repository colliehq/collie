"""Adding a requirement to a Mission that is already running.

Every test drives a REAL ingress: the HTTP route a person's browser calls, the
durable store the note lands in, and the driver boundary that turns it into the
agent's scope.  Nothing here seeds ``case`` to fake an accepted instruction —
the whole point of the feature is the path in between, and that is what used to
lose, duplicate or silently shorten what somebody typed.

No provider and no tool ever runs: the code capability is a local function that
records the goal it was handed, and the planner is a scripted decider.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from harness import webapp
from harness.actions import ActionStore
from harness.jobs import (Capability, DONE_VERIFIED, FAILED_S, NEEDS_YOU,
                          PAUSED, QUEUED, RUNNING, WAITING)
from harness.mission import (HUMAN_NOTE_MAX_CHARS, MissionDriver, MissionStore,
                             NOTE_BODY_MAX_BYTES, STEER_BODY_MAX_BYTES,
                             create_mission, world_leash)
from harness.primitives import _code_verify, _real_code
from harness.verification import workspace_snapshot
from harness.verifier import MissionGoalVerifier

GOAL = "为 collie 的 CLI 增加 --category 过滤，并与 --since、--json 正确组合。"
NOTE = "  另外还要支持 --exclude-category，与现有过滤取差集。\n  缩进和换行都算指令的一部分。  "


# ── local fixtures ──────────────────────────────────────────────────────────
def _dedicated_case(workspace):
    return {
        "_isolated_workspace": str(workspace),
        "code_profile": {
            "durable": True, "direct_dispatch": True, "overnight": False,
            "verify_command": "python -m unittest -q", "slice_turns": 0,
            "verify_timeout_seconds": 300, "max_session_storage_bytes": 0,
            "session_id": "mission-notes-api-test",
        },
    }


def _driver(tmp_path, runner, control=None, decider=None, db="jobs.db"):
    store = MissionStore(str(tmp_path / db))
    actions = ActionStore(str(tmp_path / "actions.db"))

    def refuse(*_args, **_kwargs):
        raise AssertionError("a dedicated code Mission spent a planner call")

    cap = Capability("code", execute=_real_code(runner), verify=_code_verify,
                     reversible=True, risk="code")
    driver = MissionDriver(store, actions, decider or refuse, [cap], control=control,
                           goal_verifier=MissionGoalVerifier(store, actions))
    return store, actions, driver


def _handoff_runner(seen, **extra):
    """A slice that patches but never verifies, so the Mission hands off."""

    def runner(goal, **_context):
        seen.append(goal)
        return {"answer": "slice %d" % len(seen), "verified": False,
                "session_id": "mission-notes-api-test",
                "slice_mutated": True, "patch_attributed": True, "turns": 3,
                **extra}

    return runner


def _green_runner(seen, workspace, target):
    """A slice that really edits and reports a green, attributed host check."""
    baseline = workspace_snapshot(str(workspace))["tree_digest"]

    def runner(goal, *, workspace=None, **_context):
        seen.append(goal)
        target.write_text("x = %d\n" % len(seen), encoding="utf-8")
        post = workspace_snapshot(workspace)
        return {"answer": "done", "verified": True,
                "session_id": "mission-notes-api-test",
                "slice_mutated": True, "patch_attributed": True, "turns": 24,
                # Green suite, but the slice was cut off at its turn limit, so
                # the Mission parks for a human instead of publishing.
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

    return runner


def _create(store, mission_id, workspace, goal=GOAL):
    create_mission(store, mission_id, goal, case=_dedicated_case(workspace),
                   leash=world_leash(may=["code"], autonomous=True,
                                     workspace_mode="isolated"))


# ── HTTP harness ────────────────────────────────────────────────────────────
def _request(url, method="GET", body=None, raw=None):
    data = raw if raw is not None else (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None else None)
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class _Server:
    def __enter__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.root = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.token = "?token=" + webapp.TOKEN
        return self

    def __exit__(self, *_exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def note(self, mid, text, client_id):
        return _request(self.root + "/api/mission/note" + self.token, "POST",
                        {"id": mid, "text": text, "client_id": client_id})

    def notes(self, mid):
        return _request(self.root + "/api/mission/notes?id=%s&token=%s"
                        % (mid, webapp.TOKEN))


def _state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("COLLIE_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("COLLIE_PROVIDER", raising=False)
    return str(tmp_path / "jobs.db")


# ── the HTTP contract ───────────────────────────────────────────────────────
def test_replay_and_refusal_release_the_writer_on_a_reused_store(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = MissionStore(str(tmp_path / "jobs.db"))
    _create(store, "msn_reuse", workspace)
    try:
        first = store.add_pending_note("msn_reuse", "one", NOTE)
        assert first["ok"]
        assert store.add_pending_note("msn_reuse", "one", NOTE)["replay"]
        assert not store.db.in_transaction
        assert store.add_pending_note("msn_reuse", "one", "changed")["code"] == 409
        assert not store.db.in_transaction
        assert store.add_pending_note("missing", "two", NOTE)["code"] == 404
        assert not store.db.in_transaction
        assert store.add_pending_note("msn_reuse", "blank", " ")["code"] == 400
        assert not store.db.in_transaction
        # Both this service and an independent connection can still write.
        other = MissionStore(str(tmp_path / "jobs.db"))
        try:
            assert other.add_pending_note("msn_reuse", "two", "more")["ok"]
        finally:
            other.close()
        assert store.add_pending_note("msn_reuse", "three", "last")["ok"]
        assert len(store.note_history("msn_reuse")) == 3
    finally:
        store.close()


def test_note_is_authed_accepted_exactly_and_replayed_for_the_same_client_id(
        monkeypatch, tmp_path):
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = MissionStore(db)
    _create(store, "msn_http", workspace)
    store.close()

    with _Server() as srv:
        # Auth is required: a drive-by cannot add scope to somebody's Mission.
        code, _ = _request(srv.root + "/api/mission/note", "POST",
                           {"id": "msn_http", "text": "x", "client_id": "c0"})
        assert code == 403
        code, _ = _request(srv.root + "/api/mission/notes?id=msn_http")
        assert code == 403

        code, out = srv.note("msn_http", NOTE, "client-a")
        assert code == 200 and out["accepted"] is True
        assert out["mission_id"] == "msn_http"
        # Byte-for-byte, including the leading/trailing whitespace and newline.
        assert out["note"]["text"] == NOTE
        assert out["note"]["state"] == "pending"
        note_id = out["note"]["id"]
        assert note_id and out["replay"] is False

        # A retry of the same client_id is the same acknowledgment, not a
        # second requirement — this is the offline/refresh/double-click case.
        code, again = srv.note("msn_http", NOTE, "client-a")
        assert code == 200 and again["accepted"] is True
        assert again["note"]["id"] == note_id and again["replay"] is True

        # Same client_id, different text is a conflict and changes nothing.
        code, clash = srv.note("msn_http", NOTE + "改了", "client-a")
        assert code == 409 and clash["accepted"] is False
        assert clash["conflict"] is True
        assert clash["note"]["text"] == NOTE

        # Blank and oversize are refused with an actionable reason, no mutation.
        code, blank = srv.note("msn_http", "   \n ", "client-blank")
        assert code == 400 and "empty" in blank["error"]
        code, big = srv.note("msn_http", "求" * (HUMAN_NOTE_MAX_CHARS + 1), "client-big")
        assert code == 400 and str(HUMAN_NOTE_MAX_CHARS) in big["error"]
        code, no_id = srv.note("msn_http", "text", "")
        assert code == 400 and "client_id" in no_id["error"]
        code, missing = _request(srv.root + "/api/mission/note" + srv.token, "POST",
                                 {"id": "msn_http", "client_id": "c9"})
        assert code == 400 and "text" in missing["error"]

        code, history = srv.notes("msn_http")
        assert code == 200 and history["mission_id"] == "msn_http"
        assert [n["text"] for n in history["notes"]] == [NOTE]
        assert [n["state"] for n in history["notes"]] == ["pending"]

    # The acknowledgment is durable: a fresh process (a restarted host) answers
    # the same retry with the same id, and stores exactly one requirement.
    with _Server() as srv:
        code, after_restart = srv.note("msn_http", NOTE, "client-a")
        assert code == 200 and after_restart["note"]["id"] == note_id
        assert after_restart["replay"] is True
        code, history = srv.notes("msn_http")
        assert len(history["notes"]) == 1

    store = MissionStore(db)
    try:
        assert [n["text"] for n in store.pending_notes("msn_http")] == [NOTE]
        assert store.human_notes("msn_http") == []      # not authority yet
    finally:
        store.close()


def test_note_refuses_terminal_and_recovery_states_but_still_replays(
        monkeypatch, tmp_path):
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = MissionStore(db)
    _create(store, "msn_open", workspace)
    _create(store, "msn_dead", workspace)
    _create(store, "msn_unsure", workspace)
    store.set_state("msn_dead", FAILED_S, "gave up")
    case = dict(store.get("msn_unsure").case)
    case["code_recovery_required"] = {"reason": "worker died mid-patch"}
    store.set_case("msn_unsure", case)
    store.close()

    with _Server() as srv:
        code, accepted = srv.note("msn_open", "继续做这个", "keep")
        assert code == 200 and accepted["accepted"] is True

        code, dead = srv.note("msn_dead", "再加一点", "late")
        assert code == 409 and dead["accepted"] is False
        assert "failed" in dead["error"] and "new Mission" in dead["error"]

        code, unsure = srv.note("msn_unsure", "再加一点", "uncertain")
        assert code == 409 and "recovery" in unsure["error"].lower()

        code, gone = srv.note("msn_missing", "x", "nowhere")
        assert code == 404 and gone["error"] == "unknown mission"

        # A retry of something that WAS accepted keeps answering, even after
        # the Mission became terminal: the person was promised that result.
        store = MissionStore(db)
        store.set_state("msn_open", FAILED_S, "stopped later")
        store.close()
        code, replay = srv.note("msn_open", "继续做这个", "keep")
        assert code == 200 and replay["replay"] is True
        assert replay["note"]["id"] == accepted["note"]["id"]
        # But a NEW client_id on the same terminal Mission is refused.
        code, fresh = srv.note("msn_open", "继续做这个", "keep-2")
        assert code == 409 and fresh["accepted"] is False

    store = MissionStore(db)
    try:
        assert store.pending_notes("msn_dead") == []
        assert store.pending_notes("msn_unsure") == []
    finally:
        store.close()


def test_note_bodies_carry_four_thousand_chinese_characters_and_refuse_oversize(
        monkeypatch, tmp_path):
    """The old 8 KiB body cap rejected legal Unicode as a malformed request."""
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = MissionStore(db)
    _create(store, "msn_cjk", workspace)
    store.close()

    long_cjk = "需" * 4000
    escaped = json.dumps({"id": "msn_cjk", "text": long_cjk,
                          "client_id": "cjk"}).encode("ascii")
    assert len(escaped) > 8192          # would have been a 400 before
    assert len(escaped) <= STEER_BODY_MAX_BYTES
    with _Server() as srv:
        code, out = _request(srv.root + "/api/mission/note" + srv.token, "POST",
                             raw=escaped)
        assert code == 200 and out["note"]["text"] == long_cjk

        # A body past the route's cap is refused whole, with the limit, and is
        # never parsed from a prefix.
        oversize = b'{"id":"msn_cjk","client_id":"huge","text":"' + \
            b"a" * (NOTE_BODY_MAX_BYTES + 64) + b'"}'
        code, refused = _request(srv.root + "/api/mission/note" + srv.token,
                                 "POST", raw=oversize)
        assert code == 413 and "nothing was truncated" in refused["error"]

        code, history = srv.notes("msn_cjk")
        assert [n["text"] for n in history["notes"]] == [long_cjk]


# ── pending → applied, exactly once, at a safe boundary ─────────────────────
def test_a_note_added_while_paused_waits_and_is_consumed_exactly_once_on_resume(
        monkeypatch, tmp_path):
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "msn_paused", workspace)
        assert store.pause("msn_paused") is True
        assert store.get("msn_paused").state == PAUSED
    finally:
        store.close()
        actions.close()

    with _Server() as srv:
        code, out = srv.note("msn_paused", NOTE, "while-paused")
        assert code == 200 and out["note"]["state"] == "pending"
        # Accepting a requirement must not restart a Mission a person paused.
        code, history = srv.notes("msn_paused")
        assert history["notes"][-1]["state"] == "pending"

    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        assert store.get("msn_paused").state == PAUSED   # still paused
        assert store.human_notes("msn_paused") == []
        before = len(seen)
        assert store.resume_paused("msn_paused") == QUEUED
        # Drive it to its next hand-off; the note is consumed once on the way.
        for _ in range(4):
            if store.get("msn_paused").state not in (QUEUED, RUNNING, WAITING):
                break
            driver.advance("msn_paused")
        notes = store.human_notes("msn_paused")
        assert [n["note"] for n in notes] == [NOTE]      # exact bytes, once
        assert store.pending_notes("msn_paused") == []
        dispatched = seen[before:]
        assert dispatched and NOTE in dispatched[0]

        # Driving again does not re-apply it.
        driver.advance("msn_paused")
        assert [n["note"] for n in store.human_notes("msn_paused")] == [NOTE]
    finally:
        store.close()
        actions.close()

    with _Server() as srv:
        code, history = srv.notes("msn_paused")
        assert code == 200
        assert [n["state"] for n in history["notes"]] == ["applied"]
        assert history["notes"][0]["text"] == NOTE
        # The id the acknowledgment promised is the id the history reports.
        assert history["notes"][0]["id"] == "pn_1"


def test_a_note_accepted_mid_slice_invalidates_the_green_check_it_races(
        monkeypatch, tmp_path):
    """The person presses "add requirement" while the coding slice is running."""
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "cli.py"
    target.write_text("x = 0\n", encoding="utf-8")
    seen, posted = [], []
    baseline = workspace_snapshot(str(workspace))["tree_digest"]

    with _Server() as srv:
        def runner(goal, *, workspace=None, **_context):
            seen.append(goal)
            if len(seen) == 1:
                # Real HTTP, from another thread, while this slice is running.
                # The POST must not wait for the tool, and must not be lost.
                posted.append(srv.note("msn_green", "还要加 --exclude-category。",
                                       "mid-slice"))
            target.write_text("x = %d\n" % len(seen), encoding="utf-8")
            post = workspace_snapshot(workspace)
            return {"answer": "done", "verified": True,
                    "session_id": "mission-notes-api-test",
                    "slice_mutated": True, "patch_attributed": True, "turns": 24,
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
            _create(store, "msn_green", workspace)
            driver.advance("msn_green")
            assert posted and posted[0][0] == 200 and posted[0][1]["accepted"]

            case = store.get("msn_green").case
            # The check passed against the goal that was in force when it ran.
            # A person asked for more before anyone could call it delivered, so
            # that verdict was retired into history rather than closing the
            # Mission, and the loop had to earn a new one against the new scope.
            archived = case["code_verification_history"][-1]
            assert archived["verified"] is True
            assert "later human instruction" in archived["reason"]
            superseded = [e for e in store.events("msn_green", 80)
                          if e["name"] == "note_applied"]
            assert superseded[-1]["payload"]["superseded_verification"][
                "verified"] is True
            # The requirement is authority now, exactly once and verbatim.
            assert [n["note"] for n in store.human_notes("msn_green")] == [
                "还要加 --exclude-category。"]
            assert store.pending_notes("msn_green") == []
            # And it reached the coding loop's goal, appended after the original.
            assert len(seen) > 1
            assert "--exclude-category" in seen[-1] and seen[-1].startswith(GOAL)
            assert store.get("msn_green").state != DONE_VERIFIED
        finally:
            store.close()
            actions.close()

    with _Server() as srv:
        code, history = srv.notes("msn_green")
        assert code == 200
        assert [n["state"] for n in history["notes"]] == ["applied"]
        assert history["notes"][0]["id"] == posted[0][1]["note"]["id"]


def test_a_note_accepted_during_verification_defers_the_completion(
        monkeypatch, tmp_path):
    """The clean-completion race: a queued requirement is never lost to a finish."""
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    target = workspace / "cli.py"
    target.write_text("x = 0\n", encoding="utf-8")
    seen, raced = [], []
    store, actions, driver = _driver(tmp_path, _green_runner(seen, workspace, target))

    class RacyVerifier:
        """Independently VERIFIED — but somebody typed during the check."""

        def verify_mission(self, mission, _events, _steps):
            if not raced:
                raced.append(store.add_pending_note(
                    "msn_race", "during-verify", "最后再加一条：也要支持 --json 输出。"))
            from harness.verifier import Verdict, VERIFIED
            return Verdict(VERIFIED, "goal met", evidence=[
                {"channel": "host_check", "at": time.time(), "ok": True,
                 "asserted": True, "detail": "suite passed"}])

    driver.goal_verifier = RacyVerifier()
    try:
        _create(store, "msn_race", workspace)
        driver.advance("msn_race")
        assert raced and raced[0]["ok"] is True
        mission = store.get("msn_race")
        # The verifier said VERIFIED, but the note won the database lock, so the
        # Mission is NOT published as done over an instruction it never saw.
        assert mission.state != DONE_VERIFIED
        assert [n["note"] for n in store.human_notes("msn_race")] == [
            "最后再加一条：也要支持 --json 输出。"]
        assert store.pending_notes("msn_race") == []
        events = [e for e in store.events("msn_race", 80)
                  if e["name"] == "completion_deferred_for_note"]
        assert events and events[-1]["payload"]["applied"] == 1
    finally:
        store.close()
        actions.close()


# ── the note actually reaches the decider ───────────────────────────────────
def test_a_root_note_reaches_the_code_dispatch_goal_in_full_and_in_order(
        monkeypatch, tmp_path):
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    first = "第一条补充：" + "补" * 3000
    second = "第二条补充：" + "充" * 3000
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "msn_scope", workspace)
    finally:
        store.close()
        actions.close()

    with _Server() as srv:
        assert srv.note("msn_scope", first, "a")[0] == 200
        assert srv.note("msn_scope", second, "b")[0] == 200

    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        driver.advance("msn_scope")
        for _ in range(3):
            if seen:
                break
            driver.advance("msn_scope")
        goal = seen[-1]
        # Complete, exact and in the order the person gave them — not the
        # 500-character-per-entry case projection.
        assert first in goal and second in goal
        assert goal.index(first) < goal.index(second)
        assert goal.startswith(GOAL)
    finally:
        store.close()
        actions.close()


def test_a_root_note_reaches_the_mixed_work_planner_in_full(monkeypatch, tmp_path):
    """A general Mission's actual decider sees the instruction, not a fragment."""
    db = _state_dir(monkeypatch, tmp_path)
    long_note = "详细需求：" + "细" * 4000
    goals, cases = [], []

    def decider(goal, case, _primitives, **_kw):
        goals.append(goal)
        cases.append(case)
        return {"action": "needs_human", "args": {"summary": "handing back"},
                "reason": "test"}

    store = MissionStore(db)
    actions = ActionStore(str(tmp_path / "actions.db"))
    driver = MissionDriver(store, actions, decider, [],
                           goal_verifier=MissionGoalVerifier(store, actions))
    try:
        create_mission(store, "msn_mixed", "帮我处理退款", case={},
                       leash=world_leash(autonomous=True))
    finally:
        store.close()
        actions.close()

    with _Server() as srv:
        assert srv.note("msn_mixed", long_note, "mixed-1")[0] == 200

    store = MissionStore(db)
    actions = ActionStore(str(tmp_path / "actions.db"))
    driver = MissionDriver(store, actions, decider, [],
                           goal_verifier=MissionGoalVerifier(store, actions))
    try:
        driver.advance("msn_mixed")
        assert goals, "the planner never ran"
        assert long_note in goals[-1]               # complete, not a prefix
        assert goals[-1].startswith("帮我处理退款")
        ledger = cases[-1]["_human_note_ledger"]
        assert ledger["notes"] == 1 and ledger["chars"] == len(long_note)
        # The case marker must not claim retention the decision does not get.
        assert "appended to GOAL" in ledger["authoritative"]
        projection = cases[-1]["human_updates"][-1]
        assert projection["projection_only"] is True
    finally:
        store.close()
        actions.close()


# ── transport identity: re-delivery must not duplicate ──────────────────────
def test_a_steer_recorded_before_a_failed_ack_is_not_appended_twice(tmp_path):
    """Save-success then ACK-failure then reopen then re-delivery."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen, acked = [], []
    deliveries = []

    def control(_mission_id):
        # The same durable mailbox message, delivered twice, because the first
        # acknowledgment never reached the transport.
        deliveries.append(1)
        message = {"text": "顺便把 README 也更新了", "id": 7, "ref": "steer:run-9:7"}

        def ack(ok, bad):
            acked.append((list(ok), list(bad)))
            if len(deliveries) == 1:
                raise RuntimeError("the worker died before the ack landed")

        return {"cancel": False, "steers": [message], "ack": ack}

    store, actions, driver = _driver(tmp_path, _handoff_runner(seen), control=control)
    try:
        _create(store, "msn_dup", workspace)
        driver.advance("msn_dup")
        assert len(store.human_notes("msn_dup")) == 1    # saved on delivery 1
        assert acked and acked[0][0] == [7]              # ack attempted, failed
        failures = [e for e in store.events("msn_dup", 60)
                    if e["name"] == "steer_ack_failed"]
        assert failures
    finally:
        store.close()
        actions.close()

    # A new process re-opens the Mission and the mailbox redelivers.
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen), control=control)
    try:
        driver.advance("msn_dup")
        assert len(deliveries) >= 2
        notes = store.human_notes("msn_dup")
        assert [n["note"] for n in notes] == ["顺便把 README 也更新了"]
        # It is settled, so the transport IS acknowledged this time, and it was
        # not re-announced as a fresh steer.
        assert acked[-1][0] == [7]
        replays = [e for e in store.events("msn_dup", 80)
                   if e["name"] == "steer_already_recorded"]
        assert replays and replays[-1]["payload"]["note_ids"] == [notes[0]["note_id"]]
    finally:
        store.close()
        actions.close()


def test_identical_text_with_a_new_message_identity_is_new_scope(tmp_path):
    """Dedup is on the message, never on the words: people do repeat themselves."""
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen, queue = [], [("再快一点", "steer:run-1:1"), ("再快一点", "steer:run-1:2")]

    def control(_mission_id):
        batch = [{"text": text, "id": ref, "ref": ref} for text, ref in queue]
        queue.clear()
        return {"cancel": False, "steers": batch, "ack": lambda _ok, _bad: None}

    store, actions, driver = _driver(tmp_path, _handoff_runner(seen), control=control)
    try:
        _create(store, "msn_same", workspace)
        driver.advance("msn_same")
        assert [n["note"] for n in store.human_notes("msn_same")] == \
            ["再快一点", "再快一点"]
    finally:
        store.close()
        actions.close()


# ── history is complete, stably identified and honest ───────────────────────
def test_history_reports_prior_ledger_notes_pending_notes_and_rejections(
        monkeypatch, tmp_path):
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "msn_hist", workspace)
        assert driver.advance("msn_hist") == NEEDS_YOU
        # A pre-existing ledger note from a completely different surface.
        assert store.continue_handoff_result("msn_hist", "先把过滤做完")["ok"] is True
        assert driver.advance("msn_hist") == NEEDS_YOU
    finally:
        store.close()
        actions.close()

    with _Server() as srv:
        assert srv.note("msn_hist", NOTE, "h1")[0] == 200
        code, history = srv.notes("msn_hist")
        assert code == 200
        assert [n["text"] for n in history["notes"]] == ["先把过滤做完", NOTE]
        assert [n["state"] for n in history["notes"]] == ["applied", "pending"]
        # Every entry has a stable id, including the one the note API never saw.
        assert history["notes"][0]["id"].startswith("hn_")
        assert history["notes"][1]["id"].startswith("pn_")
        assert len({n["id"] for n in history["notes"]}) == 2
        first_ids = [n["id"] for n in history["notes"]]

        code, again = srv.notes("msn_hist")
        assert [n["id"] for n in again["notes"]] == first_ids   # stable

        code, gone = _request(
            srv.root + "/api/mission/notes?id=nope&token=" + webapp.TOKEN)
        assert code == 404


def test_a_pending_note_the_ledger_can_no_longer_hold_is_settled_as_rejected(
        monkeypatch, tmp_path):
    """A note left 'pending' for ever would be a silent drop wearing a label."""
    db = _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    seen = []
    store, actions, driver = _driver(tmp_path, _handoff_runner(seen))
    try:
        _create(store, "msn_full", workspace)
        assert store.add_pending_note("msn_full", "c1", "这条会被拒绝")["ok"] is True
        # The ledger fills up between acceptance and consumption.
        original = store._admit_notes_locked
        store._admit_notes_locked = lambda *_a, **_k: (
            False, "this Mission already holds 500 durable instructions", {})
        driver.advance("msn_full")
        store._admit_notes_locked = original

        assert store.human_notes("msn_full") == []
        assert store.pending_notes("msn_full") == []      # settled, not looping
        history = store.note_history("msn_full")
        assert [n["state"] for n in history] == ["rejected"]
        assert "500 durable instructions" in history[0]["error"]
        assert history[0]["text"] == "这条会被拒绝"
    finally:
        store.close()
        actions.close()

    with _Server() as srv:
        code, out = srv.notes("msn_full")
        assert code == 200 and out["notes"][0]["state"] == "rejected"
        assert out["notes"][0]["error"]


def test_bounds_are_counted_across_accepted_and_still_pending_notes(tmp_path):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = MissionStore(str(tmp_path / "missions.db"))
    try:
        _create(store, "msn_bounds", workspace)
        chunk = "满" * HUMAN_NOTE_MAX_CHARS
        for n in range(20):
            assert store.add_pending_note("msn_bounds", "c%d" % n, chunk)["ok"] is True
        # 20 x 20,000 = the whole 400,000-character ledger budget, all of it
        # still pending. The next one is refused NOW, at the surface, instead
        # of being accepted and quietly dropped at consumption.
        refused = store.add_pending_note("msn_bounds", "c20", chunk)
        assert refused["ok"] is False and refused["code"] == 409
        assert "none are discarded" in refused["error"]
        assert len(store.pending_notes("msn_bounds")) == 20
    finally:
        store.close()


# ── the root steer route points at the surface that works ───────────────────
def test_steering_a_root_mission_run_is_refused_with_the_note_api(tmp_path):
    from harness.missionweb import MissionService
    from harness.tasktree import TaskTreeStore

    store = MissionStore(str(tmp_path / "missions.db"))
    tree = TaskTreeStore(str(tmp_path / "tasktree.db"))
    workspace = tmp_path / "repo"
    workspace.mkdir()
    try:
        _create(store, "msn_root", workspace)
        run_id = tree.create_root(GOAL, {}, [], mission_id="msn_root",
                                  workspace=str(workspace))["run_id"]
        svc = MissionService.__new__(MissionService)
        svc.store = store
        svc._run_tree = tree
        out = svc.steer_specialist(run_id, "把它做完")
        # Not "queued": queued would be a lie, because nothing consumes it.
        assert out["queued"] is False
        assert out["use"] == "/api/mission/note"
        assert "msn_root" in out["error"] and "/api/mission/note" in out["error"]
        # And it really did not enqueue anything.
        assert tree.has_messages(run_id, ("steer",)) is False
    finally:
        tree.close()
        store.close()


def test_the_specialist_steer_route_parses_a_four_thousand_character_body(
        monkeypatch, tmp_path):
    """An escaped 4,000-character steer must reach admission, not a 400."""
    _state_dir(monkeypatch, tmp_path)
    payload = json.dumps({"run_id": "run_missing", "text": "改" * 4000}).encode("ascii")
    assert len(payload) > 8192          # the old cap; a legal steer, refused
    with _Server() as srv:
        code, out = _request(srv.root + "/api/mission/specialist/steer" + srv.token,
                             "POST", raw=payload)
        # It got past the transport and was answered on its merits.
        assert code != 400 and "JSON" not in str(out.get("error") or "")

        # 4,001 characters is over the admitted steer limit, and the refusal
        # says so with the number instead of shortening the instruction.
        code, refused = _request(
            srv.root + "/api/mission/specialist/steer" + srv.token, "POST",
            {"run_id": "run_missing", "text": "改" * 4001})
        assert code == 400 and refused["note_limit"] == 4000
        assert refused["note_chars"] == 4001

        # And a body past the route's own byte cap is refused whole.
        oversize = b'{"run_id":"run_missing","text":"' + \
            b"a" * (STEER_BODY_MAX_BYTES + 64) + b'"}'
        code, big = _request(srv.root + "/api/mission/specialist/steer" + srv.token,
                             "POST", raw=oversize)
        assert code == 413 and "nothing was truncated" in big["error"]


def test_returning_an_accepted_mission_refuses_an_oversize_note_before_creating_one(
        monkeypatch, tmp_path):
    """A refused instruction must not leave a half-scoped successor behind."""
    from harness.jobs import DONE_ACCEPTED
    from harness.missionweb import MissionService

    _state_dir(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    store = MissionStore(str(tmp_path / "jobs.db"))
    _create(store, "msn_done", workspace)
    store.set_state("msn_done", DONE_ACCEPTED, "accepted by user")
    store.close()

    svc = MissionService()
    try:
        before = {m["mission_id"] for m in svc.missions()}
        out = svc.continue_after_human("msn_done", "长" * (HUMAN_NOTE_MAX_CHARS + 1))
        assert out["note_rejected"] is True
        assert out["note_limit"] == HUMAN_NOTE_MAX_CHARS
        assert str(HUMAN_NOTE_MAX_CHARS) in out["error"]
        # Nothing was created: the person fixes their message and tries again.
        assert {m["mission_id"] for m in svc.missions()} == before

        # An accepted one becomes the successor's scope verbatim — the whole
        # instruction, not a 2,000-character prefix of it.
        exact = "  重新开始：\n" + "继" * 5000 + "  "
        out = svc.continue_after_human("msn_done", exact)
        successor = out["mission_id"]
        assert successor not in before
        assert [n["note"] for n in svc.store.human_notes(successor)] == [exact]
        assert svc.store.note_history(successor)[0]["text"] == exact
    finally:
        svc.close()
