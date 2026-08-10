"""Regression contracts for the security, cancellation, and landing-page UI fixes."""

import base64
import hashlib
import json
import re
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_every_run_surface_uses_the_server_cancel_contract():
    for name in ("index.html", "mobile.html", "ambient.html", "wallpaper.html"):
        page = read(f"harness/webui/{name}")
        assert "/api/run/cancel" in page, name
        assert re.search(r"method\s*:\s*[\"']POST[\"']", page), name
        assert re.search(r"JSON\.stringify\(\{\s*session\s*:\s*[^}]+,\s*run\s*:\s*[^}]+\}\)", page), name
        assert (re.search(r"addEventListener\([\"']start[\"']", page) or '["start"' in page), name
        assert re.search(r"d\.run|data\.run", page), name
        assert "/api/runs" in page, name


def test_mobile_steer_and_zoom_contracts():
    page = read("harness/webui/mobile.html")
    assert "maximum-scale" not in page
    assert "JSON.stringify({session:currentSession,q:qv})" in page
    assert "JSON.stringify({session:currentSession,text:qv})" not in page
    assert "externalRunning" in page and "Steer not delivered" in page


def test_ecosystem_shell_exposes_missions_pack_library_and_global_approvals():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")
    ambient = read("harness/webui/ambient.html")
    server = read("harness/webapp.py")

    for node in ("navHome", "navMissions", "navPack", "navLibrary", "navActivity", "needsYouNav"):
        assert f'id="{node}"' in desktop
    assert 'data-fill="/mission "' in desktop
    assert 'id="slashMenu"' in desktop and 'data-command="/mission --review "' in desktop
    assert "function updateSlashMenu()" in desktop and "chooseSlash(opts[slashIndex])" in desktop
    assert '"MISSION_APPROVAL_MODE"' in desktop
    assert "/api/whoami" in desktop and "data-collie-name" in desktop
    assert "Returns scoped evidence" in desktop and "Proves its work" not in desktop
    assert "GLOBAL_PERMS = {}" in desktop
    assert 'var permissionLive = new EventSource("/api/live")' in desktop
    assert 'permissionLive.addEventListener("permission_resolved"' in desktop
    assert "if (!PENDING_PERMS[d.id]) return" in desktop
    assert 'settle("sending…")' not in desktop

    assert "/api/approve" in mobile
    assert '"permission","permission_resolved","done"' in mobile
    assert 'live.addEventListener("permission"' in ambient
    assert 'live.addEventListener("permission_resolved"' in ambient
    assert 'Handler._live_pub("permission"' in server
    assert 'cls._mirror_pub(sid, "permission_resolved"' in server


def test_library_is_a_real_digest_and_authority_lifecycle_surface():
    desktop = read("harness/webui/index.html")
    server = read("harness/webapp.py")

    assert 'id="libraryPanel"' in desktop and 'id="libraryGrid"' in desktop
    library_click = desktop.split('$("navLibrary").onclick', 1)[1].split(";", 2)[:2]
    assert "setLibraryOpen(true)" in ";".join(library_click)
    assert '$("navLibrary").onclick = function () { setProductNav("library"); openSettings' not in desktop
    for value in ("/api/library", "/api/library/action", "SHA-256 digest", "Declared authority",
                  "Scope change", "integrity_ok", "rollback_version"):
        assert value in desktop or value in server
    assert 'approve: !version.approved' in desktop
    assert 'window.confirm(libraryApprovalText' in desktop
    assert 'force=False' in server and 'force=True' not in server.split('if path == "/api/library/action"', 1)[1].split('if path ', 1)[0]


def test_pack_page_reports_operational_state_without_inventing_device_presence():
    page = read("harness/webui/remote.html")

    assert "<title>Pack — Collie</title>" in page and 'id="packcard"' in page
    for endpoint in ("/api/whoami", "/api/run-capabilities", "/api/healthz",
                     "/api/activity", "/api/approvals", "/api/remote/status"):
        assert endpoint in page
    assert 'id="packmembers"' in page and 'id="packassignments"' in page
    assert "Worker freshness is reported by local heartbeats" in page
    assert "Paired · live reachability not reported" in page
    assert 'colspan="4"' in page and "pendingError(" in page


