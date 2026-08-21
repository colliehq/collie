"""GUI interactive-component regression suite ($0 — mock provider, no model runs). Starts its own
collie web server, drives the UI with Playwright, checks the interactive parts I hand-wrote:
theme persist, retractable sidebar persist, mobile no-overflow, session rename/delete, mode
selector, CSRF token gate, welcome state.
    python3 tests/gui_test.py     (needs: system python w/ playwright; exit 0 = all pass)"""
import json, os, subprocess, sys, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = 8795
results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  PASS " if cond else "  FAIL ") + name + (("  :: " + detail) if detail and not cond else ""))

def wait_up(url, tries=40):
    for _ in range(tries):
        try:
            urllib.request.urlopen(url, timeout=1); return True
        except Exception:
            time.sleep(0.25)
    return False

def main():
    import tempfile
    setpath = os.path.join(tempfile.gettempdir(), "collie_gui_test_settings.json")
    try: os.remove(setpath)
    except OSError: pass
    # redirect settings to a temp file so the test never clobbers the user's real ~/.collie/settings.json
    sessdir = os.path.join(tempfile.gettempdir(), "collie_gui_test_sessions")
    # redirect settings AND sessions to temp so the test never clobbers real ~/.collie or floods the Map
    #
    # mock goes in the SETTINGS FILE, not COLLIE_PROVIDER. An env var set before the server starts is
    # deliberately unbeatable by the picker — so pinning it there made the model-switch checks below
    # test a UI that is correctly refusing to switch. The file gets the same $0 provider with none of
    # that: the picker is genuinely in charge, which is what these checks are about.
    with open(setpath, "w", encoding="utf-8") as fh:
        json.dump({"PROVIDER": "mock", "MODEL": "mock"}, fh)
    # One MCP server, in a temp config, so the MCP pane has a row to draw. It had none here, and the
    # code that draws a row read a variable belonging to a different function: the first server
    # anybody configured made the whole pane stop redrawing, and mcpLoad's catch-all swallowed the
    # ReferenceError so completely that pressing Connect looked like pressing nothing.
    mcppath = os.path.join(tempfile.gettempdir(), "collie_gui_test_mcp.json")
    with open(mcppath, "w", encoding="utf-8") as fh:
        json.dump({"servers": {"probe": {"url": "https://mcp.example.invalid/mcp"}}}, fh)
    slackpath = os.path.join(tempfile.gettempdir(), "collie_gui_test_empty_slack.json")
    try: os.remove(slackpath)
    except OSError: pass
    env = dict(os.environ, PYTHONUNBUFFERED="1", COLLIE_MCP_CONFIG=mcppath,
               COLLIE_SETTINGS_PATH=setpath, COLLIE_SESSIONS_DIR=sessdir,
               COLLIE_SLACK_STORE=slackpath)
    env.pop("COLLIE_PROVIDER", None)
    env.pop("COLLIE_MODEL", None)
    srv = subprocess.Popen([sys.executable if os.path.exists(sys.executable) else "python3",
                            "-m", "harness.webapp", "--port", str(PORT), "--no-open"],
                           cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not wait_up("http://127.0.0.1:%d/" % PORT):
            print("  FAIL server did not come up"); return 1
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            b = p.chromium.launch()
            pg = b.new_page(viewport={"width": 1200, "height": 820})
            perrs = []
            pg.on("pageerror", lambda e: perrs.append(str(e)))
            pg.goto("http://collie.localhost:%d/" % PORT, wait_until="load")
            pg.wait_for_timeout(400)

            # --- collie.localhost resolves + loads (cool URL) ---
            check("collie.localhost loads", "collie" in pg.title().lower())

            # --- welcome empty state ---
            check("welcome state shown", pg.query_selector("#welcome") is not None)

            # --- first-run companion naming: adoption is a real step, not a hidden config key ---
            try:
                pg.wait_for_selector("#nameOverlay.open", timeout=8000)
                name_appeared = True
            except Exception:
                name_appeared = False
            check("first run offers a companion name", name_appeared)
            if name_appeared:
                check("naming starts from a calm editable default",
                      pg.input_value("#nameInput") == "Rowan")
                pg.fill("#nameInput", "Mochi")
                pg.click("#nameContinue")
                pg.wait_for_selector("#nameOverlay.open", state="detached", timeout=15000)
                pg.wait_for_function("document.querySelector('[data-collie-name]').textContent === 'Mochi'")
                check("chosen name updates the desktop identity live",
                      pg.text_content("[data-collie-name]") == "Mochi")
                check("renamed avatar uses a versioned transparent endpoint",
                      "/api/avatar.png?v=" in (pg.get_attribute("[data-collie-avatar]", "src") or ""))

            # --- first run shows the onboarding, and it must be dismissable ---
            # This is why the suite broke: CI runs with COLLIE_PROVIDER=mock, so there is no working
            # brain and the onboarding overlay opens over everything — correctly. Every later
            # pg.click() then waited 30s for an element it could see but could not reach, and the
            # 30s timeout was the FIRST sign, with the reason nowhere in the log. So assert the
            # overlay behaves, then dismiss it the way a real first-run user does.
            #
            # WAIT for it rather than sampling it. The overlay opens when /api/models comes back,
            # and that call probes every provider — on a cold machine it lands well after the page
            # does. Sampling made this check fail on timing alone, and worse: the failure meant the
            # dismissal below was skipped, the overlay opened a moment later, and the FIRST honest
            # report of it was a 30s click timeout twelve checks further down.
            try:
                pg.wait_for_selector("#obOverlay.open", timeout=20000)
                appeared = True
            except Exception:
                appeared = False
            check("onboarding appears when no provider is authed", appeared)
            if appeared:
                pg.click("#obSkip")
                pg.wait_for_selector("#obOverlay.open", state="detached", timeout=15000)
            check("onboarding dismisses and stops blocking the page",
                  "open" not in ((pg.query_selector("#obOverlay").get_attribute("class") or "")
                                 if pg.query_selector("#obOverlay") else ""))

            # --- CSRF token injected ---
            tok = pg.eval_on_selector('meta[name="collie-token"]', "e => e.content")
            check("CSRF token injected", bool(tok) and len(tok) == 32, "token=%r" % tok)

            # Slash commands must be discoverable. A command that exists only in docs is why users
            # typed `/` and saw nothing, and it made the old --auto syntax feel mandatory.
            pg.fill("#input", "/")
            pg.wait_for_selector("#slashMenu", state="visible", timeout=5000)
            commands = pg.eval_on_selector_all(
                "#slashMenu .slash-option:not([hidden])",
                "els => els.map(e => e.getAttribute('data-command'))")
            check("slash opens a command palette",
                  commands == ["/mission ", "/mission --review ", "/code ", "/chat "],
                  str(commands))
            pg.press("#input", "ArrowDown")
            pg.press("#input", "Enter")
            check("slash palette keyboard selection fills without sending",
                  pg.input_value("#input") == "/mission --review " and
                  not pg.is_visible("#slashMenu"))
            pg.fill("#input", "")

            # --- run setup exposes independent axes; workspace/Pack are not quality modes ---
            axes = pg.eval_on_selector_all(
                ".mode-item", "els => els.map(e => [e.getAttribute('data-axis'),e.getAttribute('data-val')])")
            check("run setup has intent/effort/verify/workspace/strategy axes",
                  all(pair in axes for pair in [["intent", "plan"], ["quality", "thorough"],
                                                ["verification", "required"], ["workspace", "isolated"],
                                                ["strategy", "pack"]]), str(axes))
            pg.click("#modeTrigger")
            pg.wait_for_selector("#modeMenu", state="visible", timeout=15000)
            pg.keyboard.press("ArrowDown")
            arrow_state = pg.evaluate("""() => ({
              value: document.querySelector('#runIntent').value,
              planChecked: document.querySelector('[data-axis="intent"][data-val="plan"]').getAttribute('aria-checked'),
              planTab: document.querySelector('[data-axis="intent"][data-val="plan"]').tabIndex,
              buildTab: document.querySelector('[data-axis="intent"][data-val="build"]').tabIndex
            })""")
            check("radio arrow selects and moves the roving tab stop",
                  arrow_state == {"value": "plan", "planChecked": "true", "planTab": 0, "buildTab": -1},
                  str(arrow_state))
            pg.keyboard.press("ArrowUp")       # restore Build before the rest of the suite
            pg.focus('[data-axis="quality"][data-val="balanced"]')
            pg.keyboard.press("End")
            check("radio End key selects the last effort option",
                  pg.eval_on_selector("#runQuality", "e => e.value") == "thorough")
            pg.keyboard.press("Home")          # restore Balanced
            pg.keyboard.press("Escape")
            pg.wait_for_selector("#modeMenu", state="hidden", timeout=15000)

            # A failed attachment must never silently downgrade into a text-only model run.
            pg.route("**/api/upload*", lambda route: route.fulfill(
                status=500, content_type="application/json", body='{"error":"upload unavailable"}'))
            pg.evaluate("""() => {
              window.__uploadFailureStreams = [];
              window.EventSource = function(url) {
                window.__uploadFailureStreams.push(url);
                this.addEventListener = function() {};
                this.close = function() {};
              };
            }""")
            pg.set_input_files("#fileInput", {"name": "probe.png", "mimeType": "image/png", "buffer": b"image"})
            pg.wait_for_selector("#attachStrip .thumb")
            pg.fill("#input", "/code inspect this image")
            pg.click("#send")
            pg.wait_for_function("() => !![...document.querySelectorAll('.err')].find(e => e.textContent.includes('no run was started'))")
            upload_failure = pg.evaluate("""() => ({
              streams: window.__uploadFailureStreams.length,
              prompt: document.getElementById('input').value,
              thumbs: document.querySelectorAll('#attachStrip .thumb').length,
              active: document.getElementById('send').classList.contains('stop') || document.getElementById('input').disabled
            })""")
            check("failed image upload restores the draft and starts no text-only run",
                  upload_failure == {"streams": 0, "prompt": "inspect this image",
                                     "thumbs": 1, "active": False}, str(upload_failure))
            pg.click("#attachStrip .rm")
            pg.fill("#input", "")
            pg.unroute("**/api/upload*")

            # Pack's number field is not inside a native <form>, so min/max only works if send()
            # explicitly checks it. An invalid value must not open a run stream.
            pg.evaluate("""() => {
              window.__invalidDesktopStream = null;
              window.EventSource = function(url) {
                window.__invalidDesktopStream = url;
                this.addEventListener = function() {};
                this.close = function() {};
              };
            }""")
            pg.click("#modeTrigger")
            pg.click('[data-axis="strategy"][data-val="pack"]')
            pg.fill("#packN", "7")
            pg.fill("#packCheck", "pytest -q")
            pg.fill("#input", "/code invalid pack should stay local")
            pg.click("#send")
            invalid_pack = pg.evaluate("""() => ({
              stream: window.__invalidDesktopStream,
              focused: document.activeElement && document.activeElement.id,
              value: document.getElementById('input').value
            })""")
            check("desktop rejects out-of-range Pack attempts before launch",
                  invalid_pack["stream"] is None and invalid_pack["focused"] == "packN" and
                  "invalid pack" in invalid_pack["value"], str(invalid_pack))
            pg.fill("#packN", "3")
            pg.click("#modeTrigger")
            pg.click('[data-axis="strategy"][data-val="single"]')
            pg.keyboard.press("Escape")

            # --- model picker lives in the toolbar; run details stay available without a status rail ---
            check("status rail removed", pg.query_selector(".runbar") is None and pg.query_selector("#rbGate") is None)
            check("model trigger present in toolbar", pg.query_selector(".topbar #modelTrigger") is not None)
            check("run details collapsed by default", pg.query_selector("#workpanel").is_hidden())
            pg.click("#runDetailsBtn")
            pg.wait_for_selector("#workpanel", state="visible", timeout=15000)
            pg.click("#runDetailsBtn")
            pg.wait_for_selector("#workpanel", state="hidden", timeout=15000)
            pg.click("#modelTrigger")
            pg.wait_for_selector("#modelOverlay.open", timeout=15000)
            pg.wait_for_selector(".model-option", timeout=15000)
            check("model picker opens with catalog", len(pg.query_selector_all(".model-option")) >= 1)
            # "Mock" also matches the synthetic Auto row through its provider
            # name.  Search for the mock row's unique copy so ArrowDown proves
            # keyboard selection without accidentally choosing Auto first.
            pg.fill("#modelSearch", "canned")
            pg.wait_for_selector('.model-option[data-model-id="mock:mock"]', timeout=15000)
            pg.keyboard.press("ArrowDown")
            pg.keyboard.press("Enter")
            pg.wait_for_selector("#modelOverlay:not(.open)", state="attached", timeout=15000)
            model_label = pg.eval_on_selector("#modelTriggerLabel", "element => element.textContent")
            check("model picker keyboard switch persists", "Mock" in model_label, model_label)
            pg.keyboard.press("Control+K")
            pg.wait_for_selector("#modelOverlay.open", timeout=15000)
            pg.keyboard.press("Escape")
            pg.wait_for_selector("#modelOverlay:not(.open)", state="attached", timeout=15000)
            check("model picker shortcut opens and closes", True)

            # --- theme toggle + persistence ---
            before = pg.eval_on_selector(":root", "e => e.getAttribute('data-theme')")
            pg.click("#themeBtn"); pg.wait_for_timeout(150)
            after = pg.eval_on_selector(":root", "e => e.getAttribute('data-theme')")
            check("theme toggles", before != after, "%s->%s" % (before, after))
            pg.reload(wait_until="load"); pg.wait_for_timeout(300)
            persisted = pg.eval_on_selector(":root", "e => e.getAttribute('data-theme')")
            check("theme persists across reload", persisted == after, "%s vs %s" % (persisted, after))

            # --- retractable sidebar + persistence ---
            pg.click("#sideToggle"); pg.wait_for_timeout(400)
            collapsed = pg.eval_on_selector(".app", "e => e.classList.contains('side-collapsed')")
            check("sidebar collapses", collapsed)
            side_w = pg.eval_on_selector(".side", "e => e.getBoundingClientRect().width")
            check("collapsed sidebar has ~0 width", side_w < 5, "width=%.0f" % side_w)
            pg.reload(wait_until="load"); pg.wait_for_timeout(300)
            still = pg.eval_on_selector(".app", "e => e.classList.contains('side-collapsed')")
            check("sidebar state persists", still)
            pg.click("#sideToggle"); pg.wait_for_timeout(400)   # expand back
            expanded_w = pg.eval_on_selector(".side", "e => e.getBoundingClientRect().width")
            check("sidebar expands back", expanded_w > 200, "width=%.0f" % expanded_w)

            # --- session rename/delete via token'd endpoints (seed one session first) ---
            sid = "gui-test-session-001"
            urllib.request.urlopen("http://127.0.0.1:%d/api/rename/%s?title=RenamedByTest&token=%s"
                                   % (PORT, sid, tok), timeout=5)   # rename creates nothing if absent
            # seed a session file so delete has a target — in the SAME store the server was launched
            # with (COLLIE_SESSIONS_DIR), never the user's real data/sessions/
            sess_dir = sessdir
            os.makedirs(sess_dir, exist_ok=True)
            open(os.path.join(sess_dir, sid + ".json"), "w").write(json.dumps(
                {"id": sid, "messages": [{"role": "user", "content": "gui test seed"}], "title": "SeedTitle"}))
            r = urllib.request.urlopen("http://127.0.0.1:%d/api/delete/%s?token=%s" % (PORT, sid, tok), timeout=5)
            ok = json.load(r).get("ok")
            check("session delete (token'd) works", ok is True)
            check("deleted session file gone", not os.path.exists(os.path.join(sess_dir, sid + ".json")))

            # --- CSRF: delete WITHOUT token -> 403 ---
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/api/delete/whatever" % PORT, timeout=5)
                code = 200
            except urllib.error.HTTPError as e:
                code = e.code
            check("CSRF: unauth delete -> 403", code == 403, "got %s" % code)

            # --- settings modal: open, render, save, persist ---
            pg.click("#settingsBtn")
            pg.wait_for_selector("#setOverlay.open", timeout=15000)
            pg.wait_for_selector(".set-row", timeout=15000)   # rows render async after /api/settings resolves
            nrows = len(pg.query_selector_all(".set-row"))
            check("settings modal opens w/ rows", nrows >= 6, "rows=%d" % nrows)
            check("My Collie keeps a permanent rename control",
                  pg.is_visible("#set_COMPANION_NAME") and pg.input_value("#set_COMPANION_NAME") == "Mochi")
            old_avatar = pg.get_attribute("[data-collie-avatar]", "src") or ""
            pg.fill("#set_COMPANION_NAME", "Nori")
            pg.press("#set_COMPANION_NAME", "Tab")
            for _ in range(60):
                if pg.text_content("[data-collie-name]") == "Nori": break
                pg.wait_for_timeout(250)
            check("Settings rename propagates without a reload",
                  pg.text_content("[data-collie-name]") == "Nori")
            check("Settings rename busts the previous avatar URL",
                  (pg.get_attribute("[data-collie-avatar]", "src") or "") != old_avatar)
            # The modal grew a rail of categories, one visible .set-pane at a time — so a field is in
            # the DOM long before it is reachable, and Playwright's fill() waited 30s for an <input>
            # it could see in the tree and never in the viewport. Click the owning category first.
            def set_field(key, value):
                cat = pg.eval_on_selector("#set_" + key,
                                          "e => e.closest('.set-pane').getAttribute('data-cat')")
                pg.click('.set-nav[data-cat="%s"]' % cat)
                pg.wait_for_selector("#set_" + key, state="visible", timeout=15000)
                pg.fill("#set_" + key, value)

            set_field("MODEL", "claude-sonnet-5")
            set_field("MAX_TURNS", "9")
            # Settings apply as you type now (debounced), and Save's remaining job is to close the
            # panel. Waiting for `.set-status.ok` to be VISIBLE was asserting the old contract: the
            # badge lives in the footer of the modal that Save just closed, so it resolved to a
            # hidden 0x0 span and the wait could only ever time out. What matters is that the value
            # reached the file, which is checked immediately below.
            pg.wait_for_timeout(1200)                    # the apply-on-change debounce
            pg.click("#setSave")
            # Wait for the OUTCOME, not for a fixed 300ms. Save closes the panel when the apply it
            # fired comes back, and the write lands with that same response — neither is instant on
            # a machine running the rest of this suite beside it. A sleep that is long enough today
            # is a flake tomorrow, and it fails as "Done does not close the panel", which sends the
            # reader into the click handler rather than at the clock.
            try:
                pg.wait_for_selector("#setOverlay.open", state="detached", timeout=15000)
            except Exception:
                pass
            check("save closes the settings panel",
                  "open" not in (pg.query_selector("#setOverlay").get_attribute("class") or ""))
            saved = {}
            for _ in range(60):
                try:
                    with open(setpath, encoding="utf-8") as f: saved = json.load(f)
                except Exception: saved = {}
                if saved.get("MAX_TURNS") == "9":
                    break
                pg.wait_for_timeout(250)
            check("settings persisted to disk", saved.get("MODEL") == "claude-sonnet-5" and saved.get("MAX_TURNS") == "9" and saved.get("COMPANION_NAME") == "Nori",
                  "file=%r" % saved)
            # re-GET reflects the saved values
            got_model = pg.evaluate("async () => (await (await fetch('/api/settings')).json()).values.MODEL")
            check("settings GET reflects save", got_model == "claude-sonnet-5", "got=%r" % got_model)
            # `.set-row` matches rows in every category, and all but the open one are display:none —
            # so waiting for the first match to be visible waits for a row in a pane nobody opened.
            pg.click("#settingsBtn")
            pg.wait_for_selector(".set-pane.on .set-row", timeout=15000)

            # --- the MCP pane draws the servers it has ---
            # The pane is not schema-driven (it is a live list), so nothing else here touches it.
            pg.click('.set-nav[data-cat="mcp"]')
            drew = True
            try:
                pg.wait_for_selector(".mcp-item .mcp-name", timeout=15000)
            except Exception:
                drew = False
            check("MCP pane draws a configured server", drew,
                  "mcpBox=%r" % (pg.eval_on_selector("#mcpBox", "e => e.textContent.slice(0, 120)")
                                 if pg.query_selector("#mcpBox") else "(no #mcpBox)"))
            if drew:
                names = pg.eval_on_selector_all(".mcp-item .mcp-name", "els => els.map(e => e.textContent)")
                check("and names it", "probe" in names, repr(names))
                chips = pg.eval_on_selector_all(".mcp-chip", "els => els.map(e => e.dataset.name)")
                check("with the one-press catalog beside it", len(chips) >= 5, str(chips[:4]))

            pg.keyboard.press("Escape"); pg.wait_for_timeout(200)
            check("settings ESC closes", "open" not in pg.query_selector("#setOverlay").get_attribute("class"))
            # unauth POST -> 403
            code403 = pg.evaluate("async () => (await fetch('/api/settings', {method:'POST', body:'{}'})).status")
            check("settings CSRF: unauth POST -> 403", code403 == 403, "got %s" % code403)

            # --- responsive desktop: popup and toolbar stay within 390/320 CSS px ---
            pg.set_viewport_size({"width": 390, "height": 780})
            pg.wait_for_timeout(400)
            overflow = pg.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check("mobile: no horizontal overflow", overflow <= 2, "overflow=%spx" % overflow)
            pg.click("#modeTrigger")
            pg.wait_for_selector("#modeMenu", state="visible", timeout=15000)
            menu_box = pg.eval_on_selector("#modeMenu", "e => {const r=e.getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width}}")
            check("390px: run setup popup stays inside viewport",
                  menu_box["left"] >= -0.5 and menu_box["right"] <= 390.5, str(menu_box))
            pg.keyboard.press("Escape")

            pg.set_viewport_size({"width": 320, "height": 700})
            pg.wait_for_timeout(300)
            overflow320 = pg.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            topbar_box = pg.eval_on_selector(".topbar", "e => {const r=e.getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width}}")
            check("320px: topbar and document do not overflow",
                  overflow320 <= 2 and topbar_box["left"] >= -0.5 and topbar_box["right"] <= 320.5,
                  "overflow=%spx topbar=%r" % (overflow320, topbar_box))
            pg.click("#modeTrigger")
            pg.wait_for_selector("#modeMenu", state="visible", timeout=15000)
            menu320 = pg.eval_on_selector("#modeMenu", "e => {const r=e.getBoundingClientRect(); return {left:r.left,right:r.right,width:r.width}}")
            check("320px: run setup popup stays inside viewport",
                  menu320["left"] >= -0.5 and menu320["right"] <= 320.5, str(menu320))

            # --- dedicated phone client exposes the same orthogonal contract ---
            pg.set_viewport_size({"width": 390, "height": 844})
            pg.goto("http://collie.localhost:%d/m" % PORT, wait_until="load")
            pg.wait_for_timeout(300)
            drawer_closed = pg.evaluate("""() => ({
              inert: drawer.hasAttribute('inert'), hidden: drawer.getAttribute('aria-hidden'),
              role: drawer.getAttribute('role'), modal: drawer.getAttribute('aria-modal')
            })""")
            check("phone closed drawer is inert and declared modal",
                  drawer_closed == {"inert": True, "hidden": "true", "role": "dialog", "modal": "true"},
                  str(drawer_closed))
            pg.click("#menuBtn")
            pg.keyboard.press("Shift+Tab")
            check("phone drawer traps keyboard focus",
                  pg.evaluate("() => !drawer.hasAttribute('inert') && drawer.contains(document.activeElement)"))
            pg.keyboard.press("Escape")
            check("phone drawer closes inert and returns focus",
                  pg.evaluate("() => drawer.hasAttribute('inert') && document.activeElement === menuBtn"))
            pg.click("#runSetup summary")
            phone_overflow = pg.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            visible_axes = pg.eval_on_selector_all(
                "#mIntent,#mQuality,#mVerification,#mWorkspace,#mStrategy",
                "els => els.filter(e => {const r=e.getBoundingClientRect(); return r.width>0 && r.height>0}).length")
            check("phone run setup shows all five axes without overflow",
                  phone_overflow <= 2 and visible_axes == 5,
                  "overflow=%spx visible=%s" % (phone_overflow, visible_axes))
            pg.select_option("#mIntent", "plan")
            plan_contract = pg.evaluate("""() => ({
              verification: mVerification.value, workspace: mWorkspace.value, strategy: mStrategy.value,
              verificationDisabled: mVerification.disabled, workspaceDisabled: mWorkspace.disabled,
              strategyDisabled: mStrategy.disabled
            })""")
            check("phone Plan resets and locks incompatible execution choices",
                  plan_contract == {"verification": "auto", "workspace": "current", "strategy": "single",
                                    "verificationDisabled": True, "workspaceDisabled": True,
                                    "strategyDisabled": True}, str(plan_contract))
            pg.select_option("#mIntent", "build")
            pg.select_option("#mQuality", "thorough")
            pg.select_option("#mVerification", "required")
            pg.select_option("#mWorkspace", "isolated")
            pg.select_option("#mStrategy", "pack")
            check("phone Pack owns isolation and requires a check",
                  pg.eval_on_selector("#mWorkspace", "e => e.value === 'current' && e.disabled") and
                  pg.eval_on_selector("#mPackCheck", "e => e.required && !e.closest('#mPackOpts').hidden"))
            pg.fill("#mPackCheck", "pytest -q")
            pg.check("#mPackApply")
            pg.evaluate("""() => {
              window.__lastRunUrl = null;
              window.EventSource = function(url) {
                window.__lastRunUrl = url;
                this.addEventListener = function() {};
                this.close = function() {};
              };
            }""")
            pg.fill("#mPackN", "7")
            pg.fill("#input", "invalid phone pack")
            pg.click("#send")
            invalid_phone_pack = pg.evaluate("""() => ({
              stream: window.__lastRunUrl, focused: document.activeElement && document.activeElement.id,
              value: document.getElementById('input').value
            })""")
            check("phone rejects out-of-range Pack attempts before launch",
                  invalid_phone_pack["stream"] is None and invalid_phone_pack["focused"] == "mPackN" and
                  "invalid phone" in invalid_phone_pack["value"], str(invalid_phone_pack))
            pg.fill("#mPackN", "4")
            pg.fill("#input", "exercise mobile options")
            pg.click("#send")
            sent = pg.evaluate("""() => {
              const u = new URL(window.__lastRunUrl, location.href);
              return Object.fromEntries(u.searchParams.entries());
            }""")
            check("phone sends every selected axis and Pack check",
                  all(sent.get(k) == v for k, v in {
                      "intent": "build", "quality": "thorough", "verification": "required",
                      "workspace": "current", "strategy": "pack", "n": "4", "check": "pytest -q",
                      "apply": "1"}.items()), str(sent))
            pg.set_viewport_size({"width": 320, "height": 700})
            pg.wait_for_timeout(250)
            phone_overflow320 = pg.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check("320px phone controls stay in viewport", phone_overflow320 <= 2,
                  "overflow=%spx" % phone_overflow320)

            check("no uncaught page errors", not perrs, str(perrs[:3]))
            b.close()
    finally:
        srv.terminate()
        try: srv.wait(timeout=5)
        except Exception: srv.kill()

    npass = sum(1 for _, c in results if c)
    print("\n== GUI: %d/%d passed ==%s" % (npass, len(results),
          "" if npass == len(results) else " FAILS: " + ", ".join(n for n, c in results if not c)))
    return 0 if npass == len(results) else 1

if __name__ == "__main__":
    sys.exit(main())
