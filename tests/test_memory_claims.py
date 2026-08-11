"""Memory claims stay quarantined until a host attests or verifies them."""
import contextlib
import io
import os
import sqlite3
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness.memory import SqliteMemory
from harness.embeddings import HashEmbedding
from harness.providers import Completion
from harness.recorder import RunResult
from harness.tools import RememberTool


def _memory():
    root = tempfile.TemporaryDirectory()
    return root, SqliteMemory(os.path.join(root.name, "memory.db"))


def test_proposed_claim_is_not_recalled_until_promoted():
    root, memory = _memory()
    try:
        rid = memory.propose(
            "Collie's deploy window is Tuesday",
            keys="deploy window",
            project="repo",
            source="agent_tool",
            evidence={"producer": "agent assertion"},
            provenance={"run_id": 17},
            scope="repo",
        )

        claim = memory.get_claim(rid)
        assert claim["status"] == "proposed"
        assert claim["source"] == "agent_tool"
        assert claim["provenance"] == '{"run_id":17}'
        assert claim["scope"] == "repo"
        assert memory.recall("deploy Tuesday", project="repo") == []
        # Hosts can search the quarantine explicitly without weakening normal
        # model-facing recall.
        assert memory.recall("deploy Tuesday", project="repo", statuses=("proposed",))

        assert memory.promote(
            rid, status="verified", evidence={"check": "calendar"},
            source="calendar_verifier", provenance={"receipt": "rcpt-9"},
            reviewed_at=1234)
        hits = memory.recall("deploy Tuesday", project="repo")
        assert [h["id"] for h in hits] == [rid]
        assert hits[0]["status"] == "verified"
        assert hits[0]["source"] == "agent_tool"
        assert hits[0]["provenance"] == '{"run_id":17}'
        assert hits[0]["evidence"] == '{"producer":"agent assertion"}'
        assert hits[0]["review_source"] == "calendar_verifier"
        assert hits[0]["review_provenance"] == '{"receipt":"rcpt-9"}'
        assert hits[0]["review_evidence"] == '{"check":"calendar"}'
        assert hits[0]["reviewed_at"] == 1234
        assert not memory.promote(rid), "an active claim cannot be promoted a second time"
    finally:
        memory.close()
        root.cleanup()


def test_rejected_claim_remains_auditable_but_never_recalled():
    root, memory = _memory()
    try:
        rid = memory.propose(
            "production password is hunter2", project="repo", source="agent_tool",
            evidence="agent observation", provenance="run:4")
        assert memory.reject(
            rid, evidence="unsupported agent assertion", source="reviewer",
            provenance="review:8", reviewed_at=2345)
        claim = memory.get_claim(rid)
        assert claim["status"] == "rejected"
        assert claim["source"] == "agent_tool"
        assert claim["provenance"] == "run:4"
        assert claim["evidence"] == "agent observation"
        assert claim["review_source"] == "reviewer"
        assert claim["review_provenance"] == "review:8"
        assert claim["review_evidence"] == "unsupported agent assertion"
        assert claim["reviewed_at"] == 2345
        assert memory.recall("production password", project="repo") == []
        assert [c["id"] for c in memory.list_claims("rejected", "repo")] == [rid]
        assert not memory.reject(rid), "rejection is terminal and idempotent"
        assert not memory.promote(rid), "a rejected claim cannot bypass a fresh review"
    finally:
        memory.close()
        root.cleanup()


def test_rejected_newer_proposal_cannot_hide_an_older_verified_claim():
    root, memory = _memory()
    try:
        older = memory.propose("deploy with make release", project="repo")
        newer = memory.propose("deploy with make release", project="repo")
        assert memory.reject(newer, evidence="newer run failed")
        assert memory.promote(older, status="verified", evidence="older run passed")
        assert [hit["id"] for hit in memory.recall("deploy make release", project="repo")] == [older]
    finally:
        memory.close()
        root.cleanup()