def test_dedicated_surfaces_expose_safe_activity_and_recovery_controls():
    pages = {
        name: read(f"harness/webui/{name}.html")
        for name in ("mobile", "remote", "ambient")
    }

    for name, page in pages.items():
        for endpoint in ("/api/activity", "/api/healthz", "/api/recovery/reconcile",
                         "/api/mission/specialist/steer",
                         "/api/mission/specialist/cancel"):
            assert endpoint in page, (name, endpoint)
        assert "confirmed:true" in page, name
        assert "Inspect the external system" in page, name
        assert "not_fired" in page and (
            'resolution:"cancel"' in page or "resolution:'cancel'" in page), name
        assert "活动与恢复" in page and "活動與復原" in page, name

    assert 'tOps("Service")' in pages["ambient"]
    assert '"Describe the outcome you want…":"描述你想完成的结果…"' in pages["ambient"]
    assert '"Service":"服务"' in pages["remote"]

    # The dedicated operations renderers consume only the server's allowlisted lifecycle fields.
    blocks = {
        "mobile": pages["mobile"].split("// ---- authenticated Activity", 1)[1].split(
            "// Independent run controls", 1)[0],
        "remote": pages["remote"].split("function activityStateLabel", 1)[1].split(
            "function renderPack", 1)[0],
        "ambient": pages["ambient"].split("// ── compact Activity", 1)[1].split(
            "// ── in-page music", 1)[0],
    }
    for name, block in blocks.items():
        for private_field in ("data.task", "data.result", "data.workspace", "data.resources",
                              "data.leash", "data.args", "data.prompt", "data.messages"):
            assert private_field not in block, (name, private_field)


def test_mobile_and_remote_render_compact_parent_child_specialist_trees():
    mobile = read("harness/webui/mobile.html")
    remote = read("harness/webui/remote.html")

    assert 'id="mobileRunTree"' in mobile and "function mobileWalkTree" in mobile
    assert "row.parent_run_id" in mobile and "--tree-indent" in mobile
    assert 'id="remoteRunTree"' in remote and "function walkActivityTree" in remote
    assert "row.parent_run_id" in remote and "--tree-indent" in remote
    for page in (mobile, remote):
        assert "Only lifecycle metadata is shown; task content and tool arguments stay private." in page
        assert "Steer this specialist at its next safe boundary:" in page
        assert "Request specialist cancellation?" in page


def test_approval_snapshots_recover_after_refresh_without_stale_resurrection():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")
    ambient = read("harness/webui/ambient.html")
    server = read("harness/webapp.py")

    assert 'path == "/api/approvals"' in server and "_inbox_pending_all" in server
    assert "PERMISSION_EPOCH" in desktop and "requestEpoch !== PERMISSION_EPOCH" in desktop
    assert "approvalEpoch" in mobile and "requestEpoch!==approvalEpoch" in mobile
    assert "opsApprovalEpoch" in ambient and "requestEpoch!==opsApprovalEpoch" in ambient


def test_desktop_language_updates_the_document_accessibility_metadata():
    desktop = read("harness/webui/index.html")
    assert "document.documentElement.lang = UI_LANG" in desktop
    assert "UI_LANG = resolveLang" in desktop and "applyLang();" in desktop


def test_missions_activity_and_settings_do_not_overstate_success_or_hide_failures():
    desktop = read("harness/webui/index.html")

    assert 'done_verified:"Verified against contract"' in desktop
    assert 'done_accepted:"Completed without independent verification"' in desktop
    assert "Completed by user acceptance; no independent verification was recorded." in desktop
    accepted_css = re.search(r"\.mchip\.state-done_accepted\s*\{([^}]+)\}", desktop)
    assert accepted_css and "--meadow" not in accepted_css.group(1)
    assert "function missionResponse" in desktop and "if (!r.ok) throw new Error" in desktop
    assert "missionError(card" in desktop and "Activity could not refresh" in desktop
    assert 'fetch("/api/healthz?token=" + encodeURIComponent(CT))' in desktop


def test_visual_run_views_name_scoped_checks_without_universal_verification_claims():
    wallpaper = read("harness/webui/wallpaper.html")
    explorer = read("harness/webui/map.html")

    assert 'd.passed?"Check passed":"Check failed"' in wallpaper
    assert 'd.cmd||"executed check"' in wallpaper
    assert "✓ Verified" not in wallpaper
    assert "GATE · VERIFIED" not in explorer
    assert '"CHECK · "+' in explorer and '"RECORDED"' in explorer
    assert 'd.cmd||"Executed check"' in explorer


