"""Regression contracts for the security, cancellation, and landing-page UI fixes."""

import base64
import hashlib
import json
import re
from html import unescape
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_first_party_surfaces_share_the_calm_personal_os_contract():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")
    remote = read("harness/webui/remote.html")
    ambient = read("harness/webui/ambient.html")
    wallpaper = read("harness/webui/wallpaper.html")
    explorer = read("harness/webui/map.html")

    assert 'data-ui="calm-os"' in desktop
    assert 'id="topbarMore"' in desktop and 'class="topbar-tools"' in desktop
    assert 'id="modeClose"' in desktop and 'class="mode-menu-head"' in desktop
    assert 'get("preview") === "onboarding"' in desktop
    assert "grid-template-columns:244px" in desktop
    assert 'class="product-mark"' in desktop and 'class="wc-avatar"' in desktop
    assert 'id="nativeWindowControls"' in desktop and 'data-window-action="drag"' not in desktop
    assert 'requestNativeWindow("drag")' in desktop and 'body.home-idle .composer-box' in desktop
    assert 'id="nativeMaximize"' in desktop and 'class="restore-icon"' in desktop
    assert 'data.type === "window-state"' in desktop and 'native-maximized' in desktop
    assert 'function libraryCardVisual' in desktop and 'className = "capability-card kind-" + kind' in desktop
    assert 'function recentThreadGroup' in desktop and '"Your recent work will appear here."' in desktop
    native_host = read("harness/wallpaper/Program.cs")
    assert "FormBorderStyle.None" in native_host and '"native_shell=1"' in native_host
    assert "WM_NCHITTEST" in native_host and '\\"action\\":\\"maximize\\"' in native_host
    assert "_restoreBounds = Bounds" in native_host and "Bounds = screen.WorkingArea" in native_host
    assert "MaximizedBounds" not in native_host and "_customMaximized" in native_host
    assert 'EventWaitHandle.OpenExisting("collie-wallpaper-show-window")' in native_host
    assert "void WakeWindow()" in native_host and "SetForegroundWindow(Handle)" in native_host
    assert "PostWebMessageAsJson" in native_host and '\\"window-state\\"' in native_host
    assert "--bg:#F3F2EE" in desktop and "--pine:#225D4B" in desktop
    assert "--bg:#F6F6F3" in mobile and "--bg:#F6F6F3" in remote
    assert "Quiet ambient mode" in ambient and "Quiet visual run view" in wallpaper
    assert "The map stays immersive" in explorer