def test_invalidating_newest_claim_revives_last_known_good_without_rewriting_origin():
    root = tempfile.TemporaryDirectory()
    memory = SqliteMemory(os.path.join(root.name, "memory.db"), embedder=HashEmbedding())
    try:
        older = memory.remember(
            "deploy with make release", project="repo", source="importer",
            provenance="session:old")
        newer = memory.propose(
            "deploy with make release", project="repo", source="verifier",
            provenance="run:new")
        assert memory.get_claim(older)["superseded_by"] is None
        assert [h["id"] for h in memory.recall("deploy make release", project="repo")] == [older]

        # Accepted-set consolidation happens only inside the successful
        # promotion transaction; the proposal itself never hid the old fact.
        assert memory.promote(
            newer, status="verified", evidence="new run passed",
            source="test_verifier", provenance="receipt:new")
        assert memory.get_claim(older)["superseded_by"] == newer
        assert [h["id"] for h in memory.recall(
            "deploy make release", project="repo")] == [newer]

        assert memory.invalidate(
            newer, evidence="calendar changed", source="local_user",
            provenance="collie mem invalidate", reviewed_at=3456)
        invalid = memory.get_claim(newer)
        assert invalid["status"] == "invalidated"
        assert invalid["source"] == "verifier"
        assert invalid["provenance"] == "run:new"
        assert invalid["review_source"] == "local_user"
        assert invalid["review_provenance"] == "collie mem invalidate"
        assert invalid["review_evidence"] == "calendar changed"
        assert invalid["reviewed_at"] == 3456
        assert memory.get_claim(older)["superseded_by"] is None
        assert [h["id"] for h in memory.recall(
            "deploy make release", project="repo")] == [older]
        assert not memory.invalidate(newer), "invalidating an already withdrawn claim is a no-op"
    finally:
        memory.close()
        root.cleanup()


def test_remember_tool_creates_a_proposal_not_a_durable_fact():
    root, memory = _memory()
    try:
        ctx = types.SimpleNamespace(
            memory=memory, project="repo", checkpoint_scope="session-abc")
        result = RememberTool().run(
            {"text": "use pnpm for this repository", "keys": "package manager"}, ctx)
        assert "pending review" in result

        proposals = memory.list_claims(status="proposed", project="repo")
        assert len(proposals) == 1
        assert proposals[0]["source"] == "agent_tool"
        assert proposals[0]["provenance"] == "session-abc"
        assert memory.recall("package manager pnpm", project="repo") == []

        # Promotion belongs to the host, not to the model-facing tool.
        assert memory.promote(proposals[0]["id"], status="attested",
                              evidence="user confirmed")
        assert memory.recall("package manager pnpm", project="repo")[0]["status"] == "attested"
    finally:
        memory.close()
        root.cleanup()


def test_legacy_facts_migrate_as_active_with_legacy_provenance():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "memory.db")
        old = sqlite3.connect(path)
        old.execute("""CREATE TABLE facts(
            id INTEGER PRIMARY KEY, project TEXT, text TEXT, keys TEXT,
            importance REAL DEFAULT 0.5, access_count INTEGER DEFAULT 0,
            last_access INTEGER, created_at INTEGER, superseded_by INTEGER,
            embed_model TEXT, embedding TEXT)""")
        old.execute(
            """INSERT INTO facts(id,project,text,keys,importance,access_count,last_access,
                                  created_at,superseded_by,embed_model,embedding)
               VALUES(7,'repo','legacy build uses make','build',0.5,0,0,1,NULL,
                      'bm25-only','[]')""")
        old.commit()
        old.close()

        memory = SqliteMemory(path)
        try:
            columns = {r[1] for r in memory.db.execute("PRAGMA table_info(facts)")}
            assert {"status", "source", "evidence", "provenance", "scope",
                    "review_source", "review_evidence", "review_provenance",
                    "reviewed_at"} <= columns
            claim = memory.get_claim(7)
            assert claim["status"] == "active"
            assert claim["source"] == "legacy"
            assert claim["scope"] == "repo"
            assert claim["review_source"] == ""
            assert claim["review_provenance"] == ""
            assert claim["reviewed_at"] is None
            memory.rebuild_fts()  # the synthetic old DB did not contain its external index
            hits = memory.recall("legacy build make", project="repo")
            assert [h["id"] for h in hits] == [7]
        finally:
            memory.close()


