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
   bundles it; developers point Chrome at the folder in the collie they're running.
3. The extension's popup shows a status dot — green means Collie can drive the tab.

!!! warning "Load the extension from the collie you actually run"
    If Chrome loads the extension from a *different* checkout than the collie you're running, every
    fix looks like it did nothing. The popup warns on a version mismatch — the bridge reports the
    version it expects, the extension reports the version Chrome loaded, and they must match.

### Security

The bridge is localhost-only and refuses any request missing its CSRF header; it checks `Origin`
and `Host`. Untrusted page content Collie reads is fenced as data (prompt-injection defense).

## Uninstalling

The installer's *Uninstall* entry (or *Add or remove programs*) stops the wallpaper, removes both
logon autostarts, and deletes the app — no leftovers.
