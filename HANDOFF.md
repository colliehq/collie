# Handoff

Two machines work on this repo — a Mac and a Windows box — and neither can see
the other's screen, shell, or session. This file is the channel between them.
It travels with the code, needs no account, and its history is the git log.

**How to use it**

- Newest entry at the top. Sign it with the machine and the date.
- Say what you *did*, what you *verified*, and what you could **not** verify.
  The last one matters most: the other machine cannot tell a checked claim from
  an assumed one, and will build on both equally.
- Ask for what you need by name. "The Windows runner is missing" is actionable;
  "CI is broken" is not.
- Delete an entry once its ask is done. This is a message queue, not a log —
  the log is `git log`.

---

## 2026-07-30 · Mac · release is unblocked, but needs a Windows runner

**The thing to know:** 0.20.18, 0.20.19 and 0.20.20 each got a version bump, a
release commit and a tag, and **none of them published anything**. That was not
a tagging mistake. Every job in `release.yml` ran on a GitHub-hosted runner,
this account's hosted minutes are gone, and a job that cannot get a runner fails
in four seconds with **zero steps and no log** — `check-version` died that way
and the other four skipped behind it. If you ever see a job fail in a few
seconds with an empty log, that is the signature; it is not your code.

**Done from this side**

- All five jobs moved off hosted runners. Four now run on this Mac, registered
  as `collie-mac` (online).
- The macOS signing decision is now based on what the machine can *do*, not on
  whether a secret is set. This repo has **no secrets at all** (`total_count: 0`),
  so the old `HAVE_CERT` test would have produced a silently unsigned dmg — from
  a machine that has the Developer ID sitting in its login keychain and a stored
  `collie` notary profile. Both confirmed working here.
- A **tag** now refuses to publish a dmg that is unsigned or un-notarised.
  Gatekeeper stops an unsigned dmg with "cannot be opened", so shipping one is
  worse than shipping nothing — it reads as the app being broken. Manual
  `workflow_dispatch` runs may still go unsigned; that is what they are for.
- `actions/setup-python` failed on the self-hosted Mac with
  `mkdir: /Users/runner: Permission denied` — it assumes the hosted image's
  tool-cache layout. Fixed at the **runner** level (`AGENT_TOOLSDIRECTORY` in the
  runner's `.env`), not in the workflow, so the workflow stays correct for hosted
  runners too.

**Verified:** `check-version` now succeeds — the billing wall is genuinely past.
The Developer ID and the `collie` notary profile both respond on this machine.

**NOT verified:** the `setup-python` fix. `wheel` and `dmg` failed on it before
the fix, and I could not re-run them, because `installer` sits queued waiting for
a Windows runner — the run never completes, and GitHub will not re-run a run that
is still going. So the fix is right in principle and **has not been proven**.

**What I need from the Windows box**

Register a runner on it for *this* repo, labelled `collie-win`:

    https://github.com/wudaming00/collie/settings/actions/runners/new   → Windows / x64
    ./config.cmd --url https://github.com/wudaming00/collie --token <from that page> --labels collie-win

Same procedure as the vocalcode runner you already set up — different repo, and
the label must be `collie-win` or the job queues forever against a runner that
never matches. Registration tokens expire in about an hour, so take a fresh one
from that page rather than reusing an old one.

Once it is up, say so here and I will re-push `v0.20.20`. The tag currently
points at the commit with the runner fix; it has been force-moved once already,
which was safe only because it had never produced a release.

**Also:** you mentioned leaving a handoff file for me about macOS-specific paths.
I could not find it — not in this repo (including untracked files), not in
`Collie-macos`, not in `vocalcode`, and nothing named `*handoff*` anywhere under
`~/projects`, `~/Desktop` or `~/Downloads` from the last 12 hours, and nothing
new on any remote branch. It was probably never committed. Put it here.
