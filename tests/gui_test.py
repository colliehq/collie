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
    env = dict(os.environ, PYTHONUNBUFFERED="1",
               COLLIE_SETTINGS_PATH=setpath, COLLIE_SESSIONS_DIR=sessdir)
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

            # --- mode selector present w/ both modes (custom dropdown: .mode-item[data-val]) ---
            modes = pg.eval_on_selector_all(".mode-item", "els => els.map(e => e.getAttribute('data-val'))")
            check("mode selector (normal+herding+pack)",
                  "normal" in modes and "herding" in modes and "pack" in modes, str(modes))

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
            pg.fill("#modelSearch", "Mock")
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
            check("settings persisted to disk", saved.get("MODEL") == "claude-sonnet-5" and saved.get("MAX_TURNS") == "9",
                  "file=%r" % saved)
            # re-GET reflects the saved values
            got_model = pg.evaluate("async () => (await (await fetch('/api/settings')).json()).values.MODEL")
            check("settings GET reflects save", got_model == "claude-sonnet-5", "got=%r" % got_model)
            # `.set-row` matches rows in every category, and all but the open one are display:none —
            # so waiting for the first match to be visible waits for a row in a pane nobody opened.
            pg.click("#settingsBtn")
            pg.wait_for_selector(".set-pane.on .set-row", timeout=15000)
            pg.keyboard.press("Escape"); pg.wait_for_timeout(200)
            check("settings ESC closes", "open" not in pg.query_selector("#setOverlay").get_attribute("class"))
            # unauth POST -> 403
            code403 = pg.evaluate("async () => (await fetch('/api/settings', {method:'POST', body:'{}'})).status")
            check("settings CSRF: unauth POST -> 403", code403 == 403, "got %s" % code403)

            # --- mobile: no horizontal BODY overflow at 390px ---
            pg.set_viewport_size({"width": 390, "height": 780})
            pg.wait_for_timeout(400)
            overflow = pg.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
            check("mobile: no horizontal overflow", overflow <= 2, "overflow=%spx" % overflow)

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