def test_desktop_defaults_to_plain_language_and_progressively_discloses_advanced_surfaces():
    desktop = read("harness/webui/index.html")

    # Home is a personal Today brief; the outcome launcher belongs to New task.
    assert 'id="newChat"' in desktop
    assert re.search(r'id="navHome"(?!\s+hidden)', desktop)
    assert 'id="todayDashboard"' in desktop and 'data-i18n="Today"' in desktop
    for node in ("todayGreeting", "todayTimeline", "todayAttention", "todayBrief", "todaySuggestion"):
        assert f'id="{node}"' in desktop
    assert 'todayJson("/api/personal")' in desktop
    assert 'todayJson("/api/missions")' in desktop
    assert 'todayJson("/api/approvals")' in desktop
    assert 'todayJson("/api/procedures?limit=20")' in desktop
    assert 'todayJson("/api/meetings/schedule")' in desktop
    assert 'document.visibilityState === "visible"' in desktop and "TODAY_VISIBLE_REFRESH_MS = 60000" in desktop
    assert 'setProductNav("today")' in desktop and 'showToday(true)' in desktop
    assert 'function returnToPrimarySurface()' in desktop and 'prepareTaskCanvas();' in desktop
    assert 'timeline.slice(0,3)' in desktop and 'attention.slice(0,3)' in desktop
    assert 'list = list.slice(0, 6)' in desktop
    assert 'id="sideMore"' in desktop
    more = desktop.split('id="sideMore"', 1)[1].split('</details>', 1)[0]
    for node in ("navPack", "navOnline", "navActivity"):
        assert f'id="{node}"' in more
    for label in ("Tasks", "Apps & connections", "Devices & team", "Sync & cloud"):
        assert f'data-i18n="{label}"' in desktop
    assert 'data-i18n="Loading tasks…"' in desktop
    assert 'data-i18n-aria-label="Close Apps & connections"' in desktop
    task_panel = desktop.split('id="missionsPanel"', 1)[1].split('</section>', 1)[0]
    assert "missions" not in re.sub(r'id="missions[^\"]*"|class="[^\"]*missions[^\"]*"', "", task_panel).lower()

    # The empty state speaks in user outcomes. Internal routing and proof nouns stay available
    # after work begins, but are not prerequisites for submitting the first request.
    welcome = desktop.split('id="welcome"', 1)[1].split('</div>\n      </div>\n    </div>', 1)[0]
    assert "Describe the outcome." in welcome
    for example in ("Organize my day", "Work across my apps", "Research a decision", "Build or fix something"):
        assert example in welcome
    for jargon in ("brain, tools, skills and workers", 'data-fill="/mission "'):
        assert jargon not in welcome
    assert '.gate[data-state="idle"] { display:none; }' in desktop
    assert '#statePill:not(.live) { display:none; }' in desktop

    # Model choice and system surfaces still exist, one layer down.
    tools = desktop.split('class="topbar-tools"', 1)[1].split('</div>\n      </details>', 1)[0]
    assert 'id="modelTrigger"' in tools
    assert '["pack", "online", "activity"].indexOf(id)' in desktop

    # Extension internals are behind an advanced disclosure; the default path starts from intent.
    add_panel = desktop.split('id="libraryAddPanel"', 1)[1].split('</div>\n        <div class="library-summary"', 1)[0]
    default_path, advanced = add_panel.split('data-i18n="Other ways to extend Collie"', 1)
    assert 'id="libraryConnectionForm"' in default_path and "Find matching apps" in default_path
    assert "Custom remote MCP" not in default_path and "Create a local Skill" not in default_path
    assert "Custom remote MCP" in advanced and "Create a local Skill" in advanced
    assert 'loc.textContent = t("On this computer")' in desktop


def test_personal_history_scanning_is_event_coalesced_and_bounded():
    background = read("harness/browser_ext/background.js")
    assert 'periodInMinutes: 30' in background
    assert 'chrome.runtime.onStartup.addListener' in background
    assert 'chrome.history.onVisited.addListener' in background
    assert 'colliePersonalHistorySoon' in background and 'delayInMinutes: 5' in background
    assert 'if (!alarm) chrome.alarms.create' in background
    assert 'if (!enabled || !granted || !chrome.history)' in background


def test_library_and_activity_replace_the_conversation_instead_of_stacking():
    desktop = read("harness/webui/index.html")
    activity = desktop.split("function setActivityOpen(open)", 1)[1].split("if (activityButton)", 1)[0]
    library = desktop.split("function setLibraryOpen(open)", 1)[1].split(
        'if ($("libraryRefresh"))', 1)[0]

    for block in (activity, library):
        assert '$("scroll").hidden = !!open' in block
        assert '$("composer").hidden = !!open' in block
        assert '$("scrollDownBtn").hidden = !!open' in block
    assert library.index("setActivityOpen(false)") < library.index("libraryPanel.hidden = !open")


def test_run_details_is_a_bounded_closeable_drawer_not_a_conversation_blocker():
    desktop = read("harness/webui/index.html")

    assert 'id="workpanelClose"' in desktop
    workpanel_css = re.search(r"\.workpanel\s*\{([^}]+)\}", desktop)
    assert workpanel_css
    assert "height: min(38vh, 360px)" in workpanel_css.group(1)
    assert "overflow: auto" in workpanel_css.group(1)
    assert '$("workpanelClose").addEventListener("click"' in desktop
    assert 'more.open = false' in desktop
    assert 't("Allow once")' in desktop and 't("Allow for this run")' in desktop
    assert 'title="\' + esc(d.rule_offer)' in desktop


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


