"""Slack Socket Mode listener — @ a collie in a channel and it goes to work.

Why Socket Mode and not a webhook. Event Subscriptions need a publicly reachable
HTTPS URL, and the machines this runs on are laptops: behind NAT, asleep half the
day, on an address that changes with the café. That means a tunnel or a relay
Worker, which is two more things that can be down while looking fine. Socket Mode
inverts it — *we* dial out to Slack over a WebSocket and events arrive on that
connection. Nothing to expose, nothing to forward, and a laptop that changes
networks just reconnects.

Zero third-party dependencies, like the rest of the core (`dependencies = []` in
pyproject): the WebSocket half is `harness/wsclient.py`, already written for the
remote relay, and the Web API half is four `urllib` POSTs.

The identity question, and why the dogs have names. "collie-mac" and "collie-win"
stop working the moment two people both have a Mac, and they read like serial
numbers. A collie is a working dog and the pack is already in this codebase
(`collie pack`), so each instance is a dog with a name it keeps: `@Rowan` is the
one on a particular machine no matter what that machine is called, and a person
can hold that in their head. The name is chosen once, stored, and announced along
with where it lives and — this matters more than the name — **what it is allowed
to do**, so its autonomy is never something you find out afterwards.
"""
from __future__ import annotations

import json
import os
import queue
import re
import socket as _socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import wsclient

SLACK_API = "https://slack.com/api/"
IDENTITY = os.path.expanduser("~/.collie/identity.json")
QUEUE_DIR = os.path.expanduser("~/.collie/")
STORE = os.path.expanduser("~/.collie/slack.json")     # this dog's app id and its two tokens

# Herding names, because a collie answers to one. Kept short and sayable — this
# is a name a human types twenty times a day, so nothing that needs spelling out.
KENNEL = [
    "Rowan", "Meg", "Bracken", "Nell", "Fly", "Tess", "Moss", "Gwen",
    "Cap", "Jess", "Pip", "Skye", "Roy", "Bess", "Glen", "Juno",
]

# Autonomy is a setting, not a policy this file gets to invent. It is stated in
# the greeting and in `who`, because the only unacceptable version of this is a
# boundary the owner discovers by watching it get crossed.
AUTONOMY = {
    "propose": "reads and reports — writes nothing",
    "branch": "works on a branch and pushes there; main is yours",
    "main": "works and pushes to main",
}


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

def _hostname() -> str:
    try:
        return _socket.gethostname().split(".")[0]
    except Exception:
        return "unknown"


def machine_label() -> str:
    """The machine, as a person would say it: "MacBook-Pro", not a serial.

    Derived at run time and never stored with the name, because the point of the
    name is that it survives moving to another machine — and the moment it does,
    a stored machine label would be a lie that nobody in the channel can see.
    """
    h = _hostname()
    # "Sinings-MacBook-Pro" → "MacBook-Pro": the owner's name is already obvious
    # from whose channel it is, and dropping it keeps the line short enough to
    # sit in front of every message.
    h = re.sub(r"^[A-Za-z]+s?[-_]", "", h)
    return (h or "unknown")[:24]


def fingerprint() -> str:
    """Four hex characters that stay put across renames.

    Only needed when two machines would otherwise read the same — two identical
    MacBook Pros in one channel is not a hypothetical. Kept out of the everyday
    line and shown in `who`, because an id in front of every message is noise
    until the day it is the only thing that disambiguates.
    """
    raw = ""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                                 capture_output=True, text=True, timeout=5).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            raw = m.group(1) if m else ""
        elif sys.platform == "win32":
            out = subprocess.run(["reg", "query", r"HKLM\SOFTWARE\Microsoft\Cryptography",
                                  "/v", "MachineGuid"], capture_output=True, text=True, timeout=5).stdout
            m = re.search(r"MachineGuid\s+REG_SZ\s+(\S+)", out)
            raw = m.group(1) if m else ""
        else:
            with open("/etc/machine-id", encoding="utf-8") as f:
                raw = f.read().strip()
    except Exception:
        raw = ""
    if not raw:
        raw = _hostname() + str(os.getuid() if hasattr(os, "getuid") else "")
    import hashlib
    return hashlib.sha256(raw.encode()).hexdigest()[:4]


