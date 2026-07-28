"""Drive the user's real browser on macOS through Apple Events — no extension.

The extension bridge in `browserbridge` is the capable path and stays the
default. What it costs is an install: Chrome -> chrome://extensions ->
Developer mode -> Load unpacked, plus Chrome's recurring "disable developer
mode extensions" nag. That is a real barrier for a first run, and macOS is the
one platform that offers a way around it: Apple Events lets us execute
JavaScript in the user's already-open, already-logged-in tab with nothing
installed at all.

This module is a *transport*, not a second implementation. The page-side
functions (`pageRead`, `pageLinks`, `pageClick`, …) are read straight out of
`browser_ext/background.js` and shipped to the browser as-is. That is possible
because the extension runs them through `chrome.scripting.executeScript`, which
serialises each function and runs it in page scope — so they are already
required to be self-contained, with no closure or `chrome.*` access. Reusing
the source means the two transports cannot drift apart.

What it cannot do, and why:

  console   DevTools console history needs the `debugger` permission (CDP).
  upload    Needs a real file input handle from the extension side.
  eval      `pageEval` is async; Apple Events returns a value synchronously and
            would hand back an unresolved Promise.
  pick      Same async problem.

Those actions report a clear error telling the caller to load the extension,
rather than silently returning something wrong.

Setup the user must do once (still far less than loading an extension):
  Chrome  View -> Developer -> Allow JavaScript from Apple Events
  Safari  Develop -> Allow JavaScript from Apple Events
Plus the usual macOS Automation permission prompt on first use.
"""
import json
import os
import re
import subprocess

from . import plat

# Browsers whose AppleScript dictionary exposes `execute javascript`, in the
# order we prefer them. All the Chromium ones share Chrome's dictionary.
CHROMIUM_BROWSERS = [
    "Google Chrome",
    "Brave Browser",
    "Microsoft Edge",
    "Arc",
    "Chromium",
]
# Safari spells the same idea differently, hence the separate template below.
SAFARI = "Safari"

# Actions we can serve, mapped to the page function that implements them and
# how to build its argument list from the command dict.
_SYNC_ACTIONS = {
    "read": ("pageRead", lambda c: []),
    "links": ("pageLinks", lambda c: [c.get("filter", "")]),
    "snapshot": ("pageSnapshot", lambda c: [c.get("max", 200)]),
    "fields": ("pageFields", lambda c: []),
}

_UNSUPPORTED = {
    "console": "DevTools console history needs the extension (it requires Chrome's debugger permission).",
    "upload": "File upload needs the extension.",
    "eval": "browser_eval needs the extension (the page-side evaluator is async; Apple Events cannot await it).",
    "pick": "browser_pick needs the extension (its page function is async).",
}


class AppleEventsUnavailable(Exception):
    """Raised when this transport cannot serve a request at all."""


# --------------------------------------------------------------------- page source ----------
_PAGE_SRC_CACHE = {}


def _background_js_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "browser_ext", "background.js")


