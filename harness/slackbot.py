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

import base64
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

# What each autonomy BOUNDS, as opposed to what it announces. Until this existed the setting was
# a sentence in the greeting and nothing else: `ident["autonomy"]` appeared once, in the hello
# message, while the run was spawned with no --mode at all and took the gate's default. A dog
# introduced to a channel as "propose — writes nothing" could write anything, and the one promise
# the greeting makes that matters was the one nothing kept.
#
# The gate has a single axis — may this run change things — so propose maps to plan (read-only)
# and the other two to project. branch-vs-main is a git DESTINATION, which no gate mode can
# express; that half travels in the identity text as an instruction to the model, and is called
# an instruction below rather than dressed up as a wall.
AUTONOMY_MODE = {"propose": "plan", "branch": "project", "main": "project"}


def identity_text(ident: dict, peers: str = "") -> str:
    """Who the dog is, and who else is in the room, for the system prompt of the run it spawns.

    A pack whose whole premise is that members have names a person can hold in their head, and
    the member did not know its own: the name reached the Slack tag and stopped there.

    The name is stated as a NAME and the breed separately, because the first version said "You are
    Cornetto, a collie" and the dog, asked in Chinese to greet the others, introduced itself as
    "collie" — it read the apposition as the answer to "what are you called". Both facts are still
    here; only the sentence that let them be confused is gone.
    """
    a = ident.get("autonomy", "branch")
    lines = [
        "Your name is %s. Introduce yourself by that name — 'collie' is what you are (a coding "
        "agent), not what you are called." % ident.get("name", "collie"),
        "You work in a repository on %s (%s)." % (ident.get("machine", "this machine"),
                                                  ident.get("os", "")),
        "You are reached by @mention in a Slack channel, so answer briefly and say what you did.",
        "Your autonomy is '%s': %s." % (a, AUTONOMY.get(a, "?")),
    ]
    if a == "branch":
        lines.append("Do not push to main. Put the work on a branch and push that.")
    if peers:
        lines.append(peers)
    return "\n".join(lines)


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
    # Keep the capital. Slack lowercases the @handle ITSELF — the bot user came back from
    # auth.test as `cornetto` whether or not we sent it that way — so pre-lowercasing here bought
    # nothing and spent the one place the name is shown with its capital. display_name is the
    # DISPLAYED name, not the handle; verified against the live endpoint (apps.manifest.update
    # accepted "Cornetto", stored "Cornetto", permissions_updated=false). The character filter
    # stays: a name like "Odd Name!" still has to arrive as something Slack will accept.
    handle = re.sub(r"[^A-Za-z0-9_.-]+", "", name) or "collie"
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
        # channels:join lets the dog walk into the public channels it was told to work in instead of
        # standing outside until somebody remembers to `/invite` it. The permission it grants is the
        # one the owner exercises anyway by typing that command — and it cannot reach a private
        # channel, where an invitation is still the only way in.
        #
        # channels:read + users:read are the pack's eyes, and the rule they follow is: a dog sees
        # what a PERSON IN THAT CHANNEL sees, and nothing else. A member can open the member list
        # and can look a name up in the directory; without these two a dog could be @-ed by another
        # dog and have no way to answer it, because addressing anyone in Slack means knowing a
        # <@U…> id and neither the id nor the name was reachable. Note what is NOT here:
        # users:read.email is a SEPARATE scope and is not requested, so what reaches the model is
        # the display name and whether the member is a bot — the member list, not the personnel file.
        "oauth_config": {"scopes": {"bot": ["app_mentions:read", "chat:write", "channels:join",
                                            "channels:read", "users:read"]}},
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


ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui", "collie-icon-512.png")