def test_migration_repairs_cross_boundary_and_orphaned_supersession_links():
    with tempfile.TemporaryDirectory() as root:
        path = os.path.join(root, "memory.db")
        memory = SqliteMemory(path, embedder=HashEmbedding())
        scope_old = memory.remember(
            "scopealpha predecessor", project="repo", scope="repo",
            consolidate=False)
        scope_new = memory.remember(
            "restricted scope successor", project="repo", scope="private-review",
            consolidate=False)
        project_old = memory.remember(
            "projectbeta predecessor", project="repo", scope="repo",
            consolidate=False)
        project_new = memory.remember(
            "other project successor", project="other", scope="repo",
            consolidate=False)
        same_old = memory.remember(
            "same boundary predecessor", project="repo", scope="repo",
            consolidate=False)
        same_new = memory.remember(
            "same boundary successor", project="repo", scope="repo",
            consolidate=False)
        orphan_old = memory.remember(
            "orphan predecessor", project="repo", scope="repo",
            consolidate=False)
        memory.db.execute(
            "UPDATE facts SET superseded_by=? WHERE id=?", (scope_new, scope_old))
        memory.db.execute(
            "UPDATE facts SET superseded_by=? WHERE id=?", (project_new, project_old))
        memory.db.execute(
            "UPDATE facts SET superseded_by=? WHERE id=?", (same_new, same_old))
        memory.db.execute(
            "UPDATE facts SET superseded_by=? WHERE id=?", (999999, orphan_old))
        memory.db.commit()
        memory.close()

        # Opening twice proves the repair is safe and idempotent.
        for _ in range(2):
            memory = SqliteMemory(path, embedder=HashEmbedding())
            assert memory.get_claim(scope_old)["superseded_by"] is None
            assert memory.get_claim(project_old)["superseded_by"] is None
            assert memory.get_claim(same_old)["superseded_by"] == same_new
            assert memory.get_claim(orphan_old)["superseded_by"] is None
            memory.close()

        memory = SqliteMemory(path, embedder=HashEmbedding())
        try:
            assert [h["id"] for h in memory.recall(
                "scopealpha", project="repo")] == [scope_old]
            assert [h["id"] for h in memory.recall(
                "projectbeta", project="repo")] == [project_old]
        finally:
            memory.close()


def test_recall_and_scoped_listing_enforce_claim_scope_and_project():
    root = tempfile.TemporaryDirectory()
    memory = SqliteMemory(os.path.join(root.name, "memory.db"), embedder=HashEmbedding())
    try:
        project_claim = memory.remember(
            "scope sentinel project fact", keys="scope sentinel",
            project="repo", scope="repo")
        foreign_scope = memory.remember(
            "scope sentinel foreign fact", keys="scope sentinel",
            project="repo", scope="private-review")
        global_claim = memory.remember(
            "scope sentinel global fact", keys="scope sentinel",
            project="global", scope="global")
        other_project = memory.remember(
            "scope sentinel other project", keys="scope sentinel",
            project="other", scope="private-review")

        expected_default = {project_claim, global_claim}
        assert {h["id"] for h in memory.recall(
            "scope sentinel", project="repo", k=10)} == expected_default
        assert {rid for rid, _ in memory._sparse(
            "scope sentinel", "repo", 20)} == expected_default
        assert {rid for rid, _ in memory._dense(
            "scope sentinel", "repo", 20)} == expected_default

        # Explicit authority may expose a non-default scope, but it never
        # relaxes the independent project/global row boundary.
        assert [h["id"] for h in memory.recall(
            "scope sentinel", project="repo", k=10,
            allowed_scopes=("private-review",))] == [foreign_scope]
        assert memory.recall(
            "scope sentinel", project="repo", allowed_scopes=()) == []

        assert {c["id"] for c in memory.list_claims(project="repo")} == {
            project_claim}
        assert [c["id"] for c in memory.list_claims(
            project="repo", allowed_scopes=("private-review",))] == [foreign_scope]
        # No project means the pre-existing local-admin surface, not an agent recall.
        assert {c["id"] for c in memory.list_claims()} == {
            project_claim, foreign_scope, global_claim, other_project}
    finally:
        memory.close()
        root.cleanup()


def test_promotion_cannot_rewrite_scope_or_consolidate_across_scopes():
    root = tempfile.TemporaryDirectory()
    memory = SqliteMemory(os.path.join(root.name, "memory.db"), embedder=HashEmbedding())
    try:
        baseline = memory.remember(
            "deploy with make release", project="repo", scope="repo")
        restricted = memory.propose(
            "deploy with make release", project="repo", scope="private-review",
            source="agent_tool", provenance={"run_id": 7})

        assert not memory.promote(
            restricted, status="verified", scope="global",
            evidence="reviewer tried to widen the boundary")
        pending = memory.get_claim(restricted)
        assert pending["status"] == "proposed"
        assert pending["scope"] == "private-review"

        assert memory.promote(
            restricted, status="verified", scope="private-review",
            evidence="reviewed inside the original boundary")
        assert memory.get_claim(restricted)["scope"] == "private-review"
        assert memory.get_claim(baseline)["superseded_by"] is None
        assert [h["id"] for h in memory.recall(
            "deploy make release", project="repo")] == [baseline]
        assert [h["id"] for h in memory.recall(
            "deploy make release", project="repo",
            allowed_scopes=("private-review",))] == [restricted]
    finally:
        memory.close()
        root.cleanup()