# ---------------------------------------------------------------------------
# The kennel — provisioning a pack, one app per dog
# ---------------------------------------------------------------------------
#
# Slack's identity model is one app = one bot user = one @handle. There is no
# arrangement of a single app that gives `@rowan` and `@juno` their own
# autocomplete, their own mentions, and their own avatar — so a pack whose
# members can be addressed separately needs an app EACH. What makes that
# affordable is that an app can be created from a manifest over the API: the
# per-dog cost becomes one command instead of a tour through six settings pages.
#
# Bot-only, on purpose, and not as a simplification. The moment an app carries
# user scopes Slack switches on token rotation, and a rotating app cannot be
# installed from the button — it must go through an OAuth redirect, and Slack
# then refuses bot scopes on a loopback one ("Bot scopes are not allowed when
# redirecting to a non-web URI"). Three rules that close on each other, verified
# against the live endpoints. A bot-only app is the only shape that installs
# without the user owning a public https endpoint. The MCP side keeps its own
# app and its own user token; the two never share a credential.

def app_manifest(name: str) -> dict:
    """The whole app for one dog: it hears an @, it answers, nothing else."""
    handle = re.sub(r"[^a-z0-9_-]+", "", name.lower()) or "collie"
    return {
        "display_information": {
            "name": name,
            "description": "A collie you can @ in a channel — it takes the ask and goes to work",
            "background_color": "#2c2d30",
        },
        "features": {
            "bot_user": {"display_name": handle, "always_online": False},
            # Without a messages tab the bot has no App Home, and a DM to it goes nowhere.
            "app_home": {"messages_tab_enabled": True, "messages_tab_read_only_enabled": False},
        },
        # channels:join is the third and last one: it lets the dog walk into the public channels it
        # was told to work in instead of standing outside until somebody remembers to `/invite` it.
        # The permission it grants is the one the owner exercises anyway by typing that command —
        # and it cannot reach a private channel, where an invitation is still the only way in.
        "oauth_config": {"scopes": {"bot": ["app_mentions:read", "chat:write", "channels:join"]}},
        "settings": {
            # Socket Mode means this dog dials OUT: no public address, no tunnel, and a laptop
            # that changes network just reconnects. It also makes Slack mint the app-level
            # token for us, which is one fewer thing to go and fetch by hand.
            "socket_mode_enabled": True,
            "event_subscriptions": {"bot_events": ["app_mention"]},
            "interactivity": {"is_enabled": False},
            "org_deploy_enabled": False,
        },
    }


def load_kennel() -> dict:
    """Every dog this machine has papers for: name -> {app_id, bot_token, app_token, …}.

    Keyed by NAME rather than by machine: the point of the name is that it is the identity, and
    one machine can perfectly well run several dogs on different repositories.
    """
    try:
        with open(STORE, encoding="utf-8") as f:
            d = json.load(f) or {}
    except (OSError, ValueError):
        return {}
    return d.get("dogs") or {}


def save_kennel(dogs: dict) -> None:
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"dogs": dogs}, f, indent=2)
        f.write("\n")
    try:
        from . import plat
        plat.chmod_private(tmp)        # it holds two bearer tokens
    except Exception:
        pass
    os.replace(tmp, STORE)


def create_app(config_token: str, manifest: dict) -> dict:
    """apps.manifest.create — the whole app in one call. Returns Slack's payload."""
    r = api("apps.manifest.create", config_token, manifest=json.dumps(manifest))
    if not r.get("ok"):
        detail = r.get("errors") or r.get("error")
        raise RuntimeError("apps.manifest.create failed: %s" % json.dumps(detail))
    return r


