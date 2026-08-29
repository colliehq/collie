from harness.memory import SqliteMemory
from harness.online import DataClass, OnlineStore
from harness.onlinesync import apply_remote, stage_local
from harness.procedure_memory import ProcedureMemory


def connected(store, user="u", device="d"):
    store.connect(base_url="https://api.collie.test", user_id=user, workspace_id="w",
                  device_id=device, device_name="Laptop", access_token="a", refresh_token="r",
                  access_expires_at=9999999999, refresh_expires_at=9999999999)


def test_only_project_scoped_recallable_memory_is_staged_and_it_is_sealed(tmp_path):
    memory_path = str(tmp_path / "memory.db")
    memory = SqliteMemory(memory_path, embedder=None)
    memory.remember("shared fact", project="local", scope="local", status="attested")
    memory.remember("personal fact", project="local", scope="personal", status="attested")
    memory.propose("unreviewed", project="local", scope="local")
    memory.close()
    store = OnlineStore(str(tmp_path / "online.db")); connected(store)
    store.bind_project("remote", "local", memory_path, memory_data_class=DataClass.SEALED)
    out = stage_local(store)
    assert out["memories"] == 1
    pending = [row for row in store.pending() if row["object_type"] == "memory"]
    assert len(pending) == 1 and pending[0]["content"]["alg"] == "A256GCM"
    assert "shared fact" not in str(pending)
    store.close()


def test_other_users_project_memory_arrives_as_proposal(tmp_path):
    memory_path = str(tmp_path / "memory.db")
    SqliteMemory(memory_path, embedder=None).close()
    store = OnlineStore(str(tmp_path / "online.db")); connected(store, user="me")
    store.bind_project("remote", "local", memory_path)
    store.db.execute("""INSERT INTO sync_objects(object_id,object_type,project_id,data_class,
      content_json,version,updated_at,tombstone,dirty,conflict_of) VALUES(?,?,?,?,?,?,?,0,0,'')""",
      ("memory:other:1", "memory", "remote", "cloud_indexed",
       '{"text":"team claim","status":"attested","origin_user_id":"someone-else"}', 1, 1))
    store.db.commit()
    assert apply_remote(store)["memories_imported"] == 1
    memory = SqliteMemory(memory_path, embedder=None)
    rows = memory.list_claims(project="local", allowed_scopes=["local"])
    assert rows[0]["status"] == "proposed" and rows[0]["text"] == "team claim"
    memory.close(); store.close()


def test_only_sealed_procedural_derivatives_are_staged_never_raw_events(tmp_path):
    project = str(tmp_path / "repo")
    project_dir = tmp_path / "repo"
    project_dir.mkdir()
    memory_path = str(tmp_path / "memory.db")
    SqliteMemory(memory_path, embedder=None).close()
    procedures = ProcedureMemory(str(tmp_path / "procedural-memory.db"))
    now = 9999999900
    for session, offset in (("one", 0), ("two", 100)):
        procedures.observe(session=session, project=project, app="browser",
                           action="browser_navigate", object_kind="web",
                           object_ref="https://example.test/private?q=secret",
                           observed_at=now + offset)
        procedures.observe(session=session, project=project, app="terminal",
                           action="shell", object_kind="command",
                           object_ref="pytest --token secret", observed_at=now + offset + 1)
    candidate = max(procedures.discover(project=project),
                    key=lambda row: len(row["sequence"]))
    procedures.review(candidate["candidate_id"], "accept", confirmed=True)
    procedures.close()

    store = OnlineStore(str(tmp_path / "online.db"))
    connected(store)
    store.bind_project("remote", "local", memory_path, cwd=project,
                       memory_data_class=DataClass.SEALED)
    out = stage_local(store)

    assert out["procedure_candidates"] >= 1
    assert out["learned_workflows"] == 1
    pending = store.pending()
    types = {row["object_type"] for row in pending}
    assert "procedure_event" not in types
    assert {"procedure_candidate", "learned_workflow"} <= types
    routine_rows = [row for row in pending if row["object_type"] in types - {"policy"}]
    assert all(row["data_class"] == "sealed" for row in routine_rows)
    assert all(row["content"]["alg"] == "A256GCM" for row in routine_rows)
    assert "example.test" not in str(routine_rows)
    store.close()