def test_dedicated_surfaces_coerce_event_values_and_mobile_workers_fail_closed():
    mobile = read("harness/webui/mobile.html")
    remote = read("harness/webui/remote.html")

    for name, page in (("mobile", mobile), ("remote", remote)):
        assert re.search(r"function esc\(s\)\{ return String\(s==null\?", page), name
        assert "&quot;" in page and "&#39;" in page, name

    assert 'available=!explicitExternal||!!(probe&&probe.runnable)' in mobile
    assert 'row.dataset.available="false";row.disabled=true' in mobile
    assert 'if(row.selected){$("mRunner").value=""' in mobile


def test_ecosystem_shell_exposes_missions_pack_library_and_global_approvals():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")
    ambient = read("harness/webui/ambient.html")
    server = read("harness/webapp.py")

    for node in ("navHome", "navMissions", "navPack", "navOnline", "navLibrary", "navActivity", "needsYouNav"):
        assert f'id="{node}"' in desktop
    assert 'class="wc-card" data-fill="/mission "' not in desktop
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
    assert '$("navOnline").onclick = openOnlinePage' in desktop
    assert 'hero.className = "online-hero"' in desktop and 'action:"create_project"' in desktop
    assert 'data-control-tab="online"' not in desktop
    assert "Stay in Local mode" not in desktop
    assert "Cloud coordinates; endpoints decide." in desktop

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


def test_missions_pack_and_studio_stay_in_the_native_application_shell():
    desktop = read("harness/webui/index.html")
    remote = read("harness/webui/remote.html")
    studio = read("harness/webui/studio.html")
    meetings = read("harness/webui/meetings.html")
    comfy = read("harness/webui/comfy.html")

    assert 'id="missionsPanel"' in desktop and 'id="missionsGrid"' in desktop
    assert '"mission-list-card"' in desktop
    assert 'm.goal' not in desktop.split("function showMissions()", 1)[1].split(
        "function missionHelp()", 1)[0]
    assert 'id="surfacePanel"' in desktop and 'id="surfaceFrame"' in desktop
    assert '<button type="button" class="side-nav-item" id="navPack">' in desktop
    assert 'openEmbeddedSurface("Devices & team", "/remote?embedded=1", "pack")' in desktop
    assert 'openEmbeddedSurface("Studio", "/studio?embedded=1")' in desktop
    assert 'openEmbeddedSurface("Meeting notes", "/meetings?embedded=1")' in desktop
    assert 'openEmbeddedSurface("Comfy", "/comfy?embedded=1")' in desktop
    mission_detail = desktop.split("function showMission(mid, openReport)", 1)[1].split(
        "var missionsPanel", 1)[0]
    assert 'card.scrollIntoView({ block: "start", behavior: "instant" })' in mission_detail
    assert "collie:prefill" in desktop and "collie:prefill" in comfy
    for page in (remote, studio, meetings, comfy):
        assert 'get("embedded")==="1"' in page
        assert "body.embedded" in page
    assert 'localConnected?"Connected"' in comfy
    assert 'localConnected?"Connected":"Connect local MCP"' in comfy
    assert ".hero>.row{flex:0 0 auto;flex-wrap:nowrap;margin-top:0}" in comfy
    assert ".hero>.row{flex-wrap:wrap;margin-top:12px}" in comfy


