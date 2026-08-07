"""What `collie slack` actually does with an ask.

The bot passed the task as `--task`, which `collie run` takes positionally. argparse rejected it
with exit 2 before a single token was spent, so every ask ever accepted failed — while the thread
filled with "queued" and "on it" and looked exactly like work. Nothing downstream of the spawn was
ever checked against the parser that has to accept it, so this checks precisely that.

    python3 tests/test_slack_worker.py
"""
import json
import os
import re
import subprocess
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
    src = open(os.path.join(ROOT, "harness", "slackbot.py"), encoding="utf-8").read()

    # --- the command it spawns must be one the CLI accepts ------------------------------------
    m = re.search(r'cmd = \[sys\.executable, "-m", "harness\.cli", (.+?)\]', src)
    check(bool(m), "the worker builds a `harness.cli` command")
    if m:
        parts = [p.strip() for p in m.group(1).split(",")]
        flags = [p.strip('"') for p in parts if p.startswith('"')]
        check("--task" not in flags,
              "the task is NOT passed as --task — `collie run` takes it positionally (%s)" % flags)

        # Not a string check: hand the real parser the real shape and see it survive.
        from harness import cli
        ap = None
        try:
            ap = cli.build_parser() if hasattr(cli, "build_parser") else None
        except Exception:
            ap = None
        if ap is None:
            # No exported parser — ask the CLI itself, which is the same guarantee for more cost.
            r = subprocess.run([sys.executable, "-m", "harness.cli", "run", "--provider", "mock",
                                "--help"], cwd=ROOT, capture_output=True, text=True, timeout=120)
            usage = (r.stdout or "") + (r.stderr or "")
            check("--task" not in usage,
                  "`collie run --help` does not offer --task, so nothing may send it")
            check(re.search(r"\n\s*task\b", usage) is not None or "task\n" in usage,
                  "`collie run` documents a positional task")

    # --- one ask should not produce three messages ----------------------------------------------
    check("def edit(" in src,
          "there is a way to rewrite a status line instead of posting another one")
    check(src.count("say(self.token, ch,") <= 2,
          "the worker posts at most two messages per ask (status + answer)")
    check("broadcast=True" in src,
          "the ANSWER is broadcast, so a thread is not where it goes to be missed")
    check('p["reply_broadcast"]' in src,
          "...via reply_broadcast, which keeps it a thread reply as well")
    # Status is a REACTION on the ask now, not a message about it. `queued #N` and `on it — #N`
    # were two messages narrating one fact in a channel people are trying to read — and the task
    # number in them indexes one dog's LOCAL queue, which a peer took for a shared reference and
    # went hunting through its own repository for.
    check("ask_ts" in src and "reactions.add" in src,
          "the ask's own ts is carried, and the state is put on that message")
    check('"queued #' not in src and '"on it — #' not in src,
          "...instead of a line saying so, and a second line correcting the first")

    # --- and the ordering that makes that possible ----------------------------------------------
    i_ts = src.find('item["ask_ts"] = ')
    i_nudge = src.find("worker.nudge()", i_ts if i_ts > 0 else 0)
    check(i_ts > 0 and i_nudge > i_ts,
          "the worker is nudged AFTER the ts is stored — otherwise it can start before it exists")

    # --- every flag the logon launcher writes must survive `collie` -----------------------------
    # The launcher goes through harness.cli, whose slack subparser was NARROWER than slackbot's own
    # parser: --channels died with "invalid choice: C0BM…". At logon that is invisible — no window,
    # and a bot that simply is not there looks exactly like a bot with nothing to say.
    cli_src = open(os.path.join(ROOT, "harness", "cli.py"), encoding="utf-8").read()
    i = cli_src.find('sub.add_parser("slack"')
    sub = cli_src[i:cli_src.find("set_defaults(fn=cmd_slack)", i)]
    for flag in ("--name", "--cwd", "--provider", "--announce", "--channels", "--allow",
                 "--install-autostart", "--uninstall-autostart"):
        check(flag in sub, "`collie slack` accepts %s" % flag)
    j = cli_src.find("def cmd_slack")
    fwd = cli_src[j:cli_src.find("\ndef ", j + 5)]
    for name in ("channels", "allow", "install_autostart", "uninstall_autostart"):
        check(name in fwd, "...and cmd_slack forwards %s through" % name)

    # Refusing without a provider is right; leaving the person to guess which one is not. On a
    # machine with a Claude subscription the credential is a token Claude Code minted, and it lives
    # under a DIFFERENT provider name than the `anthropic` that `collie config` displays — so
    # "pick one in the Settings panel" can be followed exactly and still not start.
    from harness import slackbot as sb
    real_env = dict(os.environ)
    try:
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-whatever"
        check("--provider anthropic" in sb.provider_hint(),
              "an API key on this machine is named as the provider to pass")
        os.environ.pop("ANTHROPIC_API_KEY")
        import harness.providers as pv
        keep = pv._read_oauth_token
        pv._read_oauth_token = lambda: "sk-ant-oat01-…"
        check("anthropic-oauth" in sb.provider_hint(),
              "a Claude Code token points at anthropic-oauth, not at anthropic")
        pv._read_oauth_token = lambda: ""
        check(sb.provider_hint() == "",
              "and a machine with neither says nothing rather than inventing a suggestion")
        pv._read_oauth_token = keep
    finally:
        os.environ.clear()
        os.environ.update(real_env)

    # ---- the pack can talk to itself ---------------------------------------------------------
    # Dropping every event with a bot_id made two dogs in one channel deaf to each other, which is
    # the opposite of what a pack is. What keeps that from looping is that a dog's reply mentions
    # nobody — so no app_mention fires back — plus a bound on the case where one is asked to.
    src = open(os.path.join(ROOT, "harness", "slackbot.py"), encoding="utf-8").read()
    check('!= "app_mention" or event.get("bot_id")' not in src,
          "a mention from another dog is no longer thrown away unread")
    check('peer == my_bot' in src and 'user == my_user' in src,
          "but a dog still never answers itself — the one loop needing no second party")
    check('"<@%s> " % item["user"]' in src and "queued #" in src,
          "the outcome is addressed to whoever asked; `queued` and `on it` are not, so one ask is "
          "one ping and not three")

    # Being asked to @ another dog is the ordinary way work is handed on, so the bound cannot be
    # "dogs may not mention dogs". It has to tell a chain that is getting somewhere from a pair
    # bouncing — and what separates those is REPETITION, not volume.
    st = {}
    chain = [sb.pack_gate(st, "T1", p) for p in ("B1", "B2", "B3", "B4")]
    check(chain == ["", "", "", ""],
          "a delegation down four different dogs passes — every hop is new ground")

    st = {}
    laps = [sb.pack_gate(st, "T2", "B1") for _ in range(sb.PACK_LAPS + 1)]
    check(laps[:sb.PACK_LAPS] == [""] * sb.PACK_LAPS,
          "one dog may come back %d times — enough for an answer and a follow-up" % sb.PACK_LAPS)
    check("loop" in laps[-1], "and the lap after that is refused, in words")
    check(sb.pack_gate({}, "T3", "B1") == "",
          "while a fresh thread starts clean — the bound is on one conversation, not on a pair of "
          "dogs ever speaking again")

    st = {}
    hops = [sb.pack_gate(st, "T4", "B%d" % i) for i in range(sb.PACK_HOPS + 1)]
    check(hops[-1] and "reaching" in hops[-1],
          "a chain that never repeats an edge still stops at %d hops" % sb.PACK_HOPS)

    big = {}
    for i in range(600):
        sb.pack_gate(big, "T%d" % i, "B1")
    check(len(big) <= 500, "and the tally cannot grow forever in a process that runs for weeks")

    # The other half of delegating: an answer a dog cannot see is not an answer. A dog reads a
    # channel only through its own mentions, so work handed back has to be addressed.
    check('"<@%s> " % item["user"]' in src,
          "an answer is addressed to the asker — for a dog that is the difference between an "
          "answer and no answer, since it sees a channel only through its own mentions")

    # --- the autonomy it ANNOUNCES has to be the autonomy it RUNS UNDER --------------------------
    # It was announced and nothing more: ident["autonomy"] reached the greeting string and the
    # spawn carried no --mode at all, so it took the gate's default. A dog introduced to a channel
    # as "propose — writes nothing" could write anything, and the greeting's one load-bearing
    # promise was the one thing nothing kept.
    check(sb.AUTONOMY_MODE["propose"] == "plan",
          "propose maps to the one gate mode that is actually read-only")
    check(set(sb.AUTONOMY) == set(sb.AUTONOMY_MODE),
          "every autonomy the greeting can name has a mode to run under")
    check('"--mode"' in src and "AUTONOMY_MODE" in src,
          "...and the spawn passes it, so the setting bounds the run and not just the hello")

    # --- a dog that knows its own name -----------------------------------------------------------
    who = sb.identity_text({"name": "Cornetto", "autonomy": "propose",
                            "machine": "box", "os": "Windows"})
    check("Cornetto" in who, "the identity carries the name the channel @-s")
    check("propose" in who and "writes nothing" in who,
          "...and what it may do, in the same words the channel was given")
    check("COLLIE_IDENTITY" in src, "...and the spawn hands that to the run")
    check("Do not push to main" in sb.identity_text({"name": "x", "autonomy": "branch"}),
          "branch states the half no gate mode can hold — a destination, not a permission")

    # --- the answer is the answer ----------------------------------------------------------------
    check("stderr=subprocess.STDOUT" not in src,
          "stderr is not merged into the reply — a huggingface warning is not an answer")
    check('"--json"' in src,
          "...and the answer arrives as a FIELD, not as whatever happened to land on stdout")

    # --- a thread is a conversation ---------------------------------------------------------------
    # Every ask started a run that remembered nothing, so a follow-up in the same thread met a dog
    # with no idea what had just been said — and a peer asked about "#9" went hunting through its
    # own repository for a number that only ever existed in someone else's queue.
    check('"--resume"' in src, "a thread that has a session continues it instead of starting over")
    tmp_threads = os.path.join(tempfile.mkdtemp(prefix="collie_threads_"), "threads.json")
    real_threads, sb.THREADS = sb.THREADS, tmp_threads
    try:
        check(sb.thread_session("C1", "t1") == "", "an unknown thread continues nothing")
        sb.thread_session("C1", "t1", "20260806-1")
        check(sb.thread_session("C1", "t1") == "20260806-1", "a remembered one comes back")
        check(sb.thread_session("C1", "t2") == "",
              "...and belongs to THAT thread, not to the channel")
        for i in range(sb._THREAD_CAP + 20):
            sb.thread_session("C1", "bulk%d" % i, "s%d" % i)
        kept = json.load(open(tmp_threads, encoding="utf-8"))
        check(len(kept) <= sb._THREAD_CAP,
              "and a dog that runs for weeks does not carry every thread it has ever seen (%d)"
              % len(kept))
    finally:
        sb.THREADS = real_threads

    # --- a pack that can be ADDRESSED, not only heard ---------------------------------------------
    # Hearing was half of it. Asked to greet the two other dogs in its channel, a dog answered that
    # it could find no trace of them and asked what they were called — and each of three separate
    # things would have been enough on its own to keep it from ever reaching them.
    mates = [{"id": "U1", "name": "Rowan", "is_bot": True},
             {"id": "U2", "name": "Daming", "is_bot": False},
             {"id": "UME", "name": "Cornetto", "is_bot": True}]

    line = sb.roster_line(mates, me="UME")
    check("Rowan <@U1>" in line and "Daming <@U2>" in line,
          "the roster names the collies and the people, each with the token that reaches them")
    check("Cornetto" not in line, "...and does not introduce the dog to itself")
    check(sb.roster_line([{"id": "UME", "name": "C", "is_bot": True}], me="UME") == "",
          "a dog alone in a channel hears about nobody, rather than about an empty list")

    check(sb.keep_known_mentions("hi <@U1> and <@U9>", mates) == "hi <@U1> and ",
          "an answer may ping whoever is in the channel, and nobody else")
    check(sb.keep_known_mentions("hi <@U1|rowan>", mates) == "hi <@U1|rowan>",
          "...including the labelled form, which the ask-side regex does not even match")
    check(sb.keep_known_mentions("hi <@U1>", []) == "hi ",
          "...and an empty roster addresses no one: a lookup that failed fails safe")

    # Scoped to the ANSWER path on purpose: `queue` still fences its listing, and should — that one
    # is a lined-up table nobody needs to @ anybody from.
    # _run_one is the LAST method on Worker, so splitting on the next "    def " runs off the end of
    # the class and swallows the rest of the module — `self.q.finish` is where it really stops.
    answer_path = src.split("def _run_one")[1].split("self.q.finish(")[0]
    check('```\\n' not in answer_path,
          "the answer goes out as ordinary text — Slack renders NO mention inside a code fence, so "
          "a fenced answer could never reach a packmate however correctly it was addressed")
    check("re.escape(my_user)" in src,
          "only the dog's OWN mention is stripped from an ask; everyone else's is the addressing "
          "information, and deleting it deleted the only thing that can reach them")

    # --- and the lookups have to be FORM-encoded --------------------------------------------------
    # api() posts JSON, which Slack's WRITE methods accept and its LOOKUP methods quietly ignore.
    # conversations.members answered `invalid_arguments — missing required field: channel` for a
    # call that carried channel, so the roster came back empty with every scope granted and the
    # channel right there — a shape that reads as "the permission did not work".
    sent = {}

    class _Resp:
        def __init__(self, body):
            self.body = body

        def read(self):
            return self.body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_open(req, timeout=None):
        sent["ctype"] = req.headers.get("Content-type") or req.get_header("Content-type")
        sent.setdefault("bodies", []).append(req.data)
        if req.full_url.endswith("conversations.members"):
            return _Resp(b'{"ok":true,"members":["U1"]}')
        return _Resp(b'{"ok":true,"user":{"is_bot":true,"profile":{"display_name":"rowan"}}}')

    real_open = sb.urllib.request.urlopen
    sb.urllib.request.urlopen = fake_open
    sb._roster_cache.clear()
    try:
        got = sb.roster("xoxb-1", "C1", now=1.0)
    finally:
        sb.urllib.request.urlopen = real_open
        sb._roster_cache.clear()

    check(got == [{"id": "U1", "name": "rowan", "is_bot": True}],
          "the roster comes back carrying the member Slack named")
    check("x-www-form-urlencoded" in (sent.get("ctype") or ""),
          "...because a lookup goes out form-encoded, which is the only encoding Slack reads it in")
    check(any(b"channel=C1" in (b or b"") for b in sent.get("bodies", [])),
          "...with the parameter in the body, where `missing required field` said it was not")

    # --- the launcher has to carry the autonomy too ----------------------------------------------
    # Every other flag the person typed was written into the generated .pyw; this one was not, so a
    # dog set to `main` came back from a reboot on whatever identity.json happened to hold — and on
    # the DEFAULT if that file were ever lost. The one setting whose whole point is that nobody
    # discovers it by watching it get crossed should not be the one that goes unwritten.
    from harness import plat as _plat

    tmpd = tempfile.mkdtemp(prefix="collie_autostart_")
    boot = os.path.join(tmpd, "slack-x.pyw")
    vbs = os.path.join(tmpd, "collie-slack-x.vbs")

    class _WP:
        def _pkg_parent(self):
            return r"/site-packages"

        def pythonw(self):
            return r"/pythonw.exe"

    real_paths, real_win = sb._autostart_paths, _plat.is_windows
    sb._autostart_paths = lambda name: (_WP(), boot, vbs)
    _plat.is_windows = lambda: True          # so this runs on the Mac half of the pack too
    try:
        rc = sb.install_autostart("Cornetto", r"/repo", provider="anthropic-oauth",
                                  autonomy="main")
        written = open(boot, encoding="utf-8").read()
    finally:
        sb._autostart_paths, _plat.is_windows = real_paths, real_win

    check(rc == 0 and "'--autonomy', 'main'" in written,
          "the launcher it generates states the autonomy, rather than leaving a reboot to re-decide")
    check("'--provider', 'anthropic-oauth'" in written and "'--name', 'Cornetto'" in written,
          "...alongside the flags it already carried")
    check("--autonomy" not in open(vbs, encoding="utf-8").read(),
          "...in the .pyw, not smuggled into the .vbs, which only launches it")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "slack worker: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
