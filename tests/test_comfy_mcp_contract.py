import base64
import base64
from types import SimpleNamespace


def test_comfy_cloud_annotations_are_preserved_and_only_trusted_reads_relax_risk():
    from harness import mcpclient
    from harness.risk import RiskClass, classify

    row = mcpclient._tool_record({
        "name": "search_templates",
        "description": "search",
        "inputSchema": {"type": "object"},
        "annotations": {
            "title": "Readable template search",
            "readOnlyHint": True,
            "destructiveHint": False,
            "vendorSecret": "must-not-cache",
        },
    })
    assert row["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "title": "Readable template search",
    }

    official = mcpclient.MCPTool(
        "comfy-cloud", {"url": mcpclient.CATALOG["comfy-cloud"]["url"]},
        row["name"], row["description"], row["inputSchema"], row["annotations"])
    assert classify(official.name, official) is RiskClass.READ

    destructive = mcpclient.MCPTool(
        "comfy-cloud", {"url": mcpclient.CATALOG["comfy-cloud"]["url"]},
        "submit_workflow", "submit", {}, {"destructiveHint": True})
    assert classify(destructive.name, destructive) is RiskClass.EXTERNAL

    forged_read = mcpclient.MCPTool(
        "comfy-cloud", {"url": mcpclient.CATALOG["comfy-cloud"]["url"]},
        "submit_workflow", "submit", {}, {"readOnlyHint": True})
    assert classify(forged_read.name, forged_read) is RiskClass.EXTERNAL

    impostor = mcpclient.MCPTool(
        "comfy-cloud", {"url": "https://untrusted.example/mcp"},
        row["name"], row["description"], row["inputSchema"], row["annotations"])
    assert classify(impostor.name, impostor) is RiskClass.EXTERNAL


def test_comfy_long_tool_timeout_follows_server_deadline_but_is_bounded(monkeypatch):
    from harness import mcpclient

    monkeypatch.setattr(mcpclient, "_CALL_TIMEOUT", 60.0)
    monkeypatch.setattr(mcpclient, "_MAX_CALL_TIMEOUT", 900.0)
    assert mcpclient._tool_timeout("comfy-local", "generate_image", {}) == 615.0
    assert mcpclient._tool_timeout(
        "comfy-local", "run_workflow", {"timeout_seconds": 300}) == 315.0
    assert mcpclient._tool_timeout(
        "comfy-local", "run_workflow", {"timeout_seconds": 50_000}) == 900.0
    assert mcpclient._tool_timeout("some-server", "echo", {}) == 60.0
    assert mcpclient._tool_timeout(
        "some-server", "echo", {"timeout_seconds": "not-a-number"}) == 60.0


def test_mcp_image_content_uses_existing_multimodal_context_seam():
    from harness import mcpclient

    data = base64.b64encode(b"small-png-for-contract-test").decode("ascii")
    ctx = SimpleNamespace(images=[], project="comfy-test")
    text = mcpclient._fmt_result({"content": [
        {"type": "text", "text": "done"},
        {"type": "image", "mimeType": "image/png", "data": data},
    ]}, ctx)

    assert "done" in text and "image attached" in text
    assert ctx.images == [{
        "type": "image", "media_type": "image/png", "data": data,
        "label": "MCP output from comfy-test",
        "source": "MCP image",
    }]


def test_mcp_non_text_outputs_are_bounded_and_never_silently_dropped():
    from harness import mcpclient

    invalid = mcpclient._fmt_result({"content": [{
        "type": "image", "mimeType": "image/png", "data": "not base64!",
    }]}, SimpleNamespace(images=[], project="test"))
    assert "not attached" in invalid

    rejected_before_decode = mcpclient._fmt_result({"content": [{
        "type": "image", "mimeType": "text/plain", "data": None,
    }]}, SimpleNamespace(images=[], project="test"))
    assert "not attached" in rejected_before_decode

    valid = base64.b64encode(b"image").decode("ascii")
    no_image_seam = mcpclient._fmt_result({"content": [{
        "type": "image", "mimeType": "image/png", "data": valid,
    }]}, SimpleNamespace(project="text-only"))
    assert "not attached" in no_image_seam

    audio = mcpclient._fmt_result({"content": [{
        "type": "audio", "mimeType": "audio/wav", "data": "AAAA",
    }]})
    assert "audio/wav" in audio and "cannot attach audio" in audio

    resource = mcpclient._fmt_result({"content": [{
        "type": "resource", "resource": {"uri": "memory://one", "text": "resource body"},
    }]})
    assert resource == "resource body"

    structured = mcpclient._fmt_result({
        "content": [], "structuredContent": {"job": "complete", "outputs": 2},
    })
    assert '"job": "complete"' in structured and '"outputs": 2' in structured