def test_library_inventory_and_add_flows_are_first_class_ui():
    desktop = read("harness/webui/index.html")
    server = read("harness/webapp.py")

    for node in ("librarySummary", "libraryBuiltins", "librarySkills", "libraryConnections",
                 "libraryWorkflows", "libraryAdd", "librarySkillForm", "libraryPackageForm",
                 "libraryConnectionForm", "libraryConnectionGoal", "libraryConnectionResults"):
        assert f'id="{node}"' in desktop
    for path in ("/api/library/skill", "/api/library/package/preview",
                 "/api/library/package/install", "/api/session-token"):
        assert path in desktop or path in server
    assert "authenticatedFetch" in desktop and "refreshSessionToken" in desktop
    assert "confirmed:true" in desktop
    assert 'openSettings("mcp")' in desktop
    assert "/api/mcp/recommend" in desktop and 'action:"connect_candidate"' in desktop
    assert "Only these generic labels will be sent" in desktop
    assert "community_unreviewed" in desktop and "Review and connect" in desktop
    assert 'setLibraryAddMode("connection")' in desktop
    assert "renderLibraryInventory" in desktop
    assert 'kind === "connection" ? "action" : "tool"' in desktop
    assert 'kind === "connection" ? "actions" : "tools"' in desktop
    assert 'libraryQuantity(row.event_count, "event", "events")' in desktop


def test_pack_page_reports_operational_state_without_inventing_device_presence():
    page = read("harness/webui/remote.html")

    assert "<title>Pack — Collie</title>" in page and 'id="packcard"' in page
    for endpoint in ("/api/whoami", "/api/run-capabilities", "/api/healthz",
                     "/api/activity", "/api/approvals", "/api/remote/status", "/api/online"):
        assert endpoint in page
    assert 'id="packmembers"' in page and 'id="packassignments"' in page
    assert 'class="packtopology"' in page and "Holds no endpoint execution authority" in page
    assert "endpoint-signed assignment" in page and "inbound shell" in page
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


def test_activity_distinguishes_same_named_workers_from_services():
    pages = {
        "desktop": read("harness/webui/index.html"),
        "mobile": read("harness/webui/mobile.html"),
        "remote": read("harness/webui/remote.html"),
        "ambient": read("harness/webui/ambient.html"),
    }
    for name, page in pages.items():
        assert 'name + " · " + t("Worker")' in page or \
               'name+" · "+t("Worker")' in page or \
               "name+' · '+t('Worker')" in page or \
               'name+" · "+tOps("Worker")' in page, name
        assert 'name + " · " + t("Service")' in page or \
               'name+" · "+t("Service")' in page or \
               "name+' · '+t('Service')" in page or \
               'name+" · "+tOps("Service")' in page, name


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


def test_live_copilot_is_a_top_level_context_and_handoff_mode():
    desktop = read("harness/webui/index.html")
    live = read("harness/webui/live.html")
    server = read("harness/webapp.py")

    assert 'id="navLive"' in desktop and 'id="navInterview"' not in desktop
    assert 'openEmbeddedSurface("Live Copilot", "/live?embedded=1" +' in desktop
    assert '"&native_shell=1"' in desktop
    assert 'path in ("/live", "/interview")' in server
    assert 'path in ("/api/live-copilot", "/api/interview", "/api/live-copilot/export")' in server
    assert "Stay in context while you work" in live
    assert "Follow windows and apps" in live and "Understand the conversation" in live
    assert 'id="observeInput" checked' in live and "LIVE LOG" in live
    assert 'id="observeScreen"' in live and 'id="voiceDialogue" disabled' in live
    assert "observe_screen:" in live and "voice_dialogue:" in live
    assert 'id="handoffGuide"' in live and 'getElementById("handoffGuide").hidden=active' in live
    assert ".handoff[hidden]{display:none}" in live
    assert 'class="handoff" id="handoff" hidden' in live
    assert "NATIVE_HOST&&!hand.pending" in live
    assert "Ctrl</kbd> + <kbd>Alt</kbd> + <kbd>Space" in live
    assert "/api/live-copilot/work" in live and 'type:"collie:run"' in live
    assert "A board or browser canvas is one place" in live