def test_settings_autosave_is_per_key_flushable_and_truthful():
    desktop = read("harness/webui/index.html")
    server = read("harness/webapp.py")

    assert 'id="setCancel" hidden' in desktop
    assert 'data-i18n="Changes save automatically."' in desktop
    assert "function flushPendingSettings()" in desktop
    assert "function requestCloseSettings()" in desktop
    assert "payload[key] = value" in desktop
    assert "JSON.stringify(payload)" in desktop
    assert "JSON.stringify(vals)" not in desktop
    assert 'label: "Brains & routing"' in desktop
    assert 'label: "Desktop & devices"' in desktop
    assert 'label: "Privacy & security"' in desktop

    wallpaper_block = server.split('if "WALLPAPER" in body:', 1)[1].split(
        'return self._send_json({"ok": True', 1
    )[0]
    assert 'except Exception as exc:' in wallpaper_block
    assert 'settings.update({"WALLPAPER": "on" if prev_wp else "off"})' in wallpaper_block
    assert '"ok": False' in wallpaper_block


def test_run_setup_is_orthogonal_accessible_and_available_on_mobile():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")

    assert desktop.count('role="radiogroup"') == 7
    assert desktop.count('role="radio"') == 19
    assert 'data-i18n-aria-label="Run setup"' in desktop
    assert "choose(axis, target.getAttribute(\"data-val\"))" in desktop
    assert "it.tabIndex = on && !it.disabled ? 0 : -1" in desktop

    for field in ("mIntent", "mQuality", "mVerification", "mWorkspace", "mStrategy"):
        assert f'id="{field}"' in mobile
    for query in ("&intent=", "&quality=", "&verification=", "&workspace=", "&strategy="):
        assert query in mobile
    assert "&mode=normal" not in mobile
    assert 'id="mPackCheck"' in mobile and "check.reportValidity()" in mobile
    assert "Number.isInteger(n)" in desktop and "Number.isInteger(n)" in mobile
    assert "Attempts must be a whole number from 2 to 6." in desktop
    assert "Attempts must be a whole number from 2 to 6." in mobile
    assert "&check=" in mobile and "&apply=1" in mobile
    assert '"pack_start","pack_attempt"' in mobile


def test_pack_terminal_verdicts_keep_candidate_evidence():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")

    assert "if(curMsg && !d.pack)curMsg.remove()" in desktop
    assert "Pack finished with an error" in desktop
    assert "apply failed — winner was not written" in desktop
    assert 'sum.classList.add(d.canceled ? "warn" : "fail")' in desktop
    assert "attempts.forEach(drawPackAttempt)" in desktop
    assert "wa.check_pass === true" in desktop
    assert 'var winnerWhy = d.reason || (applyFailed ? ""' in desktop
    assert 'id="pkrow' not in desktop
    assert 'd.canceled ? "stop" : "fail"' in desktop
    assert "if(data.pack)packDone(data)" in mobile
    assert "No winner" in mobile and "Pack stopped" in mobile
    assert "terminalWasHandled" in mobile and "lastTerminalRun" in mobile


def test_run_configuration_is_snapshotted_and_mobile_drawer_is_modal():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")

    assert "var runConfig = readRunConfig(), runSession = currentSession" in desktop
    assert "runStream(q, imgs, runConfig, runSession, userMsgEl)" in desktop
    assert "if (thisLaunch !== streamLaunchToken || !running) return" in desktop
    assert "if (routePending) return" in desktop
    assert 'typeof d.id !== "string"' in desktop
    assert "Image upload failed — no run was started." in desktop
    assert "attached = imgs.slice(); renderAttached()" in desktop
    assert ".catch(function () { launch([]); })" not in desktop

    assert 'role="dialog" aria-modal="true"' in mobile
    assert 'aria-hidden="true" inert' in mobile
    assert "removeAttribute('inert')" in mobile and "setAttribute('inert','')" in mobile
    assert "detachActive()" in mobile and "navigationToken" in mobile


def test_new_run_ui_text_has_chinese_and_traditional_chinese_variants():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")

    assert '"Run setup": "运行设置"' in desktop
    assert '"Run setup": "執行設定"' in desktop
    assert '"running {n} attempts…": "正在运行 {n} 个尝试…"' in desktop
    assert '"running {n} attempts…": "正在執行 {n} 個嘗試…"' in desktop
    assert "var ZHTW=" in mobile and '"Required check command":"必填檢查命令"' in mobile


def test_untrusted_map_and_wallpaper_labels_are_text_not_markup():
    map_page = read("harness/webui/map.html")
    wallpaper = read("harness/webui/wallpaper.html")
    assert 'hlab.textContent=String(hv.f.p||"")' in map_page
    assert 'hlab.textContent=String(hv.f.p||"")' in wallpaper
    assert 'sel.innerHTML' not in map_page
    assert 'typeof THREE==="undefined"' in map_page
    assert 'id="fileSearch"' in map_page and 'id="fileList"' in map_page
    assert "function safeHttpUrl" in wallpaper
    assert 'replace(/[&<>"\']/g' in wallpaper
    assert 'rel="noopener noreferrer"' in wallpaper


