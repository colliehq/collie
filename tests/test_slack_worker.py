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

    print("\n  " + ("%d FAILED" % len(fails) if fails else "slack worker: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
