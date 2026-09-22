"""Privacy-preserving MCP capability discovery and recommendation.

The user's goal is classified locally into a small allowlisted capability vocabulary.  Only those
generic labels may be sent to the public MCP Registry; the raw goal, project names, file paths and
conversation never leave the machine through this module.

The public Registry is a source of publisher metadata, not a security review.  Its results remain
``community_unreviewed`` and executable stdio packages are review-only until Collie has a sandbox
that can enforce their declared host authority.  A small host-owned catalog is ranked above it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.parse
import urllib.request


REGISTRY_BASE = "https://registry.modelcontextprotocol.io"
REGISTRY_API = REGISTRY_BASE + "/v0.1/servers"
REGISTRY_NOTICE = (
    "Community result from the public MCP Registry. Registry presence verifies publication "
    "metadata, not code quality or security; review the endpoint, publisher and authority first."
)
_CACHE = os.environ.get("COLLIE_MCP_REGISTRY_CACHE") or os.path.expanduser(
    "~/.collie/mcp_registry_cache.json")
_CACHE_TTL = int(os.environ.get("COLLIE_MCP_REGISTRY_TTL", str(6 * 3600)))
_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_GOAL = 4000
_MAX_RESULTS_PER_TERM = 40


# Patterns stay local. registry_terms are deliberately generic and allowlisted: even a goal such as
# "search ACME-secret-project" can only emit "github" or "issue", never the private project name.
_NEEDS = {
    "team_chat": {
        "label": "team messages", "patterns": (
            "slack", "teams", "discord", "message", "notify", "notification", "chat",
            "消息", "通知", "群聊", "聊天", "发送", "傳送", "訊息"),
        "registry_terms": ("slack", "teams", "discord"), "services": ("slack",),
    },
    "issue_tracking": {
        "label": "issues and project tracking", "patterns": (
            "linear", "jira", "issue", "ticket", "sprint", "project task", "backlog",
            "工单", "工單", "任务", "任務", "项目管理", "專案管理", "迭代"),
        "registry_terms": ("linear", "jira", "issue"), "services": ("linear", "atlassian"),
    },
    "knowledge": {
        "label": "documents and knowledge", "patterns": (
            "notion", "confluence", "wiki", "knowledge base", "docs", "document",
            "知识库", "知識庫", "文档", "文件", "笔记", "筆記"),
        "registry_terms": ("notion", "confluence", "docs"),
        "services": ("notion", "atlassian"),
    },
    "error_monitoring": {
        "label": "errors and incidents", "patterns": (
            "sentry", "incident", "error monitoring", "crash", "stack trace",
            "错误监控", "錯誤監控", "崩溃", "崩潰", "事故"),
        "registry_terms": ("sentry", "incident"), "services": ("sentry",),
    },
    "payments": {
        "label": "payments and billing", "patterns": (
            "stripe", "payment", "invoice", "billing", "subscription", "refund",
            "付款", "支付", "账单", "帳單", "发票", "發票", "退款"),
        "registry_terms": ("stripe", "payment"), "services": ("stripe",),
    },
    "crm": {
        "label": "CRM and sales", "patterns": (
            "hubspot", "crm", "lead", "sales pipeline", "customer relationship",
            "客户关系", "客戶關係", "销售", "銷售", "线索", "線索"),
        "registry_terms": ("hubspot", "crm"), "services": ("hubspot",),
    },
    "deployment": {
        "label": "deployments and hosting", "patterns": (
            "vercel", "deploy", "deployment", "hosting", "domain", "production release",
            "部署", "发布上线", "發佈上線", "托管", "託管", "域名"),
        "registry_terms": ("vercel", "deploy"), "services": ("vercel",),
    },
    "database": {
        "label": "databases", "patterns": (
            "neon", "postgres", "postgresql", "database", "sql", "schema", "query",
            "数据库", "資料庫", "查询", "查詢", "数据表", "資料表"),
        "registry_terms": ("postgres", "database", "neon"), "services": ("neon",),
    },
    "source_control": {
        "label": "source control", "patterns": (
            "github", "pull request", "repository", "repo", "commit", "branch", "release",
            "代码仓库", "程式碼倉庫", "仓库", "倉庫", "拉取请求", "合并请求", "發佈"),
        "registry_terms": ("github", "git"), "services": ("github",),
    },
    "visual_ai": {
        "label": "visual AI workflows", "patterns": (
            "comfy", "comfyui", "generate image", "generate video", "image workflow",
            "生成图片", "生成圖像", "生成视频", "生成影片", "视觉工作流", "視覺工作流程"),
        "registry_terms": ("comfy", "image", "video"), "services": ("comfy-cloud",),
    },
    "diagramming": {
        "label": "collaborative diagrams and whiteboards", "patterns": (
            "diagram", "whiteboard", "architecture diagram", "system design", "eraser",
            "miro", "figjam", "lucidchart", "excalidraw", "tldraw",
            "画板", "白板", "架构图", "架構圖", "系统设计", "系統設計"),
        "registry_terms": ("diagram", "whiteboard", "eraser"), "services": ("eraser",),
    },
    "calendar": {
        "label": "calendar and scheduling", "patterns": (
            "calendar", "schedule", "meeting", "appointment", "availability",
            "日历", "日曆", "日程", "会议", "會議", "预约", "預約"),
        "registry_terms": ("calendar", "google-calendar"), "services": (),
    },
    "email": {
        "label": "email", "patterns": (
            "email", "gmail", "outlook", "mailbox", "inbox", "reply",
            "邮件", "郵件", "邮箱", "郵箱", "收件箱", "回信"),
        "registry_terms": ("gmail", "email", "outlook"), "services": (),
    },
    "music": {
        "label": "music playback", "patterns": (
            "spotify", "apple music", "music", "playlist", "song", "album",
            "音乐", "音樂", "歌单", "歌單", "歌曲", "播放"),
        "registry_terms": ("spotify", "music"), "services": (),
    },
    "files_cloud": {
        "label": "cloud files", "patterns": (
            "google drive", "dropbox", "onedrive", "box", "cloud files",
            "网盘", "網盤", "云盘", "雲端硬碟", "云文件", "雲端檔案"),
        "registry_terms": ("drive", "dropbox", "onedrive"), "services": (),
    },
}


_CURATED = {
    "slack": {
        "description": "Read and work with Slack messages and channels through Slack's remote MCP.",
        "capabilities": ("team_chat",), "data": ("messages", "channels", "workspace identity"),
        "effects": ("read external data", "send or modify only with action-time authority"),
    },
    "linear": {
        "description": "Read and manage Linear issues, projects and cycles.",
        "capabilities": ("issue_tracking",), "data": ("issues", "projects", "teams"),
        "effects": ("read external data", "create or modify only with action-time authority"),
    },
    "notion": {
        "description": "Use Notion pages, databases and workspace knowledge.",
        "capabilities": ("knowledge",), "data": ("pages", "databases", "workspace identity"),
        "effects": ("read external data", "create or modify only with action-time authority"),
    },
    "sentry": {
        "description": "Inspect Sentry projects, errors, traces and incidents.",
        "capabilities": ("error_monitoring",), "data": ("errors", "traces", "projects"),
        "effects": ("read external data", "external changes require action-time authority"),
    },
    "atlassian": {
        "description": "Use Jira issues and Confluence knowledge through Atlassian's remote MCP.",
        "capabilities": ("issue_tracking", "knowledge"),
        "data": ("issues", "projects", "pages", "workspace identity"),
        "effects": ("read external data", "create or modify only with action-time authority"),
    },
    "stripe": {
        "description": "Inspect and operate Stripe billing objects under explicit payment authority.",
        "capabilities": ("payments",), "data": ("customers", "payments", "billing records"),
        "effects": ("read external data", "money movement and refunds remain restricted"),
    },
    "hubspot": {
        "description": "Use HubSpot CRM contacts, companies, deals and engagement data.",
        "capabilities": ("crm",), "data": ("contacts", "companies", "deals"),
        "effects": ("read external data", "create, message or modify only with authority"),
    },
    "vercel": {
        "description": "Inspect and manage Vercel projects and deployments.",
        "capabilities": ("deployment",), "data": ("projects", "deployments", "domains"),
        "effects": ("read external data", "deploy or change configuration only with authority"),
    },
    "neon": {
        "description": "Work with Neon Postgres projects, branches and databases.",
        "capabilities": ("database",), "data": ("schemas", "queries", "project metadata"),
        "effects": ("read external data", "writes and destructive queries remain restricted"),
    },
    "github": {
        "description": "Use GitHub repositories, issues, pull requests and releases.",
        "capabilities": ("source_control",),
        "data": ("repositories", "issues", "pull requests", "account identity"),
        "effects": ("read external data", "push, merge, publish or modify only with authority"),
    },
    "comfy-cloud": {
        "description": "Search and run image, video, audio and 3D workflows through Comfy Cloud.",
        "capabilities": ("visual_ai",), "data": ("prompts", "workflows", "generated media"),
        "effects": ("read catalogs", "generation may consume provider credits"),
    },
    "eraser": {
        "description": "Create, read and update collaborative architecture diagrams through Eraser's official MCP.",
        "capabilities": ("diagramming",),
        "data": ("diagram prompts", "diagram code", "Eraser files and workspace identity"),
        "effects": ("read diagrams", "create or update diagrams only with action-time authority"),
    },
}


def _clean(value, limit=500):
    return " ".join(str(value or "").replace("\x00", " ").split())[:limit]


def infer_needs(goal) -> dict:
    """Map a private goal to allowlisted capability labels without returning the raw text."""
    text = _clean(goal, _MAX_GOAL).casefold()
    matched = []
    for key, profile in _NEEDS.items():
        hits = [pattern for pattern in profile["patterns"] if pattern.casefold() in text]
        if hits:
            matched.append({
                "id": key, "label": profile["label"], "strength": min(1.0, .55 + .15 * len(hits)),
                "registry_terms": list(profile["registry_terms"][:3]),
            })
    matched.sort(key=lambda row: (-row["strength"], row["id"]))
    terms = []
    for row in matched:
        for term in row["registry_terms"]:
            if term not in terms:
                terms.append(term)
    return {
        "needs": matched[:6], "registry_terms": terms[:8],
        "recognized": bool(matched), "raw_goal_shared": False,
    }


def _read_cache() -> dict:
    try:
        with open(_CACHE, encoding="utf-8") as handle:
            value = json.load(handle)
        if isinstance(value, dict) and value.get("schema_version") == 1:
            value.setdefault("queries", {})
            return value
    except (OSError, ValueError, TypeError):
        pass
    return {"schema_version": 1, "queries": {}}


def _write_cache(value) -> None:
    parent = os.path.dirname(_CACHE)
    os.makedirs(parent, exist_ok=True)
    try:
        from . import plat
        plat.chmod_private(parent)
    except Exception:
        pass
    temp = _CACHE + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        from . import plat
        plat.chmod_private(temp)
    except Exception:
        pass
    os.replace(temp, _CACHE)


def _registry_request(term: str) -> list:
    if term not in {item for profile in _NEEDS.values() for item in profile["registry_terms"]}:
        raise ValueError("registry search term is not in the local capability vocabulary")
    query = urllib.parse.urlencode({
        "search": term, "version": "latest", "limit": _MAX_RESULTS_PER_TERM,
    })
    request = urllib.request.Request(
        REGISTRY_API + "?" + query,
        headers={"Accept": "application/json", "User-Agent": "Collie-MCP-Discovery/1"},
    )
    with urllib.request.urlopen(request, timeout=12) as response:
        raw = response.read(_MAX_RESPONSE + 1)
    if len(raw) > _MAX_RESPONSE:
        raise RuntimeError("MCP Registry response exceeded the 2 MiB safety limit")
    value = json.loads(raw or b"{}")
    rows = value.get("servers") if isinstance(value, dict) else None
    return rows if isinstance(rows, list) else []


def _https_remote(server: dict) -> dict | None:
    for row in server.get("remotes") or []:
        if not isinstance(row, dict):
            continue
        url = _clean(row.get("url"), 2048)
        try:
            parsed = urllib.parse.urlsplit(url)
        except ValueError:
            continue
        if (parsed.scheme == "https" and parsed.hostname and not parsed.username
                and not parsed.password and row.get("type") in ("streamable-http", "sse")):
            return {"type": row.get("type"), "url": url}
    return None


def _packages(server: dict) -> list:
    out = []
    for row in server.get("packages") or []:
        if not isinstance(row, dict):
            continue
        kind, identifier, version = (_clean(row.get("registryType"), 40),
                                     _clean(row.get("identifier"), 500),
                                     _clean(row.get("version"), 100))
        if not kind or not identifier or not version:
            continue
        item = {"registry_type": kind, "identifier": identifier, "version": version}
        digest = _clean(row.get("fileSha256"), 128).lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest):
            item["sha256"] = digest
        out.append(item)
        if len(out) >= 6:
            break
    return out


def _normalize_registry_row(row) -> dict | None:
    if not isinstance(row, dict) or not isinstance(row.get("server"), dict):
        return None
    server = row["server"]
    name, version = _clean(server.get("name"), 240), _clean(server.get("version"), 100)
    if not name or not version:
        return None
    meta = row.get("_meta") if isinstance(row.get("_meta"), dict) else {}
    official = meta.get("io.modelcontextprotocol.registry/official")
    official = official if isinstance(official, dict) else {}
    if official.get("status") == "deleted":
        return None
    remote, packages = _https_remote(server), _packages(server)
    repo = server.get("repository") if isinstance(server.get("repository"), dict) else {}
    # Bind the review handle to the connection material, not just publisher name/version.  Registry
    # metadata is expected to be immutable per version, but this makes a cache refresh fail closed
    # even if a publisher mutates an endpoint behind the same version string.
    connection_material = json.dumps(
        {"remote": remote, "packages": packages}, ensure_ascii=True, sort_keys=True,
        separators=(",", ":"))
    fingerprint = hashlib.sha256(connection_material.encode()).hexdigest()[:12]
    return {
        "id": "registry:%s@%s#%s" % (name, version, fingerprint),
        "registry_name": name, "name": name, "label": _clean(server.get("title"), 160) or name,
        "version": version, "description": _clean(server.get("description"), 800),
        "source": "official_mcp_registry", "trust_level": "community_unreviewed",
        "trust_summary": REGISTRY_NOTICE, "repository": _clean(repo.get("url"), 1000),
        "website": _clean(server.get("websiteUrl"), 1000), "remote": remote,
        "packages": packages, "published_at": _clean(official.get("publishedAt"), 80),
        "updated_at": _clean(official.get("updatedAt"), 80),
        "installability": "review_and_connect" if remote else "review_only",
        "warnings": (["Unreviewed community listing; unknown tools remain external-write by default."]
                     + ([] if remote else [
                         "Local executable packages are not one-click installed without an enforceable sandbox.",
                     ])),
    }


def search_registry(terms, *, refresh=False, now=None) -> dict:
    """Search only allowlisted capability terms and cache public metadata locally."""
    now = int(time.time() if now is None else now)
    allowed = {item for profile in _NEEDS.values() for item in profile["registry_terms"]}
    clean_terms = []
    for term in terms or []:
        term = _clean(term, 60).casefold()
        if term in allowed and term not in clean_terms:
            clean_terms.append(term)
    cache, errors, records = _read_cache(), [], {}
    for term in clean_terms[:8]:
        entry = (cache.get("queries") or {}).get(term) or {}
        age = now - int(entry.get("fetched_at") or 0)
        fresh = 0 <= age <= _CACHE_TTL
        rows = entry.get("servers") if fresh and not refresh else None
        if not isinstance(rows, list):
            try:
                rows = _registry_request(term)
                cache["queries"][term] = {"fetched_at": now, "servers": rows}
            except Exception as exc:
                errors.append("%s: %s: %s" % (term, type(exc).__name__, _clean(exc, 240)))
                rows = entry.get("servers") if isinstance(entry.get("servers"), list) else []
        for raw in rows[:_MAX_RESULTS_PER_TERM]:
            item = _normalize_registry_row(raw)
            if item:
                records[item["id"]] = item
    if clean_terms:
        try:
            _write_cache(cache)
        except OSError as exc:
            errors.append("cache: %s" % _clean(exc, 240))
    return {"candidates": list(records.values()), "terms": clean_terms,
            "raw_goal_shared": False, "errors": errors}


def _configured_by_name() -> dict:
    try:
        from . import mcpclient
        return {row["name"]: row for row in mcpclient.status()}
    except Exception:
        return {}


def _configured_by_url() -> dict:
    try:
        from . import mcpclient
        configs, states = mcpclient._load_config(), _configured_by_name()
    except Exception:
        return {}
    return {str(cfg.get("url") or ""): states.get(name, {})
            for name, cfg in configs.items() if isinstance(cfg, dict) and cfg.get("url")}


def _state(row) -> str:
    if not row:
        return "not_connected"
    if row.get("enabled") is False:
        return "off"
    if row.get("auth") == "login-needed":
        return "sign_in_needed"
    return "ready"


def _curated_candidates(needs: list) -> list:
    from . import mcpclient
    configured = _configured_by_name()
    need_ids = [row["id"] for row in needs]
    scores = {}
    for rank, need in enumerate(need_ids):
        for service in _NEEDS[need]["services"]:
            scores[service] = scores.get(service, 0) + 100 - rank * 6
    # An explicit service name is already represented by its need; no generic fallback that happens
    # to sort first. Unknown goals should remain unknown instead of manufacturing a recommendation.
    out = []
    for service, score in scores.items():
        catalog = mcpclient.CATALOG.get(service)
        profile = _CURATED.get(service)
        if not catalog or not profile:
            continue
        matched = [item for item in profile["capabilities"] if item in need_ids]
        row = configured.get(service)
        out.append({
            "id": "curated:" + service, "name": service, "label": catalog["label"],
            "description": profile["description"], "source": "collie_curated",
            "trust_level": "collie_verified_endpoint",
            "trust_summary": (
                "Collie pins this provider endpoint and has exercised its MCP/OAuth handshake. "
                "That is endpoint provenance, not a guarantee about every tool or provider policy."
            ),
            "capabilities": list(profile["capabilities"]),
            "reason": "Matches: " + ", ".join(_NEEDS[item]["label"] for item in matched),
            "data": list(profile["data"]), "effects": list(profile["effects"]),
            "remote": {"type": "streamable-http", "url": catalog["url"]},
            "installability": "guided_setup" if catalog.get("byo_client") else "one_click_oauth",
            "connection_state": _state(row), "configured": bool(row), "score": score + 30,
            "warnings": (["Provider may still require a pre-registered OAuth client."]
                         if catalog.get("byo_client") else []),
        })
    return out


def _registry_matches(item: dict, needs: list) -> list:
    haystack = " ".join((item.get("registry_name", ""), item.get("label", ""),
                         item.get("description", ""))).casefold()
    matches = []
    for need in needs:
        profile = _NEEDS[need["id"]]
        if (any(term.casefold() in haystack for term in profile["registry_terms"])
                or any(pattern.casefold() in haystack for pattern in profile["patterns"]
                       if len(pattern) >= 4)):
            matches.append(need)
    return matches


def _registry_score(item: dict, needs: list) -> int:
    haystack = " ".join((item.get("registry_name", ""), item.get("label", ""),
                         item.get("description", ""))).casefold()
    score = 0
    for rank, need in enumerate(needs):
        profile = _NEEDS[need["id"]]
        if any(term.casefold() in haystack for term in profile["registry_terms"]):
            score += 45 - rank * 3
        if any(pattern.casefold() in haystack for pattern in profile["patterns"]
               if len(pattern) >= 4):
            score += 18
    if item.get("remote"):
        score += 14
    if item.get("repository"):
        score += 5
    return score


def recommend(goal, *, include_registry=False, refresh=False, available_tools=None,
              max_results=5) -> dict:
    """Return ranked connection proposals. This function never connects or installs anything."""
    intent = infer_needs(goal)
    needs = intent["needs"]
    candidates = _curated_candidates(needs)
    registry_result = {"candidates": [], "terms": [], "errors": []}
    if include_registry and intent["registry_terms"]:
        registry_result = search_registry(intent["registry_terms"], refresh=refresh)
        by_url = _configured_by_url()
        for item in registry_result["candidates"]:
            matches = _registry_matches(item, needs)
            # The Registry's `search` parameter is deliberately broad.  Do not present an entry
            # merely because it appeared in a response if its bounded public metadata does not
            # actually contain one of the locally inferred generic capability terms.
            if not matches:
                continue
            item["connection_state"] = _state(by_url.get((item.get("remote") or {}).get("url", "")))
            item["configured"] = item["connection_state"] != "not_connected"
            item["capabilities"] = [need["id"] for need in matches]
            item["reason"] = "Public Registry match for: " + ", ".join(
                need["label"] for need in matches)
            item["data"] = ["Declared by the server only; inspect its OAuth screen and tools/list."]
            item["effects"] = ["Unknown tools remain external-write until a host-owned review."]
            item["score"] = _registry_score(item, needs)
            candidates.append(item)
    # Existing MCP tools are evidence that the capability may already be present in this session.
    names = [str(name) for name in (available_tools or [])]
    existing = sorted(name for name in names if name.startswith("mcp__"))
    candidates.sort(key=lambda row: (
        0 if row.get("connection_state") == "ready" else 1,
        -int(row.get("score") or 0), row.get("label", "").casefold()))
    selected, seen = [], set()
    for row in candidates:
        key = (row.get("remote") or {}).get("url") or row.get("id")
        if key in seen:
            continue
        seen.add(key); selected.append(row)
        if len(selected) >= max(1, min(int(max_results or 5), 10)):
            break
    return {
        "schema_version": 1, "recognized": intent["recognized"], "needs": needs,
        "recommendations": selected, "existing_mcp_tools": existing[:40],
        "registry_searched": bool(include_registry), "registry_terms": registry_result.get("terms", []),
        "registry_errors": registry_result.get("errors", []), "raw_goal_shared": False,
        "connection_made": False,
    }


def cached_candidate(candidate_id, *, now=None) -> dict | None:
    """Resolve an exact public candidate from local cache; never trust a client-supplied URL."""
    wanted = _clean(candidate_id, 500)
    if not wanted.startswith("registry:"):
        return None
    now = int(time.time() if now is None else now)
    cache = _read_cache()
    for entry in (cache.get("queries") or {}).values():
        age = now - int(entry.get("fetched_at") or 0)
        if not 0 <= age <= _CACHE_TTL:
            continue
        for raw in entry.get("servers") or []:
            item = _normalize_registry_row(raw)
            if item and item["id"] == wanted:
                return item
    return None


def candidate_config_name(candidate: dict) -> str:
    """Stable, collision-resistant local name for a registry server."""
    source = _clean(candidate.get("registry_name") or candidate.get("name"), 240).casefold()
    tail = source.rsplit("/", 1)[-1]
    slug = re.sub(r"[^a-z0-9_-]+", "-", tail).strip("-_")[:36] or "community"
    return "registry-%s-%s" % (slug, hashlib.sha256(source.encode()).hexdigest()[:7])


def status() -> dict:
    cache = _read_cache()
    entries = list((cache.get("queries") or {}).values())
    newest = max((int(row.get("fetched_at") or 0) for row in entries), default=0)
    return {
        "registry": REGISTRY_BASE, "cache_path": _CACHE, "cached_queries": len(entries),
        "last_refresh_at": newest or None, "cache_ttl_seconds": _CACHE_TTL,
        "raw_goal_shared": False, "moderation": "minimal; community results are unreviewed",
    }