def test_desktop_dialogs_and_dynamic_model_count_are_accessible():
    page = read("harness/webui/index.html")
    assert "function dialogOpened" in page and "function dialogClosed" in page
    assert 'event.key !== "Tab"' in page
    assert "modelStatus.textContent = optionCount" in page
    assert 'role="switch"' in page and 'aria-checked="true"' in page
    steer_catch = page.split('fetch("/api/steer?', 1)[1].split("function send()", 1)[0]
    assert 'classList.add("dropped")' in steer_catch and "Steer not delivered" in steer_catch


def test_only_complete_ui_languages_are_selectable():
    page = read("harness/webui/index.html")
    settings = read("harness/settings.py")
    assert 'var SUPPORTED = ["en", "zh-tw", "zh"]' in page
    language_block = settings.split('"key": "LANG"', 1)[1].split("],", 1)[0]
    assert all(code in language_block for code in ('"auto"', '"en"', '"zh"', '"zh-tw"'))
    assert '"es"' not in language_block


def test_landing_has_no_passive_tracking_and_has_disclosure_and_a11y():
    page = (ROOT / "landing/index.html").read_bytes().decode("utf-8")
    lowered = page.lower()
    assert "cloudflareinsights" not in lowered and "googletagmanager" not in lowered
    assert "api.github.com" not in lowered
    assert 'id="askDisclosure"' in page and 'maxlength="1000"' in page
    assert 'role="tab"' in page and 'e.key==="ArrowRight"' in page
    assert 'localStorage.setItem("collie-theme"' in page
    assert "maximum-scale" not in page
    assert "successfulQuestions++" in page and "successfulQuestions>=MAX_MSGS" in page
    assert "new AbortController()" in page and "controller.abort()" in page
    assert 'typeof d.error==="string"' in page and 'typeof d.reply==="string"' in page


def test_landing_verification_copy_and_download_metadata_are_truthful():
    page = read("landing/index.html")
    chat = read("landing/functions/api/chat.js")

    assert "Verification you control" in page
    assert "Auto asks for a relevant check after edits" in page
    assert "Required makes an executed passing assertion a hard finish gate" in page
    assert "Proves its work" not in page
    assert "latest release" in page and "48 MB" not in page and "137 MB" not in page
    assert "a single line on macOS and Linux" not in page
    assert "Packaged installers for Windows and Apple-silicon Macs" in page
    assert "Your files stay with you." not in page
    assert "Task context goes only to the model provider you choose" in page
    assert "Do not claim that Auto has this hard-gate guarantee" in chat
    assert "--faint:#7B8395" in page and "--faint:#636B7A" in page


def test_readme_surfaces_table_is_contiguous():
    page = read("README.md")
    table_start = page.index("| Surface | Command | Reaches |")
    table_end = page.index("\n\n", table_start)
    assert "| **Streaming / CI** |" in page[table_start:table_end]


def test_landing_build_is_an_explicit_allowlist_and_rate_limit_is_atomic():
    package = json.loads(read("landing/package.json"))
    build = read("landing/build.mjs")
    config = read("landing/wrangler.toml")
    chat = read("landing/functions/api/chat.js")
    assert package["scripts"]["build"] == "node build.mjs"
    assert "publicFiles" in build and '"_headers"' in build and "index.draft.html" not in build and "_preview.html" not in build
    assert 'pages_build_output_dir = "dist"' in config
    assert "RATE_LIMITER" in config and "durable_objects.bindings" in config and "kv_namespaces" not in config
    assert "...parsed.history" in chat and "MAX_HISTORY_MESSAGES = 6" in chat
    assert "fails closed" in chat and "MAX_BODY_BYTES" in chat


def test_landing_has_local_privacy_and_404_pages():
    privacy = read("landing/privacy.html")
    not_found = read("landing/404.html")
    assert "up to six recent messages" in privacy
    assert "end-to-end encrypted" in privacy and "routing metadata" in privacy
    assert "does not write questions or answers to R2, KV, or Durable Object content storage" in privacy
    assert "developers.cloudflare.com/workers-ai/platform/data-usage/" in privacy
    assert 'meta name="robots" content="noindex"' in not_found


class _InlineHandlerParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.handlers = []

    def handle_starttag(self, tag, attrs):
        self.handlers.extend((tag, key) for key, _ in attrs if key.lower().startswith("on"))