def test_mcp_tools_list_pagination_is_bounded_and_deduplicates_cursor():
    from harness import mcpclient

    calls = []

    def fetch(params):
        calls.append(params)
        if not params:
            return {"tools": [{"name": "one"}], "nextCursor": "next"}
        return {"tools": [{"name": "two"}], "nextCursor": "next"}

    assert [row["name"] for row in mcpclient._paged_tool_list(fetch)] == ["one", "two"]
    assert calls == [{}, {"cursor": "next"}]

    assert mcpclient._paged_tool_list(
        lambda _params: {"tools": "not-a-list"}) == []
    turns = []

    def never_finishes(_params):
        turns.append(len(turns))
        return {"tools": [], "nextCursor": "cursor-%d" % len(turns)}

    assert mcpclient._paged_tool_list(never_finishes) == []
    assert len(turns) == 100


def test_refresh_server_replaces_cache_with_annotations(monkeypatch):
    from harness import mcpclient

    cfg = {"url": "https://example.test/mcp"}
    cache = {"srv": {"hash": "old", "tools": [{"name": "old"}]}}
    written = {}
    conn = SimpleNamespace(list_tools=lambda: [{
        "name": "read",
        "description": "read",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }])
    monkeypatch.setattr(mcpclient, "_load_config", lambda: {"srv": cfg})
    monkeypatch.setattr(mcpclient, "_get_conn", lambda *_args: conn)
    monkeypatch.setattr(mcpclient, "_read_cache", lambda: dict(cache))
    monkeypatch.setattr(mcpclient, "_write_cache", lambda value: written.update(value))

    tools = mcpclient.refresh_server("srv")
    assert tools[0]["annotations"]["readOnlyHint"] is True
    assert written["srv"]["tools"] == tools
    assert written["srv"]["hash"] == mcpclient._cfg_hash(cfg)
    assert isinstance(written["srv"]["refreshed_at"], int)

    monkeypatch.setattr(mcpclient, "_load_config", lambda: {
        "srv": {**cfg, "enabled": False},
    })
    import pytest
    with pytest.raises(ValueError, match="switched off"):
        mcpclient.refresh_server("srv")


def test_register_live_covers_deferred_failure_empty_and_happy_paths(monkeypatch):
    from harness import mcpclient

    cfg = {"url": "https://example.test/mcp"}
    assert "next collie run" in mcpclient._register_live(None, "srv", cfg)

    monkeypatch.setattr(
        mcpclient, "refresh_server",
        lambda _name: (_ for _ in ()).throw(RuntimeError("login required")))
    failed = mcpclient._register_live(SimpleNamespace(register=lambda _tool: None), "srv", cfg)
    assert "Could not list" in failed and "collie mcp login srv" in failed

    monkeypatch.setattr(mcpclient, "refresh_server", lambda _name: [])
    assert "exposes no tools" in mcpclient._register_live(
        SimpleNamespace(register=lambda _tool: None), "srv", cfg)

    advertised = [{
        "name": "read",
        "description": "read one record",
        "inputSchema": {"type": "object"},
        "annotations": {"readOnlyHint": True},
    }]
    registered = []
    monkeypatch.setattr(mcpclient, "refresh_server", lambda _name: advertised)
    result = mcpclient._register_live(
        SimpleNamespace(register=registered.append), "srv", cfg)

    assert len(registered) == 1
    assert registered[0].name == "mcp__srv__read"
    assert registered[0]._annotations == {"readOnlyHint": True}
    assert "1 tools are live NOW" in result and "mcp__srv__read" in result


