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

No open handoffs.