def set_icon(config_token: str, app_id: str, path: str = "") -> str:
    """Give the app a face while we are already holding the credential that can. "" on success.

    `apps.icon.set` is in no method list — the manifest has no icon field, and Slack's own CLI
    uploads one on deploy, so something had to exist. It takes `app_id` and a `file` part, and a
    square PNG of at least 512px (128 comes back `invalid_icon_size`).

    Undocumented means it may change without warning, so this reports and never raises: an app
    wearing Slack's grey default is a working app, and a setup that got everything else right
    should not end in a traceback over a picture.
    """
    path = path or ICON
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        return str(e)
    boundary = "----collie%s" % base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip("=")
    body = b"".join([
        ("--%s\r\nContent-Disposition: form-data; name=\"app_id\"\r\n\r\n%s\r\n" % (boundary, app_id)).encode(),
        ("--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"icon.png\"\r\n"
         "Content-Type: image/png\r\n\r\n" % boundary).encode(),
        blob, b"\r\n", ("--%s--\r\n" % boundary).encode()])
    req = urllib.request.Request(
        SLACK_API + "apps.icon.set", data=body,
        headers={"Content-Type": "multipart/form-data; boundary=" + boundary,
                 "Authorization": "Bearer " + config_token})
    try:
        r = json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))
    except Exception as e:
        return str(e)
    return "" if r.get("ok") else str(r.get("error"))


def create_app(config_token: str, manifest: dict) -> dict:
    """apps.manifest.create — the whole app in one call. Returns Slack's payload."""
    r = api("apps.manifest.create", config_token, manifest=json.dumps(manifest))
    if not r.get("ok"):
        detail = r.get("errors") or r.get("error")
        raise RuntimeError("apps.manifest.create failed: %s" % json.dumps(detail))
    return r