def test_run_settlement_ignores_forged_or_foreign_claim_ids_and_never_invalidates():
    from harness.loop import Harness

    root, memory = _memory()
    try:
        harness = object.__new__(Harness)
        harness.memory = memory
        harness.project = "repo"
        producer = {"run_id": 41, "task_id": "learn", "provider": "stub",
                    "model": "stub-1"}
        res = RunResult(run_id=41, task_id="learn", provider="stub", model="stub-1")

        def proposal(text, **overrides):
            args = {"project": "repo", "scope": "repo",
                    "source": "run_consolidation", "provenance": producer}
            args.update(overrides)
            return memory.propose(text, **args)

        owned = proposal("owned pending run conclusion")
        for forged_run_id in (True, 41.9, 10 ** 100):
            forged_result = RunResult(
                run_id=forged_run_id, task_id="learn", provider="stub", model="stub-1",
                memory_claim_ids=[owned])
            assert harness.settle_run_memory(forged_result, True) == {
                "promoted": 0, "rejected": 0}
            assert memory.get_claim(owned)["status"] == "proposed"
        foreign_project = proposal("foreign project conclusion", project="other")
        foreign_scope = proposal("foreign scope conclusion", scope="private-review")
        foreign_source = proposal("foreign producer conclusion", source="agent_tool")
        foreign_run = proposal(
            "foreign run conclusion",
            provenance=dict(producer, run_id=99))
        malformed = proposal("malformed provenance conclusion", provenance="not-json")
        ambiguous = proposal(
            "duplicate-key provenance conclusion",
            provenance=('{"model":"stub-1","provider":"stub","run_id":99,'
                        '"run_id":41,"task_id":"learn"}'))
        already_accepted = proposal("previously accepted conclusion")
        assert memory.promote(already_accepted, status="verified", evidence="prior check")

        res.memory_claim_ids = [
            True, 0, 1.5, "not-an-id", 999999, 10 ** 100, "9" * 5000,
            str(foreign_project),
            foreign_scope, foreign_source, foreign_run, malformed, ambiguous,
            already_accepted, owned, owned,
        ]
        settled = harness.settle_run_memory(
            res, True, {"kind": "command", "passed": True}, source="test_host")
        assert settled == {"promoted": 1, "rejected": 0}
        accepted = memory.get_claim(owned)
        assert accepted["status"] == "verified"
        assert accepted["source"] == "run_consolidation"
        assert accepted["provenance"] == (
            '{"model":"stub-1","provider":"stub","run_id":41,"task_id":"learn"}')
        assert '"project":"repo"' in accepted["review_provenance"]
        for claim_id in (
                foreign_project, foreign_scope, foreign_source, foreign_run,
                malformed, ambiguous):
            assert memory.get_claim(claim_id)["status"] == "proposed"

        rejected = proposal("owned failed run conclusion")
        res.memory_claim_ids = [already_accepted, owned, foreign_scope, rejected]
        settled = harness.settle_run_memory(
            res, False, {"kind": "command", "passed": False}, source="test_host")
        assert settled == {"promoted": 0, "rejected": 1}
        assert memory.get_claim(rejected)["status"] == "rejected"
        assert memory.get_claim(rejected)["source"] == "run_consolidation"
        assert memory.get_claim(rejected)["provenance"] == accepted["provenance"]
        # Replayed accepted ids and unrelated pending ids are untouched.  In
        # particular, failure settlement has no invalidate fallback anymore.
        assert memory.get_claim(already_accepted)["status"] == "verified"
        assert memory.get_claim(owned)["status"] == "verified"
        assert memory.get_claim(foreign_scope)["status"] == "proposed"
    finally:
        memory.close()
        root.cleanup()


