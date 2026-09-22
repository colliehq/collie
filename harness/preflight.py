"""Host-owned verification STATE carried into the next model request.

The problem this exists for is a sequencing one, not a policy one.  In recorded runs the
model composed a long final answer FIRST, and only then did the host's finish gate append a
``verification_reminder`` — so the run paid for a full answer, a response-contract repair,
the check, and then a SECOND full answer.  Everything the gate decided was correct; it just
decided it after the expensive part had already been streamed.

So the host says what it knows EARLY: on the request that follows a landed edit, while fresh
verification is still missing or failed, it attaches a few lines of current state plus the
order of operations it wants (finish editing → run the check → only then answer).  It rides a
request the loop was making anyway and adds no model call of its own; what is established is
that sequencing, not a latency or token improvement, which has not been measured live.

What this is NOT, deliberately:

  * not evidence.  Nothing here is consulted by ``Harness._repro_verified``, the verify gate,
    or the host check receipt.  A hint that is ignored changes no verdict: the existing
    reminder still fires and a required gate still refuses the finish.
  * not durable history.  The caller attaches the returned message to the outgoing request
    only (the same request-scoped channel ``format_repair`` already uses), so it is never
    appended to ``session["messages"]``, never checkpointed, never compacted and never
    re-billed as a cached prefix.  It is recomputed from live state each turn, which is also
    why a check that genuinely passes on the latest edit makes it disappear on its own.
  * not a user request.  The text says so, because the model's own delivery contract keys off
    the user's original wording and a stray instruction-shaped message is exactly what makes
    an answer drift into a status report about the harness.

``state_line`` never upgrades an outcome.  It is told what the loop already decided from the
strict accounting in ``loop._host_observed_check`` / ``loop._repro_failed`` — a failed,
exit-masked, zero-test or otherwise uncertain run reaches here as "not verified", and the
worst honest reading is the one that gets printed.
"""

# Keep the whole attachment near a couple of hundred tokens: it rides a request that was
# happening regardless, and its value is arriving early, not saying more than the reminder.
_MAX_NAMED_FILES = 3
_MAX_COMMAND_CHARS = 200

HINT_KIND = "verification_state"

_HEADER = ("[host verification state — guidance for this turn from the harness, "
           "not a new request from the user]")

_ORDER = ("Finish any remaining edits first, then %s. Only after that, answer the user's "
          "ORIGINAL request, in its original language, format and length. Do not answer this "
          "note, do not restate it to the user, and do not treat it as permission to skip a "
          "check or as a check that already ran.")

# What to do when the host can name a command versus when it cannot.  The no-command branch
# stays task-neutral on purpose: the workspace may hold a deliverable that no runner covers,
# and naming a runner there is what makes a model install a toolchain it was never asked for.
_RUN_NAMED = ("run this project's check with the bash tool: `%s`%s, and fix what it reports")
_RUN_UNNAMED = ("run this project's applicable check, or — if it genuinely has none — validate "
                "the artifact you actually produced, without installing a toolchain or running "
                "a runner that would collect nothing")
_RUN_REQUIRED = ("satisfy the verification this task requires (see the harness's own "
                 "instruction for it) and fix what it reports")


def _files_line(edited_paths) -> str:
    names = sorted({str(p).strip() for p in (edited_paths or []) if str(p).strip()})
    if not names:
        return "edits: files changed this run"
    shown = ", ".join("`%s`" % n for n in names[:_MAX_NAMED_FILES])
    more = (" (+%d more)" % (len(names) - _MAX_NAMED_FILES)) if len(names) > _MAX_NAMED_FILES else ""
    return "edits landed, not yet covered by a check: %s%s" % (shown, more)


def state_line(*, check_ran_after_edit: bool, check_failed: bool,
               check_inconclusive: bool = False, command: str = "") -> str:
    """One line describing what the host observed about verification of the CURRENT edit.

    The three cases are kept distinct because they call for different next actions, and
    collapsing them is how "it ran and failed" quietly reads as "it ran".  ``command`` is only
    wording; it names what ran or what could run, never what its result was.
    """
    named = (" `%s`" % command) if command else ""
    if not check_ran_after_edit:
        return "check on the current edit: NOT RUN yet%s" % (
            (" (candidate:%s)" % named) if command else "")
    if check_failed:
        return "check on the current edit:%s ran and DID NOT PASS — read its output and fix it" % (
            named or " the check you ran")
    if check_inconclusive:
        return ("check on the current edit:%s ran without asserting the expected result, "
                "which does not verify it" % (named or " the check you ran"))
    # A genuinely passing fresh check is not a pending state at all; the caller drops the hint
    # rather than asking for it, so this branch only guards a future caller.
    return "check on the current edit:%s passed" % (named or " the check you ran")


def verification_state_hint(*, edited_paths=(), check_ran_after_edit: bool = False,
                            check_failed: bool = False, check_inconclusive: bool = False,
                            command: str = "", command_source: str = "",
                            required_override: bool = False) -> dict:
    """The request-scoped message the loop attaches, or ``None`` when there is nothing to say.

    ``required_override`` means this run carries an explicit required-verification wording of
    its own (``Harness.verify_nudge``, e.g. the SWE reproduction contract).  That wording stays
    authoritative, so the hint deliberately names no command and points at it instead — the
    host must not talk a run into a detected pytest when its task defines verification
    differently.
    """
    cmd = command.strip() if isinstance(command, str) else ""
    if "\n" in cmd or len(cmd) > _MAX_COMMAND_CHARS:
        cmd = ""
    if required_override:
        action = _RUN_REQUIRED
        cmd = ""
    elif cmd:
        action = _RUN_NAMED % (
            cmd, (" (%s)" % command_source) if command_source else "")
    else:
        action = _RUN_UNNAMED
    body = "\n".join([
        _HEADER,
        _files_line(edited_paths),
        state_line(check_ran_after_edit=check_ran_after_edit, check_failed=check_failed,
                   check_inconclusive=check_inconclusive, command=cmd),
        _ORDER % action,
    ])
    return {"role": "user", "content": body, "source": "harness", "kind": HINT_KIND}