def update_app(config_token: str, app_id: str, manifest: dict) -> dict:
    """apps.manifest.update — the same whole-app call, aimed at one that already exists.

    `permissions_updated` in the reply is the part worth reading: true means the SCOPES changed and
    a reinstall has to grant them, false means the change was cosmetic and is already live. Both
    are ordinary outcomes, so both are reported rather than one being treated as a failure.
    """
    r = api("apps.manifest.update", config_token, app_id=app_id, manifest=json.dumps(manifest))
    if not r.get("ok"):
        detail = r.get("errors") or r.get("error")
        raise RuntimeError("apps.manifest.update failed: %s" % json.dumps(detail))
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

    def add(self, text: str, channel: str, thread: str, user: str, from_dog: bool = False) -> dict:
        with self._lock:
            item = {"id": self.next_id, "text": text, "channel": channel,
                    "thread": thread, "user": user, "state": "waiting",
                    "from_dog": from_dog,      # kept for `queue`/receipts: who is waiting on this
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


def api_q(method: str, token: str, **params) -> dict:
    """Same, FORM-encoded — for the query methods that will not read a JSON body.

    Slack's write methods (chat.postMessage, conversations.join) take application/json. Its
    lookup methods do not: conversations.members and users.info silently ignore a JSON body and
    answer `invalid_arguments — missing required field: channel` for a call that plainly carried
    one. Sent as a form they work. The split is Slack's, so it is a second function rather than a
    guess inside the first: a caller picks the encoding its method actually accepts.
    """
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    req = urllib.request.Request(
        SLACK_API + method, data=data.encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
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


# Who else is in the room. The rule is one sentence: a dog sees what a PERSON IN THAT CHANNEL sees.
#
# It is needed because addressing anyone in Slack means writing a <@U…> id, and a dog had no way to
# learn one. Asked to greet the two other dogs in its channel it answered "there is only one collie
# here, tell me what they are called" — which was accurate. The kennel could not help: it is keyed
# by name, holds no ids, and lives on ONE machine, while the pack is spread across several. Slack is
# the thing all of them share, so Slack is where the roster comes from.
_ROSTER_TTL = 120.0                 # seconds; a member list changes on human timescales
_roster_cache: dict = {}            # channel -> (fetched_at, [member, …])
_roster_warned = False              # the missing-scope note is worth saying once, not every ask


def roster(token: str, channel: str, now: float = 0.0) -> list:
    """[{id, name, is_bot}] for one channel, newest-first-effort and cached.

    Never raises and never blocks a run: a roster that cannot be fetched comes back empty, and an
    empty roster costs the dog the ability to address anyone — not the ability to answer. An app
    provisioned before these scopes existed returns missing_scope here, which is exactly that case.
    """
    now = now or time.time()
    hit = _roster_cache.get(channel)
    if hit and (now - hit[0]) < _ROSTER_TTL:
        return hit[1]
    out = []
    try:
        ids, cursor = [], ""
        for _ in range(10):                       # bounded: a channel is not an unbounded page walk
            r = api_q("conversations.members", token, channel=channel, limit=200, cursor=cursor)
            if not r.get("ok"):
                # Say it ONCE, and say what to do. A dog provisioned before these scopes existed
                # lands here on every ask, and an empty roster is invisible from the channel: it
                # looks like a dog that chose not to answer anyone. Silence is how every other bug
                # in this file stayed alive.
                global _roster_warned
                if r.get("error") == "missing_scope" and not _roster_warned:
                    _roster_warned = True
                    print("[slack] no roster: this app predates `channels:read`/`users:read` — "
                          "reinstall it from its Slack app page to pick them up. It can still "
                          "answer; it cannot address anyone.", file=sys.stderr)
                return []
            ids += r.get("members") or []
            cursor = ((r.get("response_metadata") or {}).get("next_cursor") or "").strip()
            if not cursor:
                break
        for uid in ids:
            u = api_q("users.info", token, user=uid)
            if not u.get("ok"):
                continue
            m = u.get("user") or {}
            p = m.get("profile") or {}
            nm = (p.get("display_name") or p.get("real_name") or m.get("name") or uid).strip()
            out.append({"id": uid, "name": nm, "is_bot": bool(m.get("is_bot"))})
    except Exception:
        return []
    _roster_cache[channel] = (now, out)
    return out


def roster_line(members: list, me: str = "") -> str:
    """The roster as one line for the run's prompt, or "" when there is nobody to name."""
    others = [m for m in members if m["id"] != me]
    if not others:
        return ""
    dogs = ["%s <@%s>" % (m["name"], m["id"]) for m in others if m["is_bot"]]
    folk = ["%s <@%s>" % (m["name"], m["id"]) for m in others if not m["is_bot"]]
    parts = []
    if dogs:
        parts.append("collies: " + ", ".join(dogs))
    if folk:
        parts.append("people: " + ", ".join(folk))
    return ("Also in this channel — %s. To address one, copy its <@…> token into your reply; that "
            "is what reaches them." % "; ".join(parts))


# Matches the plain <@U123> and the older labelled <@U123|name>. Deliberately wider than MENTION_RE:
# that one strips the dog's own mention out of an ask, where missing a form is harmless, while this
# one decides who a reply is allowed to ping, where missing a form is the whole failure.
_MENTION_ANY = re.compile(r"<@([UW][A-Z0-9]+)(\|[^>]*)?>")


def keep_known_mentions(text: str, members: list) -> str:
    """Drop <@…> tokens from an outgoing answer that name nobody in this channel.

    The answer is posted as ordinary text precisely so a mention in it reaches someone — which is
    also why an id the model invented would reach someone. The bound is the ROSTER, not the wording
    of the ask: a person in a channel may @ anyone in it, so a dog may too, and no further. With an
    empty roster nothing is known to be addressable and every token goes, which is the safe way for
    a failed lookup to fail.
    """
    ok = {m["id"] for m in members}
    return _MENTION_ANY.sub(lambda mo: mo.group(0) if mo.group(1) in ok else "", text)


def say(token: str, channel: str, text: str, thread: str = "", tag: str = "",
        broadcast: bool = False) -> str:
    """Reply in the thread the ask arrived in. Returns the message ts, so it can be edited.

    In-thread on purpose: one run's output is long, and a channel that fills with it stops being
    somewhere anyone reads. But a thread is also where an answer goes to be missed — so the one
    message that is actually an ANSWER is sent with `reply_broadcast`, which keeps it a thread reply
    and still surfaces it in the channel. Progress stays quiet; conclusions do not.
    """
    try:
        p = {"channel": channel, "text": (tag + " — " + text) if tag else text}
        if thread:
            p["thread_ts"] = thread
            if broadcast:
                p["reply_broadcast"] = "true"
        r = api("chat.postMessage", token, **p)
        if not r.get("ok"):
            print("[slack] postMessage failed: %s" % r.get("error"), file=sys.stderr)
            return ""
        return r.get("ts", "")
    except Exception as e:
        print("[slack] postMessage error: %s" % e, file=sys.stderr)
        return ""


def edit(token: str, channel: str, ts: str, text: str, tag: str = "") -> bool:
    """Rewrite a message already sent. One ask used to produce `queued #1` and `on it — #1` a second
    apart — two messages for one fact — before the result made a third. A status that CHANGES should
    be one line that changes, not a transcript of its own state machine."""
    if not ts:
        return False
    try:
        r = api("chat.update", token, channel=channel, ts=ts,
                text=(tag + " — " + text) if tag else text)
        if not r.get("ok"):
            print("[slack] update failed: %s" % r.get("error"), file=sys.stderr)
        return bool(r.get("ok"))
    except Exception as e:
        print("[slack] update error: %s" % e, file=sys.stderr)
        return False


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
        # This dog's own Slack user id, so the roster it is handed does not introduce it to itself.
        # auth.test needs no scope, so this works on an app provisioned before the roster existed.
        try:
            self.me = (api("auth.test", bot_token) or {}).get("user_id", "")
        except Exception:
            self.me = ""
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
        ts = item.get("status_ts", "")
        if not edit(self.token, ch, ts, "on it — #%d" % item["id"], self.tag):
            ts = say(self.token, ch, "on it — #%d" % item["id"], th, self.tag)
        # `run` takes the task POSITIONALLY. It was passed as --task, which argparse rejects with
        # exit 2 before a single token is spent — so every ask this bot ever accepted failed, while
        # the thread filled with "on it" and "queued" and looked for all the world like it was
        # working. Same shape as the constructor bug that stopped it starting at all: the failure
        # was downstream of everything anyone watches.
        # --print: the answer, and nothing else on stdout. --mode: the autonomy this dog was
        # ANNOUNCED with, finally bounding what the run may do rather than only what it said.
        # COLLIE_IDENTITY: its name, which until now reached the Slack tag and no further.
        cmd = [sys.executable, "-m", "harness.cli", "run", item["text"], "--print",
               "--mode", AUTONOMY_MODE.get(self.dog.get("autonomy", ""), "project")]
        if self.provider:
            cmd += ["--provider", self.provider]
        # Who else is in this channel, so the dog can answer the one who asked and hand work on to
        # a packmate. Fetched per ask because the channel is per ask; cached, so this is one call
        # in two minutes rather than one per task. An empty roster is survivable — see roster().
        mates = roster(self.token, ch)
        env = dict(os.environ,
                   COLLIE_IDENTITY=identity_text(self.dog, roster_line(mates, self.me)))
        try:
            self.current = subprocess.Popen(
                cmd, cwd=self.cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
            out, err = self.current.communicate()
            rc = self.current.returncode
        except Exception as e:
            out, err, rc = "", str(e), -1
        finally:
            self.current = None

        out, err = (out or "").strip(), (err or "").strip()
        # stderr is diagnostics, not the answer. Merged into stdout (stderr=STDOUT) it went to the
        # channel AS the reply: a huggingface_hub "unauthenticated requests" warning and the run's
        # own stats line sat above the answer inside one code fence, and the warning is what the
        # person then asked about. A failed run is the exception — there, stderr is the only thing
        # that says why, and silence would be worse than noise.
        out = out or ("(no output)" if rc == 0 else "")
        if rc != 0:
            out = (out + "\n" + err).strip() or "(no output)"
        # Slack rejects a message over 40k; keeping the tail keeps the conclusion,
        # which is the part anyone reads.
        if len(out) > 3500:
            out = "…(trimmed)…\n" + out[-3500:]
        head = "#%d done" % item["id"] if rc == 0 else "#%d failed (exit %s)" % (item["id"], rc)
        # The answer is the one message worth surfacing: broadcast so it appears in the channel as
        # well as the thread. The status line above is left showing how it ended, so the thread reads
        # as one request with one outcome rather than a log of a state machine.
        edit(self.token, ch, ts, head, self.tag)
        # Addressed to whoever asked — dog or person, the same way. For a dog it is the difference
        # between an answer and no answer: it reads a channel only through its own mentions, so work
        # it delegated would come back somewhere it cannot see. For a person it is the difference
        # between an answer and one they find later: a run takes minutes, by which time they are in
        # another window, and a thread reply is the notification easiest to miss on a phone. Only
        # the outcome is addressed; "queued" and "on it" stay unmentioned, because being pinged
        # three times for one ask is how a colleague becomes a nuisance.
        back = "<@%s> " % item["user"] if item.get("user") else ""
        # ORDINARY TEXT, not a code fence. Slack does not render a mention inside ```, so an answer
        # posted that way could never reach a packmate however correctly it was addressed — the
        # asker's <@…> worked only because it sits outside the fence. The fence earned its place
        # when the answer was raw CLI output with stats and warnings in it; --print made the answer
        # prose, and prose in a fence loses its wrapping, its emphasis and its links as well.
        # Whatever the model fences itself still renders as code.
        say(self.token, ch, "%s%s\n%s" % (back, head, keep_known_mentions(out, mates) or "(no output)"),
            th, self.tag, broadcast=True)
        self.q.finish(item["id"])


# ---------------------------------------------------------------------------
# Socket Mode
# ---------------------------------------------------------------------------

MENTION_RE = re.compile(r"<@[UW][A-Z0-9]+>")

# ---------------------------------------------------------------------------
# The pack, talking to itself
# ---------------------------------------------------------------------------
#
# Every event carrying a `bot_id` used to be dropped, which made two dogs in one channel deaf to
# each other: BigMac's `@rowan hello` reached rowan's app and went in the bin. The reason was real —
# two bots answering each other's mentions is a loop that spends real money on every lap — but it
# also ruled out the thing a pack is for.
#
# "A reply mentions nobody, so it cannot re-trigger anyone" was true of the code and false of the
# job: being asked to @ another dog is the ordinary way work is handed on, and a dog that reports
# back to the dog that asked has to mention it or the answer goes nowhere. So mentions between dogs
# are the mechanism, and the loop has to be bounded by something that can tell a chain that is
# GETTING SOMEWHERE from a pair of dogs bouncing.
#
# Repetition is what distinguishes them. A delegation walks new ground — rowan asks juno, juno asks
# cap — and every step is an edge nobody has used in this thread. A loop re-walks one edge: rowan,
# juno, rowan, juno. So the rule is per-edge and not per-message: any one dog may reach this dog
# PACK_LAPS times in a thread, which leaves room for "here is the answer" and "thanks, one more
# thing" and stops the third identical lap. PACK_HOPS is the backstop for a long chain that never
# repeats an edge but has clearly stopped being useful.
#
# Both are per-thread, so a new thread starts clean: the bound is on one conversation going round,
# never on a pair of dogs speaking again.
#
# One thing stays forbidden at any depth: answering yourself. That loop needs no second party.
PACK_LAPS = 2                      # times one particular dog may reach this one, per thread
PACK_HOPS = 8                      # dog-to-dog turns in a thread, however many dogs are involved


def pack_gate(state: dict, thread: str, peer: str,
              laps: int = PACK_LAPS, hops: int = PACK_HOPS) -> str:
    """Record a dog-to-dog turn. "" to go ahead, else the reason to stop, in words."""
    t = state.setdefault(thread, {"n": 0, "edges": {}})
    if len(state) > 500:                       # a process that runs for weeks cannot grow forever
        state.pop(next(iter(state)), None)
    t["n"] += 1
    t["edges"][peer] = t["edges"].get(peer, 0) + 1
    if t["edges"][peer] > laps:
        return ("we have been round this %d times in this thread — stopping before it becomes a "
                "loop. A person, or a new thread, starts it again." % laps)
    if t["n"] > hops:
        return ("that is %d hands-off in one thread — stopping here; whatever this was meant to "
                "reach, it is not reaching it." % hops)
    return ""


def provider_hint() -> str:
    """Name a provider that has a credential on THIS machine, or "" if none does.

    "Pick one in the Settings panel" is advice a person can follow and still land on a provider
    that cannot start: `anthropic` wants ANTHROPIC_API_KEY, and the machine that has a Claude
    subscription instead has a token Claude Code minted, under a different provider name. The gap
    between those two names is invisible until a dog refuses to run, so the refusal names the one
    that would have worked.
    """
    try:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "\n  This machine has ANTHROPIC_API_KEY — try: --provider anthropic"
        from . import providers
        if providers._read_oauth_token():
            return ("\n  This machine has a Claude Code token, which is a different provider name"
                    " than\n  `anthropic`: --provider anthropic-oauth")
    except Exception:
        pass
    return ""


def _open_socket_url(app_token: str) -> str:
    r = api("apps.connections.open", app_token)
    if not r.get("ok"):
        raise RuntimeError(
            "apps.connections.open failed: %s — is this an app-level token (xapp-…) "
            "with connections:write, and is Socket Mode enabled on the app?" % r.get("error"))
    return r["url"]


def _refresh(name: str, entry: dict, config_token: str) -> int:
    """Bring one already-provisioned dog up to the manifest setup would build TODAY.

    Every member of a pack was created at some point in this file's history and keeps the manifest
    of that day — scopes, display name and face are all fixed at creation and none of them follow
    a later change. The first dog to need the roster scopes was brought up to date by hand over the
    API, once, which is exactly the per-dog handwork this command exists to remove; and there is
    never only one such dog. `--config-token` on a finished dog means this, for any of them.
    """
    app_id = entry.get("app_id", "")
    print("bringing %s up to the current manifest (app %s)…" % (name, app_id))
    try:
        res = update_app(config_token, app_id, app_manifest(name))
    except Exception as e:
        print("  %s" % e, file=sys.stderr)
        return 1
    print("  manifest updated")

    face = ""
    try:
        from . import avatar
        face = avatar.write(name)
    except Exception as e:
        print("  (could not draw an avatar: %s)" % e)
    icon_err = set_icon(config_token, app_id, face)
    print("  face on" if not icon_err else "  (icon unchanged — %s)" % icon_err)

    if res.get("permissions_updated"):
        print("\n  the SCOPES changed, so one reinstall is needed to grant them — Slack exposes no\n"
              "  API for that, it is the install itself that authorizes:\n"
              "    https://api.slack.com/apps/%s/install-on-team\n"
              "  then hand the new bot token back:\n"
              "    collie slack setup --name %s --bot-token xoxb-…" % (app_id, name))
    else:
        print("  no scope changed — nothing to reinstall, it is already live")
    return 0


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
    # Papers means BOTH tokens. Checking only the bot token turned the half-provisioned case into a
    # dead end: a dog whose xoxb- was saved and whose xapp- came later — which is the order the two
    # pages hand them over in — was told it "already has papers" and refused, while `--list` said in
    # the same breath that it needs its tokens. The one command that could finish it was the one
    # command that would not run.
    have = dogs.get(name) or {}
    if have.get("bot_token") and have.get("app_token"):
        # A finished dog is not a dead end: it is the one that goes STALE. Scopes, the display name
        # and the face are all fixed at creation, so every dog provisioned before a change to the
        # manifest keeps the old one forever — and the only fix was to reach for the API by hand,
        # once per dog, which is exactly the per-dog handwork this command exists to remove. Given
        # the credential that can, bring it up to today's manifest instead of refusing.
        if a.config_token and have.get("app_id"):
            return _refresh(name, have, a.config_token)
        print("%s already has papers (app %s). Pick another name, run `collie slack --name %s`, "
              "or pass --config-token to bring its app up to the current manifest (scopes, name "
              "and face)." % (name, have.get("app_id", "?"), name))
        return 1

    entry = dict(dogs.get(name) or {})

    # THIS dog's face, drawn BEFORE anything can fail, so there is something of its own to upload
    # and something on disk if the rest of setup stops early. Derived from the name, so it is the
    # same face on every machine and after any reinstall.
    face = ""
    try:
        from . import avatar
        face = avatar.write(name)
        t = avatar.traits(name)
        print("  %s: %s coat on a %s plate — %s" % (name, t["coat"], t["plate"], face))
    except Exception as e:                               # never let a picture stop a setup
        print("  (could not draw an avatar: %s)" % e)

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
        # Now, while the config token is still in hand and before anyone has seen the app: an
        # icon set later is a second visit to a settings page, which is the cost this command
        # exists to remove. It uploads THIS dog's face rather than one picture shared by the
        # pack — the whole point of the name is that the members are told apart.
        icon_err = set_icon(a.config_token, entry["app_id"], face)
        print("  face on" if not icon_err else "  (default icon — %s)" % icon_err)

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
    if face:
        # It IS uploaded above, by `apps.icon.set`. That method is in no published list — the
        # manifest has no icon field, which is why this was written off as un-automatable — but
        # Slack's own CLI uses it on deploy, and it works. Undocumented means it can go away, so
        # the path is still printed: if the upload ever fails the fallback is a drag, not a hunt.
        print("  its face: %s\n    (already uploaded; to redo it by hand: "
              "https://api.slack.com/apps/%s/general → Display Information)" % (face, app_id))
    return 0


def _autostart_paths(name: str):
    from . import wallpaper as wp
    slug = re.sub(r"[^A-Za-z0-9_-]+", "", name.lower()) or "collie"
    boot = os.path.join(os.path.expanduser("~"), ".collie", "slack-%s.pyw" % slug)
    vbs = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                       "Microsoft", "Windows", "Start Menu", "Programs", "Startup",
                       "collie-slack-%s.vbs" % slug)
    return wp, boot, vbs


def install_autostart(name: str, cwd: str, channels: str = "", provider: str = "") -> int:
    """Bring this dog back after a restart.

    A dog started from a terminal dies with the terminal, which is how one sat silent through a
    day's worth of @-mentions with nothing anywhere saying it had gone. Same mechanism the
    wallpaper already uses — a generated .pyw plus a hidden .vbs in the Startup folder — rather than
    a second invention: no hardcoded interpreter or repo path, and removable by deleting two files.

    Per DOG, not per machine: the pack is keyed by name, and two dogs on one laptop want two
    entries.
    """
    from . import plat
    if not plat.is_windows():
        print("collie slack --install-autostart is Windows-only for now "
              "(macOS wants a LaunchAgent; not written yet).", file=sys.stderr)
        return 2
    wp, boot, vbs = _autostart_paths(name)
    log = os.path.join(os.path.expanduser("~"), ".collie", "slack.log")
    argv = ["slack", "--name", name, "--cwd", cwd]
    if channels:
        argv += ["--channels", channels]
    if provider:
        argv += ["--provider", provider]
    with open(boot, "w", encoding="utf-8") as f:
        # repr() every path: a username with an apostrophe closes a raw string early and the
        # generated launcher dies with a SyntaxError, silently, at logon.
        f.write("# auto-generated by `collie slack --install-autostart`.\n"
                "import sys, os\n"
                "sys.path.insert(0, %s)\n"
                "sys.stdin = open(os.devnull, 'r')\n"
                "f = open(%s, 'a', encoding='utf-8', buffering=1)\n"
                "sys.stdout = sys.stderr = f\n"
                "from harness.cli import main\n"
                "sys.argv = ['collie'] + %r\n"
                "sys.exit(main())\n" % (repr(wp._pkg_parent()), repr(log), argv))
    os.makedirs(os.path.dirname(vbs), exist_ok=True)
    with open(vbs, "w", encoding="utf-8") as f:
        f.write("' collie slack (%s) - hidden logon autostart (auto-generated).\n"
                "q = Chr(34)\n"
                'CreateObject("WScript.Shell").Run q & "%s" & q & " " & q & "%s" & q, 0, False\n'
                % (name, wp.pythonw(), boot))
    print("%s will come back after a restart.\n  launcher: %s\n  startup : %s\n"
          "  remove  : collie slack --uninstall-autostart --name %s" % (name, boot, vbs, name))
    return 0


def uninstall_autostart(name: str) -> int:
    _, boot, vbs = _autostart_paths(name)
    gone = []
    for p in (vbs, boot):
        try:
            if os.path.exists(p):
                os.remove(p)
                gone.append(p)
        except OSError as e:
            print("could not remove %s: %s" % (p, e), file=sys.stderr)
    print("removed %d file(s); %s will not start itself again." % (len(gone), name))
    return 0


def main(argv=None) -> int:
    import argparse
    if argv and argv[0] == "setup":
        return setup(argv[1:])
    # Before the parser is built, because argparse evaluates defaults at construction: this is what
    # lands a Provider chosen in the Settings panel into the environment. Without it `--provider`
    # defaulted to "" for ever, nothing was passed to the child, and `collie run` fell to its own
    # `or "mock"` — so a dog answered every ask from canned fixtures. webapp._provider() already
    # refuses to do that and says why; this path never got the same treatment.
    try:
        from . import settings as _settings
        _settings.apply()
    except Exception:
        pass

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
    ap.add_argument("--install-autostart", action="store_true",
                    help="bring this dog back after a restart (opt-in; writes two files)")
    ap.add_argument("--uninstall-autostart", action="store_true",
                    help="stop it coming back")
    args = ap.parse_args(argv)

    if args.uninstall_autostart:
        return uninstall_autostart(args.name or "collie")
    if args.install_autostart:
        return install_autostart(args.name or "collie", args.cwd, args.channels, args.provider)

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

    # A dog with no provider is worse than no dog. `collie run` defaults to "mock", which answers
    # from canned fixtures — and a fixture is indistinguishable from a model that has gone wrong, so
    # the dog reports "#1 done" and hands over confident nonsense. It did exactly that, in a real
    # channel, for every ask. mock stays reachable, but only by NAME.
    if not args.provider:
        print("collie slack: no provider.\n"
              "  Looked at: --provider, then COLLIE_PROVIDER (which the Settings panel injects).\n"
              "  A value in `collie config` is not necessarily a saved one — that listing falls\n"
              "  back to defaults, so it can name a provider nothing has actually configured.%s\n"
              "  Refusing rather than falling back to `mock`: mock answers from fixtures, and a\n"
              "  fixture in a channel reads exactly like a model that has gone wrong.\n"
              "  To do that on purpose: --provider mock"
              % provider_hint(), file=sys.stderr)
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

    # Who this dog is to Slack. Needed only since the pack can talk: "is this me" cannot be answered
    # from the name, and answering your own mention is the one loop with no second party to tire of
    # it. Failing to find out is not fatal — it costs the self-check, not the dog.
    my_user = my_bot = ""
    try:
        me = api("auth.test", bot_token)
        my_user, my_bot = me.get("user_id", ""), me.get("bot_id", "")
    except Exception as e:
        print("[slack] auth.test failed (%s) — self-mentions will not be filtered" % e, file=sys.stderr)

    pack: dict = {}                 # thread -> dog-to-dog turns taken in it
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
                if event.get("type") != "app_mention":
                    continue

                # Strip only THIS dog's own mention. Everyone else's is the ask's ADDRESSING
                # information, and removing it removed the only thing that can reach them:
                # "@cornetto go ask @rowan about the branch" arrived as "go ask about the branch".
                # So the remedy the dog itself proposes when it cannot find its packmates — tell me
                # what they are called — did not work either, because the telling was deleted too.
                # No my_user (auth.test failed) falls back to the old strip-everything: without an
                # id of its own a dog cannot tell its mention from anyone else's.
                _raw = event.get("text", "")
                text = (re.sub(r"<@%s(\|[^>]*)?>" % re.escape(my_user), "", _raw) if my_user
                        else MENTION_RE.sub("", _raw)).strip()
                ch = event.get("channel", "")
                th = event.get("thread_ts") or event.get("ts") or ""
                user = event.get("user", "")
                low = text.lower()

                # Another dog may ask; this dog may not ask itself. Self-mention is the one loop
                # with no bound — it re-triggers on its own reply and never needs a second party.
                peer = event.get("bot_id", "")
                if (peer and peer == my_bot) or (user and user == my_user):
                    continue
                if peer:
                    stop = pack_gate(pack, th, peer)
                    if stop:
                        # Said once, to the dog that asked, and then not again: silence is how a
                        # bounded exchange looks identical to a broken one.
                        if not pack[th].get("said"):
                            pack[th]["said"] = True
                            say(bot_token, ch, stop, th, tag)
                        continue

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
                    item = q.add(text, ch, th, user, from_dog=bool(peer))
                    ahead = q.waiting() - 1
                    # The ts is kept ON the item so the worker can edit this same line rather than
                    # post another one under it.
                    item["status_ts"] = say(
                        bot_token, ch,
                        "queued #%d%s" % (item["id"], "" if ahead <= 0 else " — %d ahead of it" % ahead),
                        th, tag)
                    worker.nudge()          # after the ts is stored, or the worker can beat it there
        except Exception as e:
            print("[slack] connection lost (%s) — reconnecting" % e, file=sys.stderr)
        finally:
            try:
                ws.close()
            except Exception:
                pass
        time.sleep(2)