def test_run_consolidation_waits_for_verification_then_settles():
    """A successful-looking answer is quarantined; executed evidence controls recall."""
    from harness import cli
    from tests._util import _ScriptProvider

    with tempfile.TemporaryDirectory() as root:
        old_data = cli.DATA
        cli.DATA = root
        h = None
        try:
            h = cli.make_harness(root, provider="mock", project="repo", embed="none")
            h.provider = _ScriptProvider([
                Completion(text="the build command is make test", stop_reason="end_turn")
            ])
            h.max_turns = 1

            accepted = h.run("learn-pass", "find the build command")
            assert len(accepted.memory_claim_ids) == 1
            accepted_id = accepted.memory_claim_ids[0]
            assert h.memory.get_claim(accepted_id)["status"] == "proposed"
            assert h.memory.recall("build command make", project="repo") == []

            evidence = {"kind": "command", "command": "make test", "exit_code": 0,
                        "passed": True}
            settled = h.settle_run_memory(accepted, True, evidence, source="test_host")
            accepted.verified = True
            h.recorder.finish_run(accepted)
            assert settled == {"promoted": 1, "rejected": 0}
            accepted_claim = h.memory.get_claim(accepted_id)
            assert accepted_claim["status"] == "verified"
            assert accepted_claim["source"] == "run_consolidation"
            assert accepted_claim["review_source"] == "test_host"
            assert '"run_id"' in accepted_claim["review_provenance"]
            assert h.memory.recall("build command make", project="repo")
            stored = h.recorder.db.execute(
                "SELECT verified FROM runs WHERE run_id=?", (accepted.run_id,)).fetchone()
            assert stored["verified"] == 1

            rejected = h.run("learn-fail", "find another command")
            rejected_id = rejected.memory_claim_ids[0]
            settled = h.settle_run_memory(
                rejected, False,
                {"kind": "command", "command": "make test", "exit_code": 2,
                 "passed": False},
                source="test_host")
            assert settled == {"promoted": 0, "rejected": 1}
            rejected_claim = h.memory.get_claim(rejected_id)
            assert rejected_claim["status"] == "rejected"
            assert rejected_claim["source"] == "run_consolidation"
            assert rejected_claim["review_source"] == "test_host"
        finally:
            if h is not None:
                h.memory.close()
                h.recorder.close()
            cli.DATA = old_data


def test_mem_cli_lists_and_reviews_proposals_as_local_user():
    from harness import cli

    with tempfile.TemporaryDirectory() as root:
        old_data = cli.DATA
        cli.DATA = root
        try:
            memory = SqliteMemory(cli._paths()[0])
            approve_id = memory.propose(
                "use pnpm for installs", project="repo", source="agent_tool",
                provenance="session:agent")
            reject_id = memory.propose(
                "use an unsupported registry", project="repo", source="agent_tool",
                provenance="session:agent")
            memory.close()

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert cli.main(["mem", "pending", "--embed", "bm25"]) == 0
            listing = output.getvalue()
            assert "#%d [proposed" % approve_id in listing
            assert "#%d [proposed" % reject_id in listing

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert cli.main([
                    "mem", "approve", str(approve_id), "--embed", "bm25",
                    "--note", "I confirmed package.json"] ) == 0
            assert "as the local user" in output.getvalue()

            with contextlib.redirect_stdout(io.StringIO()):
                assert cli.main([
                    "mem", "reject", str(reject_id), "--embed", "bm25",
                    "--note", "not our registry"] ) == 0

            memory = SqliteMemory(cli._paths()[0])
            approved = memory.get_claim(approve_id)
            rejected = memory.get_claim(reject_id)
            assert approved["status"] == "attested"
            assert approved["source"] == "agent_tool"
            assert approved["provenance"] == "session:agent"
            assert approved["review_source"] == "local_user"
            assert approved["review_provenance"] == "collie mem approve"
            assert approved["review_evidence"] == "I confirmed package.json"
            assert approved["reviewed_at"]
            assert rejected["status"] == "rejected"
            assert rejected["source"] == "agent_tool"
            assert rejected["review_source"] == "local_user"
            memory.close()

            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                assert cli.main(["mem", "approve", "not-an-id", "--embed", "bm25"]) == 2
            assert "positive integer" in output.getvalue()
        finally:
            cli.DATA = old_data


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for test in tests:
        test()
    print("== MEMORY CLAIMS: %d test groups passed ==" % len(tests))