def test_live_capsule_is_a_native_hotkey_surface_not_a_full_window_handoff():
    capsule = read("harness/webui/live_capsule.html")
    native_host = read("harness/wallpaper/Program.cs")
    build = read("harness/wallpaper/build.ps1")
    server = read("harness/webapp.py")

    assert 'path == "/live-capsule"' in server
    assert 'authority_text = ' in server and 'run_kwargs["authority_msg"] = authority_text' in server
    assert "LIVE CAPSULE COMMAND" in capsule and "/api/stream" in capsule
    assert "authority_text=" in capsule and "runner=collie" in capsule
    assert '/api/live-copilot/event' in capsule and 'kind:"command"' in capsule
    assert "capsule-speech-final" in capsule and "capsule-context" in capsule
    assert "TARGET.hwnd" in capsule and "TARGET.pid" in capsule
    assert "OpenLiveCapsule(CaptureLiveTarget())" in native_host
    hotkey = native_host.split("m.Msg == WM_HOTKEY", 1)[1].split("return;", 1)[0]
    assert "WakeWindow()" not in hotkey
    assert "GetForegroundWindow()" in native_host
    assert "SpeechRecognitionEngine" in native_host and "System.Speech" in build
    assert "live-native-state" in native_host and "live-native-transcript" in native_host
    assert "live-native-speak" in native_host and "SpeechSynthesizer" in native_host
    assert "_liveVoiceSpeaking" in native_host and "StopLiveSpeechEngine();" in native_host
    ready = native_host.split('raw.IndexOf("capsule-ready"', 1)[1].split(
        'else if (raw.IndexOf("capsule-listen"', 1)[0]
    assert "PostCapsuleTarget(target)" in ready and "StartCapsuleSpeech" not in ready
    assert 'if(STATE.active)host({type:"capsule-listen"' in capsule


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
    assert "End mission & take over" in desktop and "Return to Collie" in desktop
    assert "Accept & take over" not in desktop
    assert "This ends the Mission without independent verification." in desktop
    assert 'summaryRow("Current"' in desktop and 'summaryRow("Next"' in desktop
    assert 'summaryRow("Coverage"' in desktop and 'summary.coverage' in desktop
    assert 'className = "mactivity"' in desktop and 't("Activity log")' in desktop
    assert 'className = "mreport"' in desktop and 't("Progress report")' in desktop
    assert 't("Copy Markdown")' in desktop and 't("Download JSON")' in desktop
    assert 'reportCoverage.branches' in desktop and 'report.log' in desktop
    assert 'reportWasOpen = !!(b && b.querySelector(".mreport[open]"))' in desktop
    assert "reportBox.open = reportWasOpen" in desktop
    assert "navigator.clipboard.writeText(text).catch(fallback)" in desktop
    assert "protected from repeat" in desktop
    assert 't("Technical audit trail")' in desktop
    assert "st.receipts.slice(-12).reverse()" in desktop
    assert "execution attempted" not in desktop
    assert 'className = "mactivity-detail"' in desktop
    assert 't("in_progress")' not in desktop  # statuses are translated through t(item.status)
    assert '"in_progress": "正在执行"' in desktop
    assert 'summaryRow("Next check"' in desktop and "st.next_wake_at" in desktop
    assert "pending_authorizations" in desktop and "Collie is continuing independent work." in desktop
    for key in ("PROFILE_AGE_BAND", "AUTO_APPLY_PROFILE_CLAIMS",
                "MAX_AUTO_AUTH_RISK", "DEFER_MISSING_AUTHORIZATIONS"):
        assert key in desktop
    assert "/api/work-identities" in desktop
    assert "Connect open Voice tab" in desktop and "Collie-assigned line" in desktop
    assert 'name="verification.fill"' in read("harness/primitives.py")


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

    assert desktop.count('role="radiogroup"') == 8
    assert desktop.count('role="radio"') == 24
    assert 'id="runRunner"' in desktop
    assert 'data-val="codex-exec"' in desktop and 'data-val="claude-code"' in desktop
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

    # A canceled regular task keeps its evidence too; deleting its whole bubble
    # used to be the implementation this static Pack check accidentally required.
    assert "renderInterruptedRun(d)" in desktop
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
    assert "runStream(q, imgs, runConfig, runSession, userMsgEl, contexts)" in desktop
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
    assert 'type:"collie:openFile"' in map_page
    assert 'data-ide="1"' in map_page
    assert 'QS.has("vscode_embed")' in map_page
    assert '@media(min-width:701px) and (max-width:1200px)' in map_page
    assert 'innerWidth>1200' in map_page
    assert "function homeDistance()" in map_page
    assert 'next.set("vscode_embed",embed)' in map_page
    wallpaper_host = read("harness/wallpaper/Program.cs")
    assert "new Size(1280, 820)" in wallpaper_host
    assert "function safeHttpUrl" in wallpaper
    assert 'replace(/[&<>"\']/g' in wallpaper
    assert 'rel="noopener noreferrer"' in wallpaper


