"""Provisioning a pack: `collie slack setup` (harness/slackbot.py).

Slack's identity model is one app = one bot user = one @handle, so several dogs that can be
addressed separately need an app each. These checks pin the shape that makes that affordable and
installable, and the refusals that keep a half-provisioned dog from looking ready.

    python3 tests/test_slack_setup.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def main():
    from harness import slackbot as sb

    tmp = tempfile.mkdtemp(prefix="collie_kennel_")
    sb.STORE = os.path.join(tmp, "slack.json")

    # ---- the manifest: exactly what a dog needs, and nothing that costs it the install button ----
    m = sb.app_manifest("Rowan")
    check(m["display_information"]["name"] == "Rowan", "the app is named after the dog")
    check(m["features"]["bot_user"]["display_name"] == "Rowan",
          "and the displayed name keeps its capital — Slack lowercases the @handle itself, so "
          "sending it pre-lowercased only cost the one place the name is shown")
    check(sorted(m["oauth_config"]["scopes"]["bot"])
          == ["app_mentions:read", "channels:join", "chat:write"],
          "hear an @, answer it, and let itself into a public channel: those three, no more")
    check("user" not in m["oauth_config"]["scopes"],
          "NO user scopes — they switch on token rotation, which disables the Install button and "
          "forces an OAuth redirect that then refuses bot scopes on loopback")
    check(m["settings"]["socket_mode_enabled"] is True, "Socket Mode, so a laptop exposes nothing")
    check(m["settings"]["event_subscriptions"]["bot_events"] == ["app_mention"],
          "and the one event it exists to receive")
    check(sb.app_manifest("Odd Name!")["features"]["bot_user"]["display_name"] == "OddName",
          "a name Slack will accept whatever the dog is called — the filter stays, the case goes")

    # ---- letting itself in -------------------------------------------------------------------
    # The scope above is only worth having if start-up actually uses it, and if the ways it can
    # fail come back as something to do rather than as Slack's method name.
    calls = []

    def fake_api(method, token, **params):
        calls.append((method, params))
        return fake_api.reply
    real_api, sb.api = sb.api, fake_api

    fake_api.reply = {"ok": True}
    check(sb.join("xoxb-1", "C123", "Rowan") == "", "a clean join reports nothing to report")
    check(calls[-1] == ("conversations.join", {"channel": "C123"}),
          "by asking Slack for that channel and no other")

    fake_api.reply = {"ok": False, "error": "already_in_channel"}
    check(sb.join("xoxb-1", "C123", "Rowan") == "",
          "already being in it is success — so every start can try, not just the first")

    fake_api.reply = {"ok": False, "error": "missing_scope"}
    check("reinstall" in sb.join("xoxb-1", "C123", "Rowan"),
          "a dog provisioned before this scope existed is told to reinstall, not left guessing")

    fake_api.reply = {"ok": False, "error": "method_not_supported_for_channel_type"}
    check("/invite @rowan" in sb.join("xoxb-1", "C1", "Rowan"),
          "a private channel says the one thing that does work there")
    sb.api = real_api

    # ---- the face ------------------------------------------------------------------------------
    # Shipped inside the wheel, not read out of the repo: `pip install collie-harness` has no
    # assets/ directory, and an icon that only exists for developers is the same as no icon.
    check(os.path.exists(sb.ICON), "the icon ships with the package (%s)" % os.path.basename(sb.ICON))
    head = open(sb.ICON, "rb").read(24)
    check(head[:8] == b"\x89PNG\r\n\x1a\n", "and it is a PNG")
    w = int.from_bytes(head[16:20], "big")
    h = int.from_bytes(head[20:24], "big")
    check(w == h and w >= 512, "square and at least 512px — Slack refuses smaller (%dx%d)" % (w, h))

    posted = {}

    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        posted["ctype"] = req.headers.get("Content-type") or req.get_header("Content-type")
        posted["auth"] = req.get_header("Authorization")
        posted["url"] = req.full_url
        posted["body"] = req.data
        return _Resp(posted.get("reply", b'{"ok":true}'))
    real_urlopen = sb.urllib.request.urlopen
    sb.urllib.request.urlopen = fake_urlopen

    check(sb.set_icon("xoxe.xoxp-1", "A0BIGMAC") == "", "a successful upload reports nothing")
    check(posted["url"].endswith("apps.icon.set"), "via apps.icon.set")
    check(posted["auth"] == "Bearer xoxe.xoxp-1", "authenticated with the app-configuration token")
    check("multipart/form-data" in (posted["ctype"] or ""), "as multipart, which is what it wants")
    check(b'name="app_id"' in posted["body"] and b"A0BIGMAC" in posted["body"], "carrying the app id")
    check(b'name="file"' in posted["body"] and b"\x89PNG" in posted["body"], "and the PNG itself")

    posted["reply"] = b'{"ok":false,"error":"invalid_icon_size"}'
    check(sb.set_icon("xoxe.xoxp-1", "A0BIGMAC") == "invalid_icon_size",
          "a refusal comes back as Slack's reason")
    sb.urllib.request.urlopen = real_urlopen
    check(sb.set_icon("xoxe.xoxp-1", "A0BIGMAC", "/nope/missing.png").startswith("[Errno"),
          "and a missing file is reported, not raised — an undocumented endpoint must never be "
          "the thing that ends a setup")

    # ---- the kennel holds a PACK, keyed by name ---------------------------------------------
    check(sb.load_kennel() == {}, "an empty kennel reads as empty, not as an error")
    sb.save_kennel({"Rowan": {"app_id": "A1", "bot_token": "xoxb-1", "app_token": "xapp-1"},
                    "Juno": {"app_id": "A2"}})
    back = sb.load_kennel()
    check(set(back) == {"Rowan", "Juno"}, "two dogs on one machine, side by side")
    check(back["Rowan"]["app_id"] == "A1", "each with its own app")

    # ---- setup refuses, in the two ways that matter -----------------------------------------
    import io
    import contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = sb.setup(["--name", "Bracken"])            # no config token, no app yet
    check(rc == 2 and "app-configuration token" in err.getvalue(),
          "without a config token it says which credential is missing and where to get it")
    check("Bracken" not in sb.load_kennel(),
          "and writes nothing — a dog in the list that cannot start is worse than no dog")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sb.setup(["--name", "Rowan"])
    check(rc == 1 and "already has papers" in out.getvalue(),
          "a name that is already provisioned is refused rather than silently re-created")

    # A dog whose app exists but whose tokens do not: setup must say what is left, keep the app id,
    # and exit non-zero so a script does not read it as finished. stdin is swapped for a
    # non-tty so this takes the unattended path rather than stopping to prompt.
    sb.save_kennel({"Juno": {"app_id": "A2"}})
    real_stdin, sys.stdin = sys.stdin, io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sb.setup(["--name", "Juno"])
    text = out.getvalue()
    check(rc == 3, "a dog still missing its tokens exits non-zero")
    check("install-on-team" in text and "A2" in text, "and points at that app's install page")
    check(sb.load_kennel()["Juno"]["app_id"] == "A2", "keeping the app it already has")

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = sb.setup(["--list"])
    listing = out.getvalue()
    check("Juno" in listing and "needs its tokens" in listing,
          "--list distinguishes a ready dog from a half-provisioned one")

    # ---- a pasted token that is obviously the wrong box ---------------------------------------
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = sb.setup(["--name", "Juno", "--bot-token", "xoxe.xoxp-nope", "--app-token", "xapp-2"])
    check(rc == 1 and "xoxb-" in err.getvalue(),
          "a user token pasted into the bot box is caught before it is stored")
    sys.stdin = real_stdin

    print("\n  " + ("%d FAILED" % len(fails) if fails else "slack setup: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
