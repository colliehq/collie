import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from harness import communications as comms


@pytest.fixture
def draft(tmp_path):
    comms.create_connection("mail", channel="email", address="collie@example.test",
        policy={"owner_reply_target": "owner@example.test"}, directory=tmp_path)
    row = comms.create_result("mail", "original", destination="owner@example.test", text="first draft",
                              metadata={"auto_eligible": True}, directory=tmp_path)
    return tmp_path, row


def test_edit_preserves_original_and_requires_explicit_send(draft):
    path, row = draft
    updated = comms.revise_result("mail", "original", "revision", text="corrected draft",
                                 actor="user", expected_digest=row["digest"], directory=path)
    old = comms.get_result("mail", "original", include_private=True, directory=path)
    new = comms.get_result("mail", "revision", include_private=True, directory=path)
    assert old["state"] == "cancelled" and old["text"] == "first draft"
    assert new["state"] == "pending" and new["text"] == "corrected draft"
    assert new["metadata"]["auto_eligible"] is False
    assert [r["id"] for r in comms.next_sendable("mail", directory=path)] == ["revision"]
    assert comms.revise_result("mail", "original", "revision", text="corrected draft", actor="user",
                               expected_digest=row["digest"], directory=path)["digest"] == updated["digest"]


def test_invalid_revision_or_stale_edit_keeps_original(draft):
    path, row = draft
    for text, digest in [("", row["digest"]), ("valid", "stale")]:
        with pytest.raises(comms.CommsError):
            comms.revise_result("mail", "original", "revision", text=text, actor="user",
                                 expected_digest=digest, directory=path)
    assert comms.get_result("mail", "original", directory=path)["state"] == "pending"
    assert comms.get_result("mail", "revision", directory=path) is None


def test_discard_never_requeues_and_does_not_claim_a_delivery(draft):
    path, row = draft
    cancelled = comms.cancel_result("mail", "original", actor="user", expected_digest=row["digest"], directory=path)
    assert cancelled["state"] == "cancelled" and not cancelled["submission_known"]
    assert not comms.next_sendable("mail", directory=path)
    with pytest.raises(comms.StateConflict):
        comms.claim_send("mail", "original", directory=path)
    with pytest.raises(comms.StateConflict):
        comms.retry("mail", "original", actor="user", directory=path)
    assert comms.cancel_result("mail", "original", actor="user", expected_digest=row["digest"], directory=path)["state"] == "cancelled"


def test_send_and_edit_race_has_one_winner(draft):
    path, row = draft
    barrier = threading.Barrier(2)
    def invoke(edit):
        barrier.wait()
        try:
            if edit:
                comms.revise_result("mail", "original", "revision", text="new", actor="user",
                                     expected_digest=row["digest"], directory=path)
            else:
                comms.claim_send("mail", "original", directory=path)
            return True
        except comms.StateConflict:
            return False
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(invoke, [True, False]))
    assert sum(results) == 1
    old = comms.get_result("mail", "original", directory=path)
    new = comms.get_result("mail", "revision", directory=path)
    assert (old["state"] == "cancelled" and new["state"] == "pending") or (old["state"] == "sending" and new is None)