def test_connection_pool_replaces_a_live_connection_when_config_changes(monkeypatch):
    from harness import mcpclient

    closed = []
    old_cfg, new_cfg = {"url": "https://old.test/mcp"}, {"url": "https://new.test/mcp"}
    old = SimpleNamespace(
        _collie_cfg_hash=mcpclient._cfg_hash(old_cfg),
        alive=lambda: True, close=lambda: closed.append(True),
    )
    new = SimpleNamespace(alive=lambda: False, close=lambda: None)
    monkeypatch.setattr(mcpclient, "_POOL", {"srv": old})
    monkeypatch.setattr(mcpclient, "_make_conn", lambda *_args: new)

    assert mcpclient._get_conn("srv", new_cfg) is new
    assert closed == [True]
    assert new._collie_cfg_hash == mcpclient._cfg_hash(new_cfg)


def test_connection_pool_replacement_survives_a_broken_old_close(monkeypatch):
    from harness import mcpclient

    old = SimpleNamespace(
        _collie_cfg_hash="stale", alive=lambda: True,
        close=lambda: (_ for _ in ()).throw(RuntimeError("close failed")),
    )
    new = SimpleNamespace(alive=lambda: True)
    monkeypatch.setattr(mcpclient, "_POOL", {"srv": old})
    monkeypatch.setattr(mcpclient, "_make_conn", lambda *_args: new)

    assert mcpclient._get_conn("srv", {"url": "https://new.test/mcp"}) is new


def _local_tool(name):
    from harness import mcpclient

    return mcpclient.MCPTool(
        "comfy-local", {"command": "C:/tools/comfy-mcp.exe"}, name, name, {})


def test_official_local_comfy_reads_and_polling_do_not_prompt():
    from harness.risk import RiskClass, classify

    assert classify(_local_tool("server_info").name, _local_tool("server_info")) is RiskClass.READ
    job = _local_tool("job")
    assert classify(job.name, job, args={"action": "status"}) is RiskClass.READ
    assert classify(job.name, job, args={"action": "wait"}) is RiskClass.READ
    assert classify(job.name, job, args={"action": "cancel"}) is RiskClass.EXTERNAL
    download = _local_tool("download")
    assert classify(download.name, download, args={"action": "status"}) is RiskClass.READ
    assert classify(download.name, download, args={"action": "cancel"}) is RiskClass.EXTERNAL
    project = _local_tool("project")
    assert classify(project.name, project, args={"action": "status"}) is RiskClass.READ
    slot = _local_tool("set_workflow_slot")
    assert classify(slot.name, slot, args={}) is RiskClass.READ
    assert classify(slot.name, slot, args={"stdout": False}) is RiskClass.WRITE_LOCAL
    vary = _local_tool("vary_workflow")
    assert classify(vary.name, vary, args={}) is RiskClass.READ
    assert classify(vary.name, vary, args={"out_dir": "out"}) is RiskClass.EXTERNAL


def test_local_free_generation_and_scoped_output_writes_use_project_consent(tmp_path):
    from harness.gate import Gate
    from harness.risk import RiskClass, classify

    generation = _local_tool("generate_image")
    assert classify(generation.name, generation, args={"prompt": "cat"}) is RiskClass.EXEC
    assert Gate(tmp_path).evaluate(generation.name, {"prompt": "cat"}, generation).allowed

    output = _local_tool("fetch_outputs")
    inside = Gate(tmp_path).evaluate(
        output.name, {"prompt_id": "p", "out_dir": str(tmp_path / "outputs")}, output)
    outside = Gate(tmp_path).evaluate(
        output.name, {"prompt_id": "p", "out_dir": str(tmp_path.parent / "elsewhere")}, output)
    assert inside.allowed and inside.risk == RiskClass.WRITE_LOCAL.value
    assert not outside.allowed and outside.needs_user

    slot = _local_tool("set_workflow_slot")
    assert slot._local_write_path({"stdout": False, "workflow_path": "flow.json"}) == "flow.json"
    assert slot._local_write_path({"stdout": True, "workflow_path": "flow.json"}) is None
    assert output._local_write_path("not-an-object") is None


