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
    check(m["features"]["bot_user"]["display_name"] == "rowan",
          "and its handle is the name, lowercased — that is what gets @-ed")
    check(sorted(m["oauth_config"]["scopes"]["bot"]) == ["app_mentions:read", "chat:write"],
          "hear an @ and answer it: the two scopes, no more")
    check("user" not in m["oauth_config"]["scopes"],
          "NO user scopes — they switch on token rotation, which disables the Install button and "
          "forces an OAuth redirect that then refuses bot scopes on loopback")
    check(m["settings"]["socket_mode_enabled"] is True, "Socket Mode, so a laptop exposes nothing")
    check(m["settings"]["event_subscriptions"]["bot_events"] == ["app_mention"],
          "and the one event it exists to receive")
    check(sb.app_manifest("Odd Name!")["features"]["bot_user"]["display_name"] == "oddname",
          "a handle Slack will accept, whatever the dog is called")

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