def _extract_function(name, source):
    """Pull `function <name>(...) { … }` out of background.js by brace matching.

    A regex cannot do this: the bodies contain braces in strings, regexes and
    nested blocks. Counting from the opening brace is short and exact.
    """
    m = re.search(r"^(?:async\s+)?function\s+%s\s*\(" % re.escape(name), source, re.M)
    if not m:
        raise AppleEventsUnavailable("page function %s not found in background.js" % name)
    start = m.start()
    brace = source.index("{", m.end() - 1)
    depth = 0
    for i in range(brace, len(source)):
        c = source[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[start:i + 1]
    raise AppleEventsUnavailable("unterminated %s in background.js" % name)


def page_function(name):
    """Source text of one page-side function, cached."""
    if name not in _PAGE_SRC_CACHE:
        with open(_background_js_path(), encoding="utf-8") as f:
            _PAGE_SRC_CACHE[name] = _extract_function(name, f.read())
    return _PAGE_SRC_CACHE[name]


# --------------------------------------------------------------------- osascript ------------
def _as_string(text):
    """Escape a Python string for embedding in an AppleScript string literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _profile_hint():
    """Name the Chrome profiles where the switch is already on, if we can tell.

    Chrome keys this setting per profile, and every profile shows the same menu
    item, so "I already turned that on" and "the browser refuses" are routinely
    both true at once. Reading the on-disk prefs turns that into a specific
    statement instead of a puzzle.
    """
    base = os.path.expanduser("~/Library/Application Support/Google/Chrome")
    if not os.path.isdir(base):
        return ""
    enabled = []
    for entry in sorted(os.listdir(base)):
        prefs = os.path.join(base, entry, "Preferences")
        if not os.path.exists(prefs):
            continue
        try:
            with open(prefs, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        if d.get("browser", {}).get("allow_javascript_apple_events"):
            enabled.append("%s (%s)" % (d.get("profile", {}).get("name", entry), entry))
    if not enabled:
        return "It is currently off in every Chrome profile on this Mac."
    return "It is currently on only in: %s." % ", ".join(enabled)


def _run_osascript(script, timeout=45):
    p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    out, err = p.stdout.strip(), p.stderr.strip()
    if p.returncode != 0:
        # -1723 is the specific, actionable one: Apple Events reached the
        # browser but JavaScript execution is switched off.
        if "-1723" in err or "Access not allowed" in err:
            raise AppleEventsUnavailable(
                "the browser refused JavaScript from Apple Events. Enable it in the window "
                "you actually browse in: Chrome -> View -> Developer -> Allow JavaScript from "
                "Apple Events (Safari: Develop -> Allow JavaScript from Apple Events). "
                "NOTE: Chrome stores this PER PROFILE, so having switched it on in another "
                "profile does not count — and it is easy to miss, because every profile shows "
                "the same menu. " + _profile_hint()
            )
        if "-1743" in err or "Not authorized" in err:
            raise AppleEventsUnavailable(
                "macOS has not granted permission to control the browser. Approve the prompt, "
                "or enable it in System Settings -> Privacy & Security -> Automation."
            )
        raise AppleEventsUnavailable(err or "osascript failed")
    return out


def running_browser():
    """The first supported, currently-running browser, or None.

    Deliberately does not launch anything: the whole point is to use the
    session the user already has open.
    """
    try:
        running = subprocess.run(["osascript", "-e",
                                  'tell application "System Events" to get name of every process'],
                                 capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return None
    names = {n.strip() for n in running.split(",")}
    for b in CHROMIUM_BROWSERS + [SAFARI]:
        if b in names:
            return b
    return None


def available():
    """True when this transport has some chance of working."""
    return plat.is_macos() and running_browser() is not None


# --------------------------------------------------------------------- JS execution ---------
def _js_in_browser(browser, js):
    """Run `js` in the browser's active tab and return its result as text."""
    if browser == SAFARI:
        tmpl = 'tell application "%s" to do JavaScript "%s" in current tab of window 1'
    else:
        tmpl = 'tell application "%s" to execute javascript "%s" in active tab of front window'
    return _run_osascript(tmpl % (browser, _as_string(js)))


def _wrapped(fn_names, call_expr):
    """Build a self-contained IIFE: the page functions, then one call.

    The result is JSON-encoded because Apple Events marshals only simple
    scalars — an object would come back as an unusable AppleScript record.
    """
    defs = "\n".join(page_function(n) for n in fn_names)
    return (
        "(function(){try{%s\nvar __r=%s;"
        "return typeof __r===\"string\"?__r:JSON.stringify(__r);}"
        "catch(e){return \"__COLLIE_ERR__\"+(e&&e.message||String(e));}})()"
        % (defs, call_expr)
    )


def _js_args(args):
    return ",".join(json.dumps(a) for a in args)


def _unwrap(raw):
    if raw.startswith("__COLLIE_ERR__"):
        return {"ok": True, "data": {"error": raw[len("__COLLIE_ERR__"):]}}
    return {"ok": True, "data": raw}


# --------------------------------------------------------------------- the transport --------
def call(cmd, timeout=45):
    """Serve one bridge command. Same envelope as `browserbridge._call`."""
    action = cmd.get("action", "")
    if action in _UNSUPPORTED:
        return {"ok": False, "error": "%s Load harness/browser_ext/ in your browser." % _UNSUPPORTED[action]}

    browser = running_browser()
    if not browser:
        return {"ok": False, "error": "no supported browser is running (Chrome, Brave, Edge, Arc or Safari)."}

    try:
        if action == "show":
            _run_osascript('tell application "%s" to activate' % browser)
            return {"ok": True, "data": "activated %s" % browser}

        if action == "open":
            url = cmd.get("url", "")
            if not url:
                return {"ok": False, "error": "open needs a url"}
            _open_url(browser, url)
            js = _wrapped(["pageRead"], "pageRead()")
            return _unwrap(_js_in_browser(browser, js))

        if action in _SYNC_ACTIONS:
            fn, argf = _SYNC_ACTIONS[action]
            js = _wrapped([fn], "%s(%s)" % (fn, _js_args(argf(cmd))))
            return _unwrap(_js_in_browser(browser, js))

        if action == "click":
            if cmd.get("ref"):
                js = _wrapped(["pageClickRef"], "pageClickRef(%s)" % json.dumps(cmd["ref"]))
            else:
                js = _wrapped(["pageClick"], "pageClick(%s,%s)"
                              % (json.dumps(cmd.get("text", "")), json.dumps(cmd.get("selector", ""))))
            return _unwrap(_js_in_browser(browser, js))

        if action == "type":
            text, submit = cmd.get("text", ""), bool(cmd.get("submit"))
            if cmd.get("ref"):
                js = _wrapped(["pageTypeRef"], "pageTypeRef(%s,%s,%s)"
                              % (json.dumps(cmd["ref"]), json.dumps(text), json.dumps(submit)))
            elif cmd.get("label"):
                js = _wrapped(["pageTypeLabel"], "pageTypeLabel(%s,%s)"
                              % (json.dumps(cmd["label"]), json.dumps(text)))
            else:
                js = _wrapped(["pageType"], "pageType(%s,%s,%s)"
                              % (json.dumps(cmd.get("selector", "")), json.dumps(text), json.dumps(submit)))
            return _unwrap(_js_in_browser(browser, js))

        return {"ok": False, "error": "unknown action %s" % action}
    except AppleEventsUnavailable as e:
        return {"ok": False, "error": str(e)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "the browser did not respond in %ds" % timeout}


def _open_url(browser, url):
    """Navigate the active tab and wait for the load to finish.

    Without the wait the page functions run against the previous document and
    return stale text, which is worse than an error because it looks correct.
    """
    if browser == SAFARI:
        nav = ('tell application "Safari"\n'
               '  set URL of current tab of window 1 to "%s"\n'
               'end tell') % _as_string(url)
        ready = ('tell application "Safari" to do JavaScript "document.readyState" '
                 'in current tab of window 1')
    else:
        nav = ('tell application "%s"\n'
               '  set URL of active tab of front window to "%s"\n'
               'end tell') % (browser, _as_string(url))
        ready = ('tell application "%s" to execute javascript "document.readyState" '
                 'in active tab of front window') % browser
    _run_osascript(nav)

    # Poll rather than sleeping a fixed amount: fast pages should not cost a
    # fixed penalty, and slow ones should not be truncated.
    deadline_script = (
        'set t to 0\n'
        'repeat until t > 100\n'
        '  try\n'
        '    if (%s) is "complete" then exit repeat\n'
        '  end try\n'
        '  delay 0.15\n'
        '  set t to t + 1\n'
        'end repeat' % ready
    )
    try:
        _run_osascript(deadline_script, timeout=30)
    except AppleEventsUnavailable:
        # readyState needs the JavaScript-from-Apple-Events switch; if it is
        # off, the caller will get the same clear error from the read below.
        pass