def test_desktop_dialogs_and_dynamic_model_count_are_accessible():
    page = read("harness/webui/index.html")
    assert "function dialogOpened" in page and "function dialogClosed" in page
    assert 'event.key !== "Tab"' in page
    assert "modelStatus.textContent = optionCount" in page
    assert 'role="switch"' in page and 'aria-checked="true"' in page
    # Durable queue acceptance/retry errors are exercised in the browser by
    # test_web_ui_run_status; the former volatile /api/steer UI was removed.


def test_desktop_shows_the_resolved_server_run_plan_and_worker_limits():
    page = read("harness/webui/index.html")
    assert "function runPlanSummary(plan)" in page
    assert "function showRunPlan(plan)" in page
    assert 'showRunPlan(d.run_plan)' in page
    assert 'shownRunPlan === plan.id' in page
    assert '(plan.limitations || []).map(t).join("; ")' in page
    show_plan = page.split("function showRunPlan(plan)", 1)[1].split("function setReceipt", 1)[0]
    assert "traceStep(" not in show_plan, "run setup belongs in details, not the conversation"
    assert 'addEvent("meta", "flag", t("Run setup")' in show_plan
    assert 'limitations ? " · " + limitations' in show_plan
    assert 'runCanSteer = !!((d.worker_capabilities || {}).steer)' in page
    assert 'This worker cannot be steered — stop it or wait' in page
    assert 'liveRuns[currentSession].state = d.canceled ? "canceled"' in page
    assert 'liveRuns[currentSession].verified = !!d.verified' in page
    assert 'row.key === "collie"' in page
    assert 't("Native tools, approvals, streaming, and steering")' in page
    assert 't(row.probe.availability ||' in page


def test_desktop_scrollbars_follow_explicit_and_system_themes():
    page = read("harness/webui/index.html")
    assert ':root[data-theme="light"]' in page and "color-scheme:light" in page
    assert ':root[data-theme="dark"]' in page and "color-scheme:dark" in page
    assert "--scrollbar-track:#11131A" in page
    assert "--scrollbar-thumb:#343946" in page
    assert "*::-webkit-scrollbar-track" in page
    assert "*::-webkit-scrollbar-thumb:hover" in page
    assert "*::-webkit-scrollbar-corner" in page
    assert "*::-webkit-scrollbar-button" in page


def test_only_complete_ui_languages_are_selectable():
    page = read("harness/webui/index.html")
    settings = read("harness/settings.py")
    assert 'var SUPPORTED = ["en", "zh-tw", "zh"]' in page
    language_block = settings.split('"key": "LANG"', 1)[1].split("],", 1)[0]
    assert all(code in language_block for code in ('"auto"', '"en"', '"zh"', '"zh-tw"'))
    assert '"es"' not in language_block


