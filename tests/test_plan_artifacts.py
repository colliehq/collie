import json

import pytest

import harness.plantool as plans


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(plans, "_DIR", str(tmp_path))
    return plans.PlanArtifactStore()


def test_plan_artifact_is_scoped_and_revisioned(store):
    one = store.update("web:s1", {"title": "Ship", "todos": [
        {"id": "code", "content": "implement", "status": "in_progress", "files": ["a.py"]},
        {"id": "test", "content": "verify", "depends_on": ["code"]},
    ]}, expected_revision=0, actor="user")
    assert one["revision"] == 1 and one["todos"][1]["depends_on"] == ["code"]
    assert store.get("web:s2")["todos"] == []
    with pytest.raises(plans.RevisionConflict):
        store.update("web:s1", {"title": "stale"}, expected_revision=0)


def test_dependency_cycle_is_rejected_without_corrupting_current(store):
    with pytest.raises(ValueError):
        store.update("s", {"todos": [
            {"id": "a", "content": "a", "depends_on": ["b"]},
            {"id": "b", "content": "b", "depends_on": ["a"]},
        ]})
    assert store.get("s")["revision"] == 0


def test_model_can_draft_but_cannot_self_approve(store):
    tool = plans.PlanTool(store)
    class ModelCtx:
        project = "p"; checkpoint_scope = "session:one"; approval_source = "model"
    assert tool.run({"approved": True}, ModelCtx()).startswith("ERROR")
    class UserCtx:
        project = "p"; checkpoint_scope = "session:one"; approval_source = "user"
    out = tool.run({"approved": True, "title": "approved plan"}, UserCtx())
    assert "approved" in out
    assert store.get("session:one")["approved"] is True


def test_legacy_array_is_migrated_on_read(tmp_path, monkeypatch):
    monkeypatch.setattr(plans, "_DIR", str(tmp_path))
    path = plans._legacy_path("legacy")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([{"content": "old", "status": "completed"}], fh)
    got = plans.PlanArtifactStore().get("legacy")
    assert got["version"] == 2 and got["todos"][0]["content"] == "old"
