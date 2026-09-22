import json
import time

import pytest


def _registry_row(name="io.example/calendar", url="https://mcp.example.test/mcp"):
    return {
        "server": {
            "name": name, "title": "Example Calendar", "version": "1.2.3",
            "description": "Calendar scheduling and meeting tools",
            "repository": {"url": "https://github.com/example/calendar-mcp"},
            "remotes": [{"type": "streamable-http", "url": url}],
        },
        "_meta": {"io.modelcontextprotocol.registry/official": {
            "status": "active", "publishedAt": "2026-08-01T00:00:00Z",
        }},
    }


def test_private_goal_is_reduced_to_allowlisted_labels_only():
    from harness import mcp_discovery as discovery

    private = "Schedule ACME-SECRET launch meetings from C:/clients/acme and play Spotify"
    intent = discovery.infer_needs(private)

    assert intent["raw_goal_shared"] is False
    assert {row["id"] for row in intent["needs"]} == {"calendar", "music"}
    wire = json.dumps(intent, ensure_ascii=False)
    assert "ACME" not in wire and "clients" not in wire
    allowed = {term for profile in discovery._NEEDS.values() for term in profile["registry_terms"]}
    assert set(intent["registry_terms"]) <= allowed


def test_registry_search_rejects_arbitrary_terms_and_uses_private_cache(tmp_path, monkeypatch):
    from harness import mcp_discovery as discovery

    cache = tmp_path / "registry.json"
    monkeypatch.setattr(discovery, "_CACHE", str(cache))
    calls = []

    def request(term):
        calls.append(term)
        return [_registry_row()]

    monkeypatch.setattr(discovery, "_registry_request", request)
    out = discovery.search_registry(["calendar", "ACME secret", "calendar"], now=100)
    again = discovery.search_registry(["calendar"], now=101)

    assert calls == ["calendar"]
    assert out["terms"] == ["calendar"] and out["raw_goal_shared"] is False
    assert again["candidates"][0]["trust_level"] == "community_unreviewed"
    assert calls == ["calendar"]
    assert json.loads(cache.read_text(encoding="utf-8"))["schema_version"] == 1


def test_registry_normalization_allows_https_remote_but_never_one_clicks_local_package():
    from harness import mcp_discovery as discovery

    remote = discovery._normalize_registry_row(_registry_row())
    assert remote["installability"] == "review_and_connect"
    assert remote["remote"]["url"].startswith("https://")

    local = _registry_row(url="http://127.0.0.1:3333/mcp")
    local["server"]["packages"] = [{
        "registryType": "npm", "identifier": "@example/mcp", "version": "1.0.0",
    }]
    local = discovery._normalize_registry_row(local)
    assert local["remote"] is None and local["installability"] == "review_only"
    assert any("not one-click" in warning for warning in local["warnings"])


def test_recommendation_prefers_curated_and_marks_public_matches(monkeypatch):
    from harness import mcp_discovery as discovery

    monkeypatch.setattr(discovery, "_configured_by_name", lambda: {})
    monkeypatch.setattr(discovery, "_configured_by_url", lambda: {})
    curated = discovery.recommend("work on a GitHub pull request")
    assert curated["recommendations"][0]["name"] == "github"
    assert curated["raw_goal_shared"] is False and curated["connection_made"] is False

    monkeypatch.setattr(discovery, "search_registry", lambda terms, refresh=False: {
        "candidates": [discovery._normalize_registry_row(_registry_row())],
        "terms": list(terms), "errors": [], "raw_goal_shared": False,
    })
    public = discovery.recommend("avoid calendar meeting conflicts", include_registry=True)
    row = public["recommendations"][0]
    assert row["trust_level"] == "community_unreviewed"
    assert row["capabilities"] == ["calendar"]
    assert public["registry_terms"] and public["raw_goal_shared"] is False


