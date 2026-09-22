import base64
import json
from pathlib import Path

import pytest

from harness import input_assets as assets


@pytest.fixture
def store(tmp_path, monkeypatch):
    directory = tmp_path / "sessions"
    monkeypatch.setenv("COLLIE_SESSIONS_DIR", str(directory))
    return directory


def picture():
    return {"media_type": "image/png", "data": base64.b64encode(b"fixture image").decode()}


def test_reference_can_compare_a_retry_without_consuming_storage(store):
    contexts = [{"path": "sample.py", "content": "exact context\n"}]
    reference = assets.reference_of(images=[picture()], contexts=contexts)
    assert not store.exists()
    assert reference == assets.save("thread", images=[picture()], contexts=contexts)
    assert assets.reference_of() is None


def test_acceptance_persists_exact_context_and_images_without_upload_cache(store):
    text = "开头\n" + "context\n" * 1000 + "原样保留结尾"
    reference = assets.save("thread", images=[picture()], contexts=[
        {"path": "代码.py", "content": text, "startLine": 2, "endLine": 1003}])
    bundle = assets.load("thread", json.loads(json.dumps(reference)))
    assert bundle["images"] == [picture()]
    assert bundle["contexts"][0]["content"] == text
    message = assets.model_message("只阅读", bundle)
    assert message[0]["text"].startswith("只阅读")
    assert text in message[0]["text"]
    assert message[1]["data"] == picture()["data"]
    assert assets.save("thread", images=[picture()], contexts=bundle["contexts"]) == reference
    assert len(list((store / "input-assets" / "thread").glob("*.json"))) == 1


@pytest.mark.parametrize("change", ["missing", "corrupt", "changed", "size", "count", "other_session"])
def test_lost_or_changed_accepted_assets_never_silently_disappear(store, change):
    ref = assets.save("thread", images=[picture()])
    path = Path(assets._path("thread", ref["digest"]))
    if change == "missing":
        path.unlink()
    elif change == "corrupt":
        path.write_text("{broken", encoding="utf-8")
    elif change == "changed":
        body = json.loads(path.read_text(encoding="utf-8"))
        body["images"][0]["data"] = base64.b64encode(b"substituted").decode()
        path.write_text(json.dumps(body), encoding="utf-8")
    elif change == "size":
        ref["bytes"] += 1
    elif change == "count":
        ref["images"] += 1
    with pytest.raises(assets.AssetError):
        assets.load("other" if change == "other_session" else "thread", ref)


@pytest.mark.parametrize("images,contexts", [
    ([{"media_type": "image/svg+xml", "data": "c2lnbg=="}], []),
    ([{"media_type": "image/png", "data": "bad base64"}], []),
    ([picture()] * 9, []), ({}, []),
    ([], [{"path": "x", "content": "z" * 64_001}]),
    ([], [{"path": "x", "content": "z" * 40_000}] * 2),
    ([], [{"path": "x" * 4097}]),
    ([], [{"path": "x", "startLine": 20, "endLine": 10}]),
])
def test_invalid_or_oversized_assets_rejected_before_acceptance(store, images, contexts):
    with pytest.raises(assets.AssetError):
        assets.save("thread", images=images, contexts=contexts)
    assert not list(store.rglob("*.json"))


def test_full_conversation_keeps_existing_attachments_and_allows_idempotent_retry(store, monkeypatch):
    ref = assets.save("thread", images=[picture()])
    monkeypatch.setattr(assets, "MAX_SESSION_BYTES", 1)
    assert assets.save("thread", images=[picture()]) == ref
    with pytest.raises(assets.AssetError, match="existing attachments were kept"):
        assets.save("thread", contexts=[{"path": "new.py", "content": "new"}])
    assert assets.load("thread", ref)["images"] == [picture()]


@pytest.mark.parametrize("sid", ["../outside", "x/../../outside", "C:\\outside"])
def test_asset_paths_are_bound_to_session_root(store, sid):
    with pytest.raises(ValueError):
        assets.save(sid, images=[picture()])


def test_no_attachment_has_no_storage_side_effect(store):
    assert assets.save("thread") is None
    assert not store.exists()