def test_strict_csp_has_no_inline_event_handlers():
    for path in list((ROOT / "harness/webui").glob("*.html")) + list((ROOT / "landing").glob("*.html")):
        parser = _InlineHandlerParser()
        parser.feed(path.read_text(encoding="utf-8"))
        assert not parser.handlers, f"{path.name}: {parser.handlers}"


class _ButtonTypeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.missing = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() != "button":
            return
        values = dict(attrs)
        if not values.get("type"):
            self.missing.append(values.get("id") or values.get("class") or "<button>")


def test_companion_surfaces_keep_product_positioning_and_localized_a11y_copy():
    mobile = read("harness/webui/mobile.html")
    ambient = read("harness/webui/ambient.html")
    remote = read("harness/webui/remote.html")

    for page in (mobile, ambient):
        assert "execution system" not in page.lower()
        assert "operations system" in page.lower()
    assert '"Personal AI operations system":"个人 AI 运营系统"' in mobile
    assert '"Personal AI operations system":"個人 AI 營運系統"' in mobile
    assert '"your AI operations system":"你的 AI 运营系统"' in ambient
    assert '"your AI operations system":"你的 AI 營運系統"' in ambient

    assert '<div class="reply" id="reply"><button' in ambient
    assert 'id="replyBody" role="status" aria-live="polite"' in ambient
    assert '<div class="reply" id="reply" role="status"' not in ambient
    assert 'b.setAttribute("data-ops-open-name",app.label||"")' in ambient
    assert 'document.querySelectorAll("[data-ops-open-name]")' in ambient
    assert 'b.setAttribute("data-ops-title",labels[0])' in ambient
    assert 'b.setAttribute("data-ops-aria",labels[1])' in ambient
    assert 'document.querySelectorAll("[data-ops-title]")' in ambient
    assert 'function opsLocale(){return OPS_LANG==="zh-tw"?"zh-TW":(OPS_LANG==="zh"?"zh-CN":"en");}' in ambient
    assert 'new Intl.DateTimeFormat(opsLocale()' in ambient
    assert 'd.toLocaleDateString(opsLocale()' in ambient

    assert 'data-i-aria="Pending pairing request"' in remote
    assert 'document.querySelectorAll("[data-i-aria]")' in remote
    assert 'data-i="Check this matches the number on the phone."' in remote
    assert '"Pending pairing request":"待處理的配對請求"' in remote
    assert "t('Some Pack state is unavailable')" in remote
    assert "t('Assignments unavailable.')" in remote
    assert "activityStateLabel(row.state)" in remote
    assert "missing:'Not running'" in remote
    assert '"Not running":"未运行"' in remote
    assert '"Not running":"未執行"' in remote


def test_dedicated_companion_surfaces_use_explicit_static_button_types():
    for path in (
        "harness/webui/mobile.html",
        "harness/webui/remote.html",
        "harness/webui/ambient.html",
        "landing/index.html",
    ):
        parser = _ButtonTypeParser()
        parser.feed(read(path))
        assert not parser.missing, f"{path}: buttons missing type: {parser.missing}"


def test_landing_csp_template_and_builder_bind_the_exact_inline_scripts():
    page = (ROOT / "landing/index.html").read_bytes().decode("utf-8")
    headers = read("landing/_headers")
    build = read("landing/build.mjs")
    scripts = re.findall(r"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", page,
                         flags=re.IGNORECASE | re.DOTALL)
    assert scripts
    assert headers.count("__COLLIE_INLINE_SCRIPT_HASHES__") == 1
    assert 'script.replace(/\\r\\n?/g, "\\n")' in build
    assert 'createHash("sha256").update(browserText, "utf8").digest("base64")' in build
    assert 'headerTemplate.replace(cspPlaceholder, hashes.join(" "))' in build
    assert "frame-ancestors 'none'" in headers and "object-src 'none'" in headers
    assert "base-uri 'none'" in headers and "X-Content-Type-Options: nosniff" in headers


def test_local_server_builds_per_document_csp_hashes():
    from harness.webapp import Handler

    page = read("harness/webui/index.html").encode()
    policy = Handler._html_csp(page)
    scripts = re.findall(br"<script\b(?![^>]*\bsrc\s*=)[^>]*>(.*?)</script\s*>", page,
                         flags=re.IGNORECASE | re.DOTALL)
    assert scripts and "script-src 'self'" in policy
    for script in scripts:
        normalized = script.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        digest = base64.b64encode(hashlib.sha256(normalized).digest()).decode()
        assert f"'sha256-{digest}'" in policy
    assert "frame-ancestors 'self'" in policy and "base-uri 'none'" in policy
