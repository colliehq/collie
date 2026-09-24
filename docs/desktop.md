# The desktop app

On Windows, Collie is more than a terminal tool — it's a native app with an optional live wallpaper
and a bridge to your real browser. All three are installed by the [one-click installer](install.md)
(the last two are opt-in checkboxes) or can be turned on later from the command line.

## Native window

```bash
collie app
```

Opens Collie in a real desktop window (WebView2) with the full web GUI inside — chat, the live
verification gate, diffs, and the star-map — instead of a browser tab. This is what the Start-menu
and desktop shortcuts launch. It falls back to the browser GUI on non-Windows platforms.

## Live star-map wallpaper

```bash
collie wallpaper --install     # behind your desktop icons, starts at logon
collie wallpaper --stop        # stop the running engine
collie wallpaper --uninstall   # remove the logon autostart
```

A live desktop background that renders Collie's star-map. On Windows it draws *behind* your icons
via a WebView2 engine (built once on first run from the shipped C# source — no .NET SDK needed);
elsewhere it degrades to a borderless full-screen window. Per-user, no admin.

## Native application control

The **Control desktop apps** setting enables Collie's `desktop_*` tools. Collie always chooses the
highest semantic layer available; a connector/application API, CLI, or browser DOM adapter is above
the generic desktop stack. On Windows the generic stack is:

1. UI Automation attaches to a real Win32 `hwnd`. It covers Invoke, Value, Text, Toggle, Selection,
   ExpandCollapse, Scroll, RangeValue, Grid/Table, Window, Transform, Dock, MultipleView and
   VirtualizedItem patterns.
2. MSAA/IAccessible supplies semantic state and default actions for older or managed controls whose
   UIA provider is incomplete.
3. Allow-listed Win32 control messages operate classic buttons, edits, checkboxes, list boxes and
   combo boxes without moving the cursor or stealing focus. Calls are time-bounded so a hung app
   cannot hang Collie.
4. Foreground Unicode keyboard input is the first simulated-input fallback.
5. Mouse `SendInput` is last, for canvases, games, remote desktops and other custom-rendered surfaces.
   Window-relative points from a downscaled `screenshot` are scaled back to the real window when
   `image_width` and `image_height` are supplied.

Every action result includes `method`/`layer`, so a caller can audit whether it used UIA, MSAA,
Win32, keyboard, or mouse. `desktop_click` and `desktop_type` follow that ladder automatically.

The surface is:

- `desktop_apps`, `desktop_inspect`, `desktop_read`, `desktop_wait` — discover and verify;
- `desktop_click`, `desktop_type`, `desktop_range` — accessibility-first actions;
- `desktop_uia` — explicit advanced semantic patterns (selection, grid, scroll, window, transform,
  docking, views and virtualized items), with no input-simulation fallback;
- `desktop_win32` — explicit MSAA/allow-listed classic-control operations;
- `desktop_mouse`, `desktop_drag`, `desktop_key` — real foreground input;
- `desktop_window`, `desktop_focus`, `desktop_launch` — window/application lifecycle;
- `desktop_clipboard` — Unicode clipboard get/set;
- `desktop_script` — up to 100 bounded inspect/action/wait steps in one local call.

Desktop and screen-capture consent remain separate. A screen image can expose unrelated private
content even when the requested mouse action is constrained to one app.

Windows still enforces its own security boundary. A normal Collie process cannot inject into an
elevated process, the UAC secure desktop, another login session or an anti-cheat protected/exclusive
input surface. Apps can also deliberately expose only a canvas, block automation, or change their UI;
those cases fall back to keyboard/vision/mouse and require stronger postcondition checks. Collie
reports OS boundaries rather than bypassing them. macOS continues to use System
Events/Accessibility and currently has the narrower labelled-control/menu surface.

## Real-browser bridge

```bash
collie browser-bridge            # run the bridge in the foreground
collie browser-bridge --install  # start it at logon
```

Lets Collie's `browser_*` tools drive **your** already-logged-in Chrome, instead of a fresh headless
browser that isn't signed in to anything. It works with a small Chrome extension that polls a
localhost bridge:

1. Run the bridge (`collie browser-bridge`, or install it at logon).
2. Load the extension from `harness/browser_ext` (Chrome → Extensions → Load unpacked). The installer
   bundles it; developers point Chrome at the folder in the collie they're running. Tagged GitHub
   releases also attach the credential-free `collie-browser-bridge-<version>.zip` used for Chrome Web
   Store review; unzip it before using **Load unpacked**.
3. The extension's popup shows a status dot — green means Collie can drive the tab.

The toolbar popup separates two permissions that are easy to confuse:

- **Website access** controls navigation. The recommended default permanently allows ordinary sites
  and still asks on bank, payment, brokerage, crypto-wallet, and user-configured sensitive domains.
- **Input fidelity** controls whether this site receives real Chrome-debugger input or synthetic DOM
  events. It does not grant permission to perform the action.

Clicks, typing, uploads, payments, sends, publishing, deletion, and permission changes retain their
normal action-time gate regardless of website access. To ask about the page without leaving it, open
**Side chat**, or select text and choose **Ask Collie about this selection** from the context menu.
Side chat starts its authenticated local Web/SSE backend on demand. During an upgrade, if an older
Collie Web process still owns the default port, the extension discovers or starts the matching
backend on the next free loopback port instead of requiring you to kill the desktop process first.

While Collie holds a tab, a small in-page pill reports Observing, Acting, Ready, Waiting, or Paused,
and a visible pointer shows where input lands. Press **Stop**, use the page yourself, or cancel Chrome's
debugger banner to hard-pause that tab. No command silently falls back to synthetic input after a
takeover; Resume is available only from extension-owned UI.

Each Web run receives its own browser space. Completion, cancellation, failure, or a disconnected
client releases control automatically. The final tab stays open as a handoff; a tab you handed to
Collie is never closed by cleanup.

A page dialog in Collie's tab is answered as it opens, so it cannot hold up every other browser
command: an alert is acknowledged, and a confirm, prompt, or "leave this page?" box is answered
**Cancel** unless the action was given `dialog: "accept"`. The tool result quotes what the page asked
and says what was answered, so a cancelled confirm reads as one rather than as a click that did
nothing. Answering OK asks for approval as a final action; a box that came up after the previous
action returned is only ever cancelled. This works on tabs Collie holds with Chrome's debugger
(the default input mode).

If the extension has not been heard from for about 90 seconds and is not busy with a command, the
bridge answers at once that it is not connected and since when, instead of letting each command wait
out its timeout. Opening Chrome with the extension enabled reconnects it.

!!! warning "Load the extension from the collie you actually run"
    If Chrome loads the extension from a *different* checkout than the collie you're running, every
    fix looks like it did nothing. The popup warns on a version mismatch — the bridge reports the
    version it expects, the extension reports the version Chrome loaded, and they must match.

### Security

The bridge is localhost-only and refuses any request missing its CSRF header; it checks `Origin`
and `Host`. Its bearer token is distinct from the local Web UI token; the side panel exchanges it
over loopback rather than exposing the Web token to page JavaScript. Untrusted page content Collie
reads is fenced as data (prompt-injection defense).

## Updating

*Settings → General → Updates* shows the version you are running and checks GitHub for a newer one
when you ask (or daily, if you turn that on). On an installer copy, *Install and restart* verifies
the download, closes Collie, runs Setup silently and brings back the window, wallpaper, browser
bridge and background services that were running. The page reconnects by itself when Collie is
back. Chrome keeps the old browser extension until you reload it in `chrome://extensions`; Settings
says so while it does. See [Staying up to date](install.md#staying-up-to-date).

## Uninstalling

The installer's *Uninstall* entry (or *Add or remove programs*) stops the wallpaper, removes both
logon autostarts, and deletes the app — no leftovers.
