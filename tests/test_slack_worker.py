"""What `collie slack` actually does with an ask.

The bot passed the task as `--task`, which `collie run` takes positionally. argparse rejected it
with exit 2 before a single token was spent, so every ask ever accepted failed — while the thread
filled with "queued" and "on it" and looked exactly like work. Nothing downstream of the spawn was
ever checked against the parser that has to accept it, so this checks precisely that.

    python3 tests/test_slack_worker.py
"""
import os
import re
import subprocess
import sys

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
    check("status_ts" in src,
          "the status message's ts is carried so the worker can edit the queuer's line")

    # --- and the ordering that makes that possible ----------------------------------------------
    i_ts = src.find('item["status_ts"] = say(')
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

    print("\n  " + ("%d FAILED" % len(fails) if fails else "slack worker: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