def test_selectable_desktop_locales_cover_declared_and_dynamic_strings():
    page = read("harness/webui/index.html")
    zh = (page.split("var ZH = {", 1)[1].split("var ZHTW = {", 1)[0] +
          page.split("Object.assign(ZH, {", 1)[1].split("Object.assign(ZHTW, {", 1)[0])
    zhtw = (page.split("var ZHTW = {", 1)[1].split("Object.assign(ZH, {", 1)[0] +
            page.split("Object.assign(ZHTW, {", 1)[1].split("var I18N = {", 1)[0])
    key_pattern = r'"((?:\\.|[^"\\])*)"\s*:'
    zh_keys, zhtw_keys = set(re.findall(key_pattern, zh)), set(re.findall(key_pattern, zhtw))
    declared = {unescape(value) for value in re.findall(
        r'data-i18n(?:-[\w-]+)?="([^"]+)"', page)}
    dynamic = set(re.findall(r'\bt\("([^"\n]+)"\)', page))
    missing = sorted((declared | dynamic) - (zh_keys & zhtw_keys))
    assert missing == []


def test_desktop_hydrates_cross_window_run_state_before_poll_interval():
    page = read("harness/webui/index.html")
    immediate = page.index("  pollRuns();\n  setInterval(pollRuns, 2500);")
    assert page.index("  function pollRuns() {") < immediate


def test_read_only_run_setup_never_keeps_an_external_worker_checked():
    page = read("harness/webui/index.html")
    assert 'effectiveWorker !== "collie" && effectiveWorker !== "auto"' in page
    assert 'fields.runner.value = "collie";' in page
    assert 'workerKey !== "collie" && workerKey !== "auto"' in page


def test_explicit_external_worker_stays_disabled_until_probe_truth_arrives():
    page = read("harness/webui/index.html")
    assert 'var explicitExternal = axis === "runner"' in page
    assert "(!capabilities || !workerRow || !workerRow.probe || !workerRow.probe.runnable)" in page


def test_selectable_mobile_locales_cover_declared_and_dynamic_strings():
    page = read("harness/webui/mobile.html")
    zh = page.split("var ZH={", 1)[1].split("var ZHTW={", 1)[0]
    zhtw = page.split("var ZHTW={", 1)[1].split("function t(en)", 1)[0]
    key_pattern = r'"((?:\\.|[^"\\])*)"\s*:'
    zh_keys, zhtw_keys = set(re.findall(key_pattern, zh)), set(re.findall(key_pattern, zhtw))
    declared = {unescape(value) for value in re.findall(
        r'data-t(?:-[\w-]+)?="([^"]+)"', page)}
    dynamic = set(re.findall(r"\bt\((?:'([^'\n]+)'|\"([^\"\n]+)\")\)", page))
    dynamic_keys = {left or right for left, right in dynamic}
    missing = sorted((declared | dynamic_keys) - (zh_keys & zhtw_keys))
    assert missing == []


def test_mobile_run_setup_carries_worker_truth_and_blocks_unsupported_steering():
    page = read("harness/webui/mobile.html")
    assert 'id="mRunner"' in page
    assert "runner:$(\"mRunner\").value" in page
    assert "'&runner='+encodeURIComponent(o.runner)" in page
    assert page.count('"runner","receipt"') >= 2
    assert "if(!runCapabilitiesKnown||!runCanSteer)" in page
    assert "data.worker_capabilities||{}" in page
    assert 'effectiveWorker!=="collie"&&effectiveWorker!=="auto"' in page
    assert "function mobileRunPlanSummary(plan)" in page
    assert "(plan.limitations||[]).map(t)" in page