def load_identity(name: str = "", autonomy: str = "") -> dict:
    """The dog's name, where it lives, and what it may do.

    The name is picked once and kept: deterministic from the hostname so two
    machines rarely collide, but written to disk immediately so it survives a
    rename of the machine. Anything passed in wins and is persisted, which is how
    someone renames a dog they do not like the name of.
    """
    ident = {}
    try:
        with open(IDENTITY, encoding="utf-8") as f:
            ident = json.load(f)
    except Exception:
        pass
    if name:
        ident["name"] = name
    if autonomy:
        ident["autonomy"] = autonomy
    if not ident.get("name"):
        host = _hostname()
        ident["name"] = KENNEL[sum(host.encode()) % len(KENNEL)]
        ident["_fresh"] = True   # so the first greeting can offer a rename, once
    ident.setdefault("autonomy", "branch")
    ident["machine"] = _hostname()
    ident["os"] = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, sys.platform)
    try:
        os.makedirs(os.path.dirname(IDENTITY), exist_ok=True)
        with open(IDENTITY, "w", encoding="utf-8") as f:
            # `_fresh` is a signal to this run, not a fact about the dog. Writing
            # it would make the greeting offer a rename on every single start —
            # charming once, and a tic by the third time.
            json.dump({k: v for k, v in ident.items() if not k.startswith("_")}, f, indent=2)
    except Exception:
        pass  # a read-only home is not a reason to refuse to work
    return ident


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------

