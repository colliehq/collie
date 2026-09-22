from harness.interop import DelegationEnvelope, DelegationScope, LiveSessionSupervisor


def test_delegation_cannot_expand_parent_authority(tmp_path):
    root = str(tmp_path)
    parent = DelegationScope(tools=frozenset({"read", "write"}),
                             read_roots=(root,), write_roots=(root,),
                             network_hosts=frozenset({"api.example.com"}),
                             max_cost_usd=5, max_tokens=1000)
    child = DelegationScope(tools=frozenset({"read"}), read_roots=(root,),
                            max_cost_usd=1, max_tokens=500)
    envelope = DelegationEnvelope.issue(
        secret=b"s" * 32, parent_id="parent", issuer="collie",
        audience="worker-a", objective="inspect", scope=child,
        now=100, ttl_s=60)
    assert envelope.verify(secret=b"s" * 32, audience="worker-a",
                           parent_scope=parent, now=120,
                           consume_nonce=lambda _nonce, _expires: True)
    restored = DelegationEnvelope.from_dict(envelope.to_dict())
    assert restored == envelope
    assert not restored.verify(secret=b"s" * 32, audience="worker-a",
                               parent_scope=parent, now=120)
    too_broad = DelegationScope(tools=frozenset({"bash"}), read_roots=(root,),
                                max_cost_usd=1, max_tokens=500)
    broad = DelegationEnvelope.issue(
        secret=b"s" * 32, parent_id="parent", issuer="collie",
        audience="worker-a", objective="inspect", scope=too_broad,
        now=100, ttl_s=60)
    assert not broad.verify(secret=b"s" * 32, audience="worker-a",
                            parent_scope=parent, now=120,
                            consume_nonce=lambda _nonce, _expires: True)


def test_live_supervisor_preserves_steer_and_follow_up_semantics():
    supervisor = LiveSessionSupervisor("voice-1")
    supervisor.submit("steer", "change course", message_id="m1")
    supervisor.submit("follow_up", "then summarize", message_id="m2")
    assert [(row["message_id"], row["mode"]) for row in
            supervisor.drain_turn_messages()] == [("m1", "steer"),
                                                   ("m2", "follow_up")]
    supervisor.submit("cancel")
    assert supervisor.cancelled()