def test_selectable_remote_locales_cover_declared_and_dynamic_strings():
    page = read("harness/webui/remote.html")
    zh = (page.split("var ZH = {", 1)[1].split("var ZHTW = {", 1)[0] +
          page.split("Object.assign(ZH,{", 1)[1].split("Object.assign(ZHTW,{", 1)[0])
    zhtw = (page.split("var ZHTW = {", 1)[1].split("Object.assign(ZH,{", 1)[0] +
            page.split("Object.assign(ZHTW,{", 1)[1].split("function t(en)", 1)[0])
    key_pattern = r'"((?:\\.|[^"\\])*)"\s*:'
    zh_keys, zhtw_keys = set(re.findall(key_pattern, zh)), set(re.findall(key_pattern, zhtw))
    declared = {unescape(value) for value in re.findall(
        r'data-i(?:-[\w-]+)?="([^"]+)"', page)}
    dynamic = set(re.findall(r"\bt\((?:'([^'\n]+)'|\"([^\"\n]+)\")\)", page))
    dynamic_keys = {left or right for left, right in dynamic}
    missing = sorted((declared | dynamic_keys) - (zh_keys & zhtw_keys))
    assert missing == []


def test_selectable_ambient_locales_cover_declared_and_dynamic_strings():
    page = read("harness/webui/ambient.html")
    zh = (page.split("var OPS_ZH=", 1)[1].split("var OPS_ZHTW=", 1)[0] +
          page.split("Object.assign(OPS_ZH,", 1)[1].split("Object.assign(OPS_ZHTW,", 1)[0])
    zhtw = (page.split("var OPS_ZHTW=", 1)[1].split("Object.assign(OPS_ZH,", 1)[0] +
            page.split("Object.assign(OPS_ZHTW,", 1)[1].split("function opsLocale", 1)[0])
    key_pattern = r'"((?:\\.|[^"\\])*)"\s*:'
    zh_keys, zhtw_keys = set(re.findall(key_pattern, zh)), set(re.findall(key_pattern, zhtw))
    declared = {unescape(value) for value in re.findall(
        r'data-ops-(?:t|aria|title)="([^"]+)"', page)}
    dynamic = set(re.findall(r'\btOps\((?:"([^"\n]+)"|\'([^\'\n]+)\')\)', page))
    dynamic_keys = {left or right for left, right in dynamic}
    missing = sorted((declared | dynamic_keys) - (zh_keys & zhtw_keys))
    assert missing == []


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
    site_version = json.loads(read("landing/site-version.json"))
    config = read("landing/wrangler.toml")
    chat = read("landing/functions/api/chat.js")
    assert package["scripts"]["build"] == "node build.mjs"
    assert "publicFiles" in build and '"_headers"' in build and "index.draft.html" not in build and "_preview.html" not in build
    assert '"site-version.json"' in build
    assert "source_product_version must match harness.__version__" in build
    assert site_version["content_version"] == "live-copilot-v1"
    assert site_version["features"]["personal_intelligence"] == "preview"
    assert site_version["features"]["live_copilot"] == "preview"
    assert site_version["features"]["collie_online"] == "preview"
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


def test_landing_positioning_and_versions_follow_the_product_release():
    page = read("landing/index.html")
    privacy = read("landing/privacy.html")
    manifest = json.loads(read("landing/site-version.json"))
    package_source = read("harness/__init__.py")
    package_version = re.search(r'^__version__\s*=\s*["\']([^"\']+)', package_source,
                                flags=re.MULTILINE).group(1)

    assert manifest["source_product_version"] == package_version
    assert f'data-site-version="{manifest["content_version"]}"' in page
    assert (f'data-privacy-notice-version="{manifest["privacy_notice_version"]}"'
            in privacy)
    assert "An AI that learns" in page and "how you work" in page
    assert "local-first personal intelligence" in page
    assert "browsing patterns alone never create a life event" in page


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


def test_pack_workspace_copy_explains_the_real_isolation_boundary():
    desktop = read("harness/webui/index.html")
    mobile = read("harness/webui/mobile.html")

    assert ("Candidates run in isolated worktrees; current files change only when a "
            "winner is applied.") in desktop
    assert 'pack?"Pack base · candidates isolated":"Current files"' in mobile