class TaskQueue:
    """FIFO of asks, persisted so a restart does not silently drop work.

    Persistence is the whole point: a queue that lives in memory turns "I asked it
    an hour ago" into "it never heard me" the first time the process is restarted,
    and there is nothing on screen to tell the difference.
    """

    def __init__(self, name: str):
        self.path = os.path.join(QUEUE_DIR, "queue-%s.json" % name.lower())
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self.next_id = 1
        self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            self.items = d.get("items", [])
            self.next_id = d.get("next_id", 1)
        except Exception:
            self.items, self.next_id = [], 1

    def _save(self):
        try:
            os.makedirs(QUEUE_DIR, exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump({"items": self.items, "next_id": self.next_id}, f, indent=2)
        except Exception:
            pass

    def add(self, text: str, channel: str, thread: str, user: str) -> dict:
        with self._lock:
            item = {"id": self.next_id, "text": text, "channel": channel,
                    "thread": thread, "user": user, "state": "waiting",
                    "queued_at": time.time()}
            self.next_id += 1
            self.items.append(item)
            self._save()
            return item

    def take(self) -> dict | None:
        with self._lock:
            for it in self.items:
                if it["state"] == "waiting":
                    it["state"] = "running"
                    self._save()
                    return it
            return None

    def finish(self, task_id: int):
        with self._lock:
            self.items = [i for i in self.items if i["id"] != task_id]
            self._save()

    def drop(self, task_id: int) -> str:
        """Remove a task that has not started. A running one is not dropped from
        under itself — `stop` is the word for that, and conflating the two is how
        someone cancels a half-written commit by accident."""
        with self._lock:
            for it in self.items:
                if it["id"] == task_id:
                    if it["state"] == "running":
                        return "#%d is already running — say `stop` to interrupt it." % task_id
                    self.items.remove(it)
                    self._save()
                    return "dropped #%d" % task_id
            return "no #%d in the queue" % task_id

    def listing(self) -> str:
        with self._lock:
            if not self.items:
                return "queue is empty"
            out = []
            for it in self.items:
                mark = "▶" if it["state"] == "running" else "·"
                out.append("%s #%d  %s" % (mark, it["id"], it["text"][:70]))
            return "\n".join(out)

    def waiting(self) -> int:
        with self._lock:
            return sum(1 for i in self.items if i["state"] == "waiting")


# ---------------------------------------------------------------------------
# Slack Web API
# ---------------------------------------------------------------------------

def api(method: str, token: str, **params) -> dict:
    """One POST to the Slack Web API.

    Raises on a transport failure and returns the parsed body otherwise; Slack
    signals its own failures with `ok: false` in a 200, so the caller checks that.
    """
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        SLACK_API + method, data=data,
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def join(token: str, channel: str, name: str = "collie") -> str:
    """Walk into a channel. "" on success, else the reason, in words.

    Run on every start, not only the first: already being in the channel is a success, and the
    alternative — a dog that is connected, listening and simply not a member — is indistinguishable
    from a dog nobody has spoken to yet. That failure is silent on both ends, which is why it is
    worth a call that usually does nothing.
    """
    try:
        r = api("conversations.join", token, channel=channel)
    except Exception as e:
        return str(e)
    if r.get("ok") or r.get("error") == "already_in_channel":
        return ""
    if r.get("error") == "missing_scope":
        return ("this app predates `channels:join` — reinstall it from its Slack app page to pick "
                "the scope up, or `/invite @%s` in the channel once" % name.lower())
    if r.get("error") == "method_not_supported_for_channel_type":
        return "private channel — nothing may let itself in; `/invite @%s` once" % name.lower()
    if r.get("error") == "channel_not_found":
        return "no such channel, or it is private and this app cannot see it"
    return str(r.get("error"))


def say(token: str, channel: str, text: str, thread: str = "", tag: str = "") -> None:
    """Reply, in the thread the ask arrived in.

    In-thread on purpose: one run's output is long, and a channel that fills with
    it stops being somewhere anyone reads.
    """
    try:
        p = {"channel": channel, "text": (tag + " — " + text) if tag else text}
        if thread:
            p["thread_ts"] = thread
        r = api("chat.postMessage", token, **p)
        if not r.get("ok"):
            print("[slack] postMessage failed: %s" % r.get("error"), file=sys.stderr)
    except Exception as e:
        print("[slack] postMessage error: %s" % e, file=sys.stderr)


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------

class Worker(threading.Thread):
    """Runs one task at a time, in this repository.

    One at a time is not laziness. Two runs in one working tree edit the same
    files, and the second one's diff would be built on the first one's half-done
    state — the queue exists precisely so a second ask waits rather than
    corrupting the first.
    """

    def __init__(self, q: TaskQueue, ident: dict, bot_token: str, cwd: str, provider: str):
        super().__init__(daemon=True)
        self.tag = "%s · %s" % (ident["name"], machine_label())
        # NOT self.ident: Worker is a Thread, and Thread.ident is a read-only property holding the
        # thread id. Assigning to it raises AttributeError in the constructor, which is why this
        # command has never started since it shipped — the crash is before the first connection, so
        # nothing ever reached Slack to show it was broken.
        self.q, self.dog, self.token = q, ident, bot_token
        self.cwd, self.provider = cwd, provider
        self.current: subprocess.Popen | None = None
        self._wake = threading.Event()

    def nudge(self):
        self._wake.set()

    def stop_current(self) -> str:
        p = self.current
        if not p or p.poll() is not None:
            return "nothing running"
        try:
            p.terminate()
            return "asked it to stop"
        except Exception as e:
            return "could not stop it: %s" % e

    def run(self):
        while True:
            item = self.q.take()
            if item is None:
                self._wake.wait(timeout=5)
                self._wake.clear()
                continue
            self._run_one(item)

    def _run_one(self, item):
        ch, th = item["channel"], item["thread"]
        say(self.token, ch, "on it — #%d" % item["id"], th, self.tag)
        cmd = [sys.executable, "-m", "harness.cli", "run", "--task", item["text"]]
        if self.provider:
            cmd += ["--provider", self.provider]
        try:
            self.current = subprocess.Popen(
                cmd, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            out, _ = self.current.communicate()
            rc = self.current.returncode
        except Exception as e:
            out, rc = str(e), -1
        finally:
            self.current = None

        out = (out or "").strip()
        # Slack rejects a message over 40k; keeping the tail keeps the conclusion,
        # which is the part anyone reads.
        if len(out) > 3500:
            out = "…(trimmed)…\n" + out[-3500:]
        head = "#%d done" % item["id"] if rc == 0 else "#%d failed (exit %s)" % (item["id"], rc)
        say(self.token, ch, "%s\n```\n%s\n```" % (head, out or "(no output)"), th, self.tag)
        self.q.finish(item["id"])


# ---------------------------------------------------------------------------
# Socket Mode
# ---------------------------------------------------------------------------

MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")


def _open_socket_url(app_token: str) -> str:
    r = api("apps.connections.open", app_token)
    if not r.get("ok"):
        raise RuntimeError(
            "apps.connections.open failed: %s — is this an app-level token (xapp-…) "
            "with connections:write, and is Socket Mode enabled on the app?" % r.get("error"))
    return r["url"]


def setup(argv=None) -> int:
    """`collie slack setup` — give one more dog its own app, its own handle, its own tokens.

    Run it again for the next dog. Nothing here is per-machine: the pack is keyed by name, so two
    dogs can live on one laptop working different repositories, and a name can move to another
    machine without Slack noticing.

    What cannot be automated, and why it is only two clicks: installing an app to a workspace has
    no API — the install IS the authorization, and Slack will not let a program grant itself one —
    and neither does reading back the two tokens it produces. Everything before that (the app, its
    scopes, Socket Mode, the event subscription) is one manifest call.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="collie slack setup")
    ap.add_argument("--name", default="", help="what to call this one (default: the next free kennel name)")
    ap.add_argument("--config-token", default=os.environ.get("SLACK_CONFIG_TOKEN", ""),
                    help="app-configuration token (xoxe.xoxp-…) from api.slack.com/apps")
    ap.add_argument("--bot-token", default="", help="xoxb-… , if you already have it")
    ap.add_argument("--app-token", default="", help="xapp-… , if you already have it")
    ap.add_argument("--list", action="store_true", help="show the pack and stop")
    a = ap.parse_args(argv)

    dogs = load_kennel()
    if a.list:
        if not dogs:
            print("(no dogs yet — `collie slack setup` gives you one)")
            return 0
        for n, d in sorted(dogs.items()):
            ready = "ready" if (d.get("bot_token") and d.get("app_token")) else "needs its tokens"
            print("  %-10s %-12s app %s" % (n, ready, d.get("app_id", "?")))
        return 0

    name = a.name or next((k for k in KENNEL if k.lower() not in
                           {d.lower() for d in dogs}), "Collie%d" % (len(dogs) + 1))
    if name in dogs and dogs[name].get("bot_token"):
        print("%s already has papers (app %s). Pick another name, or run "
              "`collie slack --name %s`." % (name, dogs[name].get("app_id", "?"), name))
        return 1

    entry = dict(dogs.get(name) or {})
    if not entry.get("app_id"):
        if not a.config_token:
            print("collie slack setup: needs an app-configuration token.\n"
                  "  Get one at https://api.slack.com/apps → 'Your App Configuration Tokens' →\n"
                  "  Generate Token, then re-run with --config-token xoxe.xoxp-… (or set\n"
                  "  SLACK_CONFIG_TOKEN). It is the one credential Slack has no API to mint, and\n"
                  "  it expires in 12 hours — it is used here once and never stored.",
                  file=sys.stderr)
            return 2
        print("creating the app for %s…" % name)
        res = create_app(a.config_token, app_manifest(name))
        entry["app_id"] = res.get("app_id", "")
        entry["team_id"] = (res.get("credentials") or {}).get("team_id", "")
        dogs[name] = entry
        save_kennel(dogs)
        print("  app %s created" % entry["app_id"])

    app_id = entry.get("app_id", "")
    install = "https://api.slack.com/apps/%s/install-on-team" % app_id
    tokens_page = "https://api.slack.com/apps/%s/general" % app_id
    entry["bot_token"] = a.bot_token or entry.get("bot_token", "")
    entry["app_token"] = a.app_token or entry.get("app_token", "")

    if not (entry["bot_token"] and entry["app_token"]):
        print("\ntwo clicks left, and they are the two Slack does not expose:\n"
              "  1. install %s to the workspace and Allow:\n     %s\n"
              "     then copy the Bot User OAuth Token (xoxb-…) from OAuth & Permissions\n"
              "  2. copy the app-level token (xapp-…), already generated by Socket Mode:\n     %s\n"
              % (name, install, tokens_page))
        asked = False
        if sys.stdin and sys.stdin.isatty():
            # isatty() can be true where stdin still reads EOF — a PowerShell child process, a
            # harness, a CI shell. Falling through to the printed instructions is the right
            # outcome; crashing with EOFError after having CREATED the app is not, because the
            # next run then meets an app it does not know it already made.
            try:
                entry["bot_token"] = entry["bot_token"] or input("  paste xoxb-…: ").strip()
                entry["app_token"] = entry["app_token"] or input("  paste xapp-…: ").strip()
                asked = True
            except (EOFError, KeyboardInterrupt):
                print("\n  (no console to paste into — carrying on without it)")
        if not asked and not (entry["bot_token"] and entry["app_token"]):
            print("  then: collie slack setup --name %s --bot-token xoxb-… --app-token xapp-…" % name)
            dogs[name] = entry
            save_kennel(dogs)
            return 3

    for label, tok, want in (("bot", entry["bot_token"], "xoxb-"), ("app", entry["app_token"], "xapp-")):
        if tok and not tok.startswith(want):
            print("that %s token does not start with %s — check you copied the right box"
                  % (label, want), file=sys.stderr)
            return 1
    dogs[name] = entry
    save_kennel(dogs)

    who = api("auth.test", entry["bot_token"])
    if not who.get("ok"):
        print("the bot token does not authenticate: %s" % who.get("error"), file=sys.stderr)
        return 1
    print("\n%s is ready — @%s in %s. Start it with:\n  collie slack --name %s --announce <channel id>"
          % (name, who.get("user", name.lower()), who.get("team", "your workspace"), name))
    return 0


def main(argv=None) -> int:
    import argparse
    if argv and argv[0] == "setup":
        return setup(argv[1:])
    ap = argparse.ArgumentParser(prog="collie slack")
    ap.add_argument("--name", default="", help="name this collie answers to (kept)")
    ap.add_argument("--autonomy", default="", choices=["", "propose", "branch", "main"])
    ap.add_argument("--cwd", default=os.getcwd(), help="repository it works in")
    ap.add_argument("--provider", default=os.environ.get("COLLIE_PROVIDER", ""))
    ap.add_argument("--announce", default="", help="channel id to say hello in")
    ap.add_argument("--channels", default=os.environ.get("COLLIE_SLACK_CHANNELS", ""),
                    help="comma-separated channel ids it will work in (default: only --announce)")
    ap.add_argument("--allow", default=os.environ.get("COLLIE_SLACK_ALLOW", ""),
                    help="comma-separated slack user ids that may task it (default: anyone in those channels)")
    args = ap.parse_args(argv)

    # The kennel first, the environment second. A pack means several dogs with several pairs of
    # tokens, and one pair of environment variables cannot hold them — but an env var still wins
    # when it is set, because that is how you run a dog with credentials you keep somewhere else.
    dogs = load_kennel()
    kept = dogs.get(args.name) or (list(dogs.values())[0] if len(dogs) == 1 and not args.name else {})
    app_token = os.environ.get("SLACK_APP_TOKEN", "") or kept.get("app_token", "")
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "") or kept.get("bot_token", "")
    # Failing loudly here rather than connecting and going quiet: a bot that is
    # silently not listening looks exactly like a bot with nothing to do.
    missing = [n for n, v in (("SLACK_APP_TOKEN", app_token), ("SLACK_BOT_TOKEN", bot_token)) if not v]
    if missing:
        known = ", ".join(sorted(dogs)) or "none yet"
        print("collie slack: missing %s.\n"
              "  `collie slack setup` gives a dog its own app and fills these in for you.\n"
              "  dogs on this machine: %s\n"
              "  SLACK_APP_TOKEN is the app-level token (xapp-…) with connections:write.\n"
              "  SLACK_BOT_TOKEN is the bot token (xoxb-…) with app_mentions:read and chat:write."
              % (" and ".join(missing), known), file=sys.stderr)
        return 2

    # Where it will work at all. Defaulting to "only the channel I was announced
    # in" rather than "anywhere I am invited": a bot dropped into another channel
    # by a colleague would otherwise arrive already able to drive this machine,
    # and nobody involved would think of that as granting access.
    channels = {c.strip() for c in (args.channels or args.announce).split(",") if c.strip()}
    allowed = {u.strip() for u in args.allow.split(",") if u.strip()}

    ident = load_identity(args.name, args.autonomy)

    # Into those channels, under its own steam. Not fatal when it fails: a private channel it was
    # already invited to works perfectly, and a dog that refuses to start over a channel it can
    # already hear would be trading a working pack for a tidy rule.
    for ch in sorted(channels):
        err = join(bot_token, ch, ident["name"])
        if err:
            print("[slack] %s: %s" % (ch, err), file=sys.stderr)
        else:
            print("[slack] in %s" % ch)

    q = TaskQueue(ident["name"])
    worker = Worker(q, ident, bot_token, args.cwd, args.provider)
    worker.start()

    # What every message is signed with. Name for who, machine for where — the
    # machine part is recomputed on each start, so moving the name to another
    # laptop changes what the channel sees rather than quietly lying.
    tag = "%s · %s" % (ident["name"], machine_label())
    who = ("*%s* on *%s* (%s · %s), working in `%s`\nautonomy: *%s* — %s\nscope: %s · %s" % (
        ident["name"], machine_label(), ident["os"], fingerprint(), args.cwd,
        ident["autonomy"], AUTONOMY.get(ident["autonomy"], "?"),
        ("%d channel(s)" % len(channels)) if channels else "*any channel I am invited to*",
        ("%d person(s)" % len(allowed)) if allowed else "anyone in them"))
    print(who.replace("*", ""))
    if args.announce:
        first = ident.pop("_fresh", False)
        hello = who + ("\n_reporting in. I picked the name myself — say `rename <name>` if you would rather._"
                       if first else "\n_reporting in._")
        say(bot_token, args.announce, hello, tag=tag)

    seen: set[str] = set()          # envelope ids, for Slack's redeliveries
    seen_order: list[str] = []

    while True:
        try:
            url = _open_socket_url(app_token)
            ws = wsclient.WebSocketClient.connect(url)
            print("[slack] connected as %s" % ident["name"])
        except Exception as e:
            print("[slack] connect failed: %s — retrying in 10s" % e, file=sys.stderr)
            time.sleep(10)
            continue

        try:
            while True:
                msg = ws.recv_message()
                if msg is None:
                    break
                op, data = msg if isinstance(msg, tuple) else (1, msg)
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                try:
                    env = json.loads(data)
                except Exception:
                    continue

                env_id = env.get("envelope_id")
                if env_id:
                    # Ack first and always. Slack re-delivers anything unacked
                    # within three seconds, and an ack sent *after* the work would
                    # mean every slow task runs twice.
                    try:
                        ws.send_text(json.dumps({"envelope_id": env_id}))
                    except Exception:
                        pass
                    if env_id in seen:
                        continue
                    seen.add(env_id)
                    seen_order.append(env_id)
                    if len(seen_order) > 500:
                        seen.discard(seen_order.pop(0))

                if env.get("type") != "events_api":
                    continue
                event = (env.get("payload") or {}).get("event") or {}
                if event.get("type") != "app_mention" or event.get("bot_id"):
                    continue

                text = MENTION_RE.sub("", event.get("text", "")).strip()
                ch = event.get("channel", "")
                th = event.get("thread_ts") or event.get("ts") or ""
                user = event.get("user", "")
                low = text.lower()

                # Two gates, and they are checked before the text is read as
                # anything. Out of scope is answered rather than ignored: a bot
                # that goes silent reads as broken, and someone will debug it by
                # inviting it somewhere else.
                if channels and ch not in channels:
                    say(bot_token, ch, "I only work in the channel I was set up in.", th, tag)
                    continue
                if allowed and user not in allowed:
                    say(bot_token, ch, "I take work from %s here." %
                        ", ".join("<@%s>" % u for u in sorted(allowed)), th, tag)
                    continue

                if low.startswith("rename "):
                    new = text.split(None, 1)[1].strip()[:24]
                    if not new.isalnum():
                        say(bot_token, ch, "a name with letters and digits only, please", th, tag)
                    else:
                        load_identity(name=new)
                        say(bot_token, ch,
                            "I answer to *%s* now — restart me so Slack sees it too." % new, th, tag)
                elif low in ("who", "who?", "status"):
                    say(bot_token, ch, "%s\n%d waiting" % (who, q.waiting()), th, tag)
                elif low in ("queue", "q", "queue?"):
                    say(bot_token, ch, "```\n%s\n```" % q.listing(), th, tag)
                elif low == "stop":
                    say(bot_token, ch, worker.stop_current(), th, tag)
                elif low.startswith("drop "):
                    try:
                        say(bot_token, ch, q.drop(int(low.split()[1])), th, tag)
                    except (ValueError, IndexError):
                        say(bot_token, ch, "say `drop <id>` — the ids are in `queue`", th, tag)
                elif not text:
                    say(bot_token, ch, "%s here. Ask me something, or say `queue`." % ident["name"], th, tag)
                else:
                    item = q.add(text, ch, th, user)
                    worker.nudge()
                    ahead = q.waiting() - 1
                    say(bot_token, ch,
                        "queued #%d%s" % (item["id"], "" if ahead <= 0 else " — %d ahead of it" % ahead),
                        th, tag)
        except Exception as e:
            print("[slack] connection lost (%s) — reconnecting" % e, file=sys.stderr)
        finally:
            try:
                ws.close()
            except Exception:
                pass
        time.sleep(2)