def test_cached_candidate_resolution_is_exact_and_config_name_is_stable(tmp_path, monkeypatch):
    from harness import mcp_discovery as discovery

    monkeypatch.setattr(discovery, "_CACHE", str(tmp_path / "registry.json"))
    discovery._write_cache({"schema_version": 1, "queries": {
        "calendar": {"fetched_at": int(time.time()), "servers": [_registry_row()]},
    }})
    normalized = discovery._normalize_registry_row(_registry_row())
    candidate = discovery.cached_candidate(normalized["id"])
    assert candidate and candidate["remote"]["url"] == "https://mcp.example.test/mcp"
    assert discovery.cached_candidate("registry:io.example/calendar@latest") is None
    assert discovery.cached_candidate(normalized["id"], now=int(time.time()) + discovery._CACHE_TTL + 1) is None
    assert discovery.candidate_config_name(candidate) == discovery.candidate_config_name(candidate)
    assert discovery.candidate_config_name(candidate).startswith("registry-calendar-")


def test_catalog_rechecks_client_metadata_document_support(monkeypatch):
    from harness import mcpclient

    hit = dict(mcpclient.CATALOG["slack"], name="slack")
    monkeypatch.setattr(mcpclient, "_discover_oauth", lambda _url: {
        "client_id_metadata_document_supported": True,
    })
    assert mcpclient.catalog_connection_mode(hit, {}) == "cimd"
    assert mcpclient.catalog_connection_mode(hit, {"client_id": "owned"}) == "configured"


def test_candidate_connection_rejects_private_network_endpoint_before_persisting(
        tmp_path, monkeypatch):
    from harness import mcp_discovery, mcpclient

    candidate = mcp_discovery._normalize_registry_row(
        _registry_row(url="https://internal.example/mcp"))
    monkeypatch.setattr(mcp_discovery, "cached_candidate", lambda _cid: candidate)
    monkeypatch.setattr(mcpclient, "_safe_oauth_url", lambda _url: False)
    monkeypatch.setattr(mcpclient, "_CONFIG", str(tmp_path / "mcp.json"))

    with pytest.raises(ValueError, match="public HTTPS"):
        mcpclient.prepare_registry_candidate(candidate["id"])
    assert not (tmp_path / "mcp.json").exists()


def test_cimd_login_uses_published_client_id_and_fixed_redirect(monkeypatch):
    from harness import httpserver
    import urllib.parse
    import webbrowser
    from harness import mcpclient

    class FakeServer:
        server_address = ("127.0.0.1", mcpclient.BYO_PORT)
        timeout = 0

        def __init__(self, address, _handler):
            assert address == ("127.0.0.1", mcpclient.BYO_PORT)

        def handle_request(self):
            return None

        def server_close(self):
            return None

    monkeypatch.setattr(httpserver, "HTTPServer", FakeServer)
    monkeypatch.setattr(webbrowser, "open", lambda _url: True)
    monkeypatch.setattr(mcpclient, "_discover_oauth", lambda _url: {
        "authorization_endpoint": "https://auth.example.test/authorize",
        "token_endpoint": "https://auth.example.test/token",
        "client_id_metadata_document_supported": True,
        "resource_scopes": ["calendar:read"],
    })
    seen = {}
    try:
        mcpclient.login("calendar", {"url": "https://mcp.example.test/mcp"}, timeout=1,
                        announce=lambda url: seen.setdefault("url", url))
    except RuntimeError as exc:
        assert "no authorization code" in str(exc)
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query)
    assert query["client_id"] == [mcpclient.CLIENT_ID_METADATA_URL]
    assert query["redirect_uri"] == ["http://localhost:8898/callback"]
    assert query["scope"] == ["calendar:read"]


def test_cimd_bind_failure_does_not_suggest_an_unpublished_port(monkeypatch):
    from harness import httpserver
    from harness import mcpclient

    def occupied(_address, _handler):
        raise OSError("address already in use")

    monkeypatch.setattr(httpserver, "HTTPServer", occupied)
    monkeypatch.setattr(mcpclient, "_discover_oauth", lambda _url: {
        "authorization_endpoint": "https://auth.example.test/authorize",
        "token_endpoint": "https://auth.example.test/token",
        "client_id_metadata_document_supported": True,
    })
    with pytest.raises(RuntimeError, match="published OAuth callback port") as exc:
        mcpclient.login("calendar", {"url": "https://mcp.example.test/mcp"}, timeout=1)
    assert "configure your own client_id" in str(exc.value)
    assert "is free right now" not in str(exc.value)