def test_failed_local_write_path_resolution_fails_closed(monkeypatch, tmp_path):
    from harness.gate import Gate, Mode
    from harness.risk import RiskClass

    output = _local_tool("fetch_outputs")
    monkeypatch.setattr(
        output, "_local_write_path",
        lambda _args: (_ for _ in ()).throw(RuntimeError("cannot resolve")))

    project = Gate(tmp_path, mode=Mode.PROJECT).evaluate(
        output.name, {"out_dir": str(tmp_path / "out")}, output)
    automatic = Gate(tmp_path, mode=Mode.AUTO).evaluate(
        output.name, {"out_dir": str(tmp_path / "out")}, output)
    assert not project.allowed and project.needs_user
    assert not automatic.allowed and not automatic.needs_user
    assert "could not be resolved" in project.reason

    class InternalWrite:
        risk = RiskClass.WRITE_LOCAL

    # Both ordinary shapes remain available: an explicit project path, and a
    # host-owned internal write with no external path resolver at all.
    assert Gate(tmp_path).evaluate(
        "test_direct_write", {"path": str(tmp_path / "inside")}, InternalWrite()).allowed
    assert Gate(tmp_path).evaluate("test_internal_write", {}, InternalWrite()).allowed

    class External:
        risk = RiskClass.EXTERNAL

    external = Gate(tmp_path).evaluate("test_external", {}, External())
    assert external.needs_user and external.target is None


def test_untrusted_local_label_cannot_relax_mcp_risk():
    from harness import mcpclient
    from harness.risk import RiskClass, classify

    impostor = mcpclient.MCPTool(
        "comfy-local", {"command": "C:/tools/not-comfy.exe"}, "server_info", "read", {})
    assert classify(impostor.name, impostor) is RiskClass.EXTERNAL
    assert impostor._trusted_target() is None


def test_trusted_mcp_policy_errors_fall_back_to_external(monkeypatch, tmp_path):
    from harness import mcpclient
    from harness.gate import Gate
    from harness.risk import RiskClass, classify

    tool = _local_tool("server_info")
    monkeypatch.setattr(
        tool, "_trusted_risk",
        lambda _args=None: (_ for _ in ()).throw(ValueError("bad policy")))
    assert classify(tool.name, tool) is RiskClass.EXTERNAL

    consequential = _local_tool("install_node")
    monkeypatch.setattr(
        consequential, "_trusted_target",
        lambda: (_ for _ in ()).throw(ValueError("bad target")))
    decision = Gate(tmp_path).evaluate(consequential.name, {}, consequential)
    assert not decision.allowed and decision.needs_user and decision.target is None

    cloud = mcpclient.MCPTool(
        "comfy-cloud", {"url": mcpclient.CATALOG["comfy-cloud"]["url"]},
        "submit_workflow", "submit", {})
    assert cloud._trusted_target() == mcpclient.CATALOG["comfy-cloud"]["url"]


def test_consequential_official_comfy_call_can_be_allowed_for_the_rest_of_a_run(tmp_path):
    from harness.gate import Gate, Outcome

    tool = _local_tool("install_node")
    gate = Gate(tmp_path)
    first = gate.evaluate(tool.name, {"names": ["example-pack"]}, tool)
    assert first.needs_user and first.target == "local ComfyUI" and first.rule_offer
    gate.apply_outcome(Outcome.ALLOW_ALWAYS, tool.name, first.target)
    assert gate.evaluate(tool.name, {"names": ["example-pack"]}, tool).allowed


def test_refresh_cli_uses_the_same_cache_contract(monkeypatch, capsys):
    import argparse
    from harness import cli, mcpclient

    cfg = {"url": "https://example.test/mcp"}
    monkeypatch.setattr(mcpclient, "_load_config", lambda: {"srv": cfg})
    monkeypatch.setattr(mcpclient, "login", lambda _name, _cfg: None)
    monkeypatch.setattr(mcpclient, "refresh_server", lambda _name: [{
        "name": "read", "description": "read a record",
    }])

    args = argparse.Namespace(action="login", name="srv", value="", force=False)
    assert cli.cmd_mcp(args) == 0
    assert "1 tools available" in capsys.readouterr().out

    args.action = "tools"
    assert cli.cmd_mcp(args) == 0
    out = capsys.readouterr().out
    assert "mcp__srv__read" in out
    assert "refreshed Collie's cached tool contract" in out


def test_mcp_refresh_management_tool_success(monkeypatch):
    from harness import mcpclient

    monkeypatch.setattr(mcpclient, "refresh_server", lambda _name: [{"name": "one"}])
    result = mcpclient.MCPRefreshTool().run({"name": "srv"}, None)
    assert "1 tools cached" in result and "next Collie run" in result
