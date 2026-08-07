"""Which dog is on the other end of this connection.

CollieIOS pairs with a machine and shows it as an address. That was enough while a machine WAS the
thing you talked to; the pack made it wrong, because one laptop runs several dogs in several
repositories and a phone holding one pairing cannot say which of them it is about to task.

So the web surface introduces itself the way the channel greeting does — name, machine, repo — and
serves that dog's own face, drawn by the one generator rather than re-derived on the client.

    python3 tests/test_whoami.py
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def main():
    from harness import webapp, slackbot

    real_kennel, real_name = slackbot.load_kennel, webapp.DOG_NAME
    try:
        # --name wins, always: it is the only thing that can be right when a machine has several.
        webapp.DOG_NAME = "BigMac"
        slackbot.load_kennel = lambda: {"BigMac": {}, "Juno": {}}
        me = webapp.whoami()
        check(me["name"] == "BigMac", "--name names the dog this server speaks for")
        check(me["machine"] and me["os"] and me["fingerprint"],
              "with the machine, its OS and the fingerprint that survives a rename")
        check(me["repo"] == os.getcwd(), "and the repository it is standing in")
        check(me["avatar"] == "/api/avatar.png", "pointing at a face served from here")
        check("autonomy" not in me,
              "and NO autonomy: this server does not enforce one, and a limit it only states is "
              "the defect that was just taken out of the Slack side")
        check("token" not in json.dumps(me).lower(),
              "nothing in the payload is a credential")

        # One dog in the kennel is an obvious default. Several is not, and guessing there is
        # indistinguishable from guessing wrong.
        webapp.DOG_NAME = ""
        slackbot.load_kennel = lambda: {"BigMac": {}}
        check(webapp.whoami()["name"] == "BigMac", "one dog in the kennel needs no flag")
        slackbot.load_kennel = lambda: {"BigMac": {}, "Juno": {}}
        check(webapp.whoami()["name"] == "", "several, and it declines to pick — the phone falls "
              "back to the machine label rather than being told the wrong name")
        slackbot.load_kennel = lambda: (_ for _ in ()).throw(OSError("no kennel"))
        check(webapp.whoami()["name"] == "", "an unreadable kennel is unnamed, not an exception")
    finally:
        slackbot.load_kennel, webapp.DOG_NAME = real_kennel, real_name

    # The face: same generator as Slack's, so one dog is one colour everywhere.
    from harness import avatar
    a = avatar.png("BigMac")
    check(a[:8] == b"\x89PNG\r\n\x1a\n", "the avatar endpoint has a PNG to serve")
    check(avatar.png("BigMac") == a, "identical for the same name — a face is not a random draw")
    check(avatar.png("Juno") != a, "and different for a different one, which is the whole point")

    src = open(os.path.join(ROOT, "harness", "webapp.py"), encoding="utf-8").read()
    check('path == "/api/whoami"' in src and 'path == "/api/avatar.png"' in src,
          "both are routed")
    i_gate = src.find("_peer_ok")
    check(0 < i_gate < src.find('path == "/api/whoami"'),
          "behind the pairing gate: which dog this is, and which repo it stands in, is not public")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "whoami: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
