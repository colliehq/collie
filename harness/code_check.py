"""The one host check a dedicated code Mission's coding loop may run on itself.

A Mission code slice is a filesystem-confined read/edit/search loop with no
shell, and until now it also had no way to run anything.  The consequence was
not safety, it was blindness: the loop wrote a patch, guessed that it worked,
and only found out after the slice had ended and the host ran the configured
check — by which time the run that could have fixed it was over.  Repairing a
one-line mistake therefore cost a whole extra slice: a resumed prompt, a
re-read of the repository, another full check.  That is where a real five-slice
run's 113 model calls went.

So the loop gets exactly one execution hand, and it is deliberately not a
general one:

* It runs the ONE command the user pre-authorized for this Mission at creation,
  in the ONE workspace bound to it.  Both are captured from host state when the
  tool is constructed.
* It takes no arguments at all.  Any argument is refused, because the only
  arguments such a tool could have are the command and the directory, and
  neither may come from a model.
* It is backed by :func:`harness.verification.run_verification_command`, the
  same host function the Mission's own verifier uses, so a stop kills the
  process tree, the output is bounded and redacted, and the receipt is real
  typed evidence rather than a sentence the model wrote about its own work.
* Its receipts belong to the host.  The model sees the true verdict and the
  true output — that is the whole point, it has to be able to repair — but
  nothing it says can become evidence, and it cannot mark its own work passed.

There is no browser, no network, no MCP, no arbitrary command, and no way to
widen this later by accident: the command is not a parameter of the call.

The tool does not arm its own durable check boundary.  It does not need one:
the execution loop journals ``executing_tool`` before every tool call and only
retires that fence when the call returns, so a process that dies mid-check is
already recovery-fenced.  What the loop cannot see by itself is a command whose
process tree outlived the call, so the tool reports that through the host-only
``tool_effect_uncertain`` channel, which fences the run.
"""
from __future__ import annotations

import os
import threading

from .tools import Tool

# The receipts every host check in this Mission carries.  The in-slice tool runs
# the same pre-authorized command with the same authority as the end-of-slice
# verifier, so it is the same source; ``invoked_by`` is what tells the two apart
# in an audit.
CHECK_SOURCE = "mission_code_profile"
IN_SLICE_INVOKER = "code_agent_tool"
# A repair loop needs several checks; nothing needs dozens.  This bounds host
# command executions inside one slice, which is a different resource from
# logical turns — a suite can take minutes, and the Mission's step leash is the
# thing that would otherwise absorb the cost.
MAX_IN_SLICE_CHECKS = 12


class VerificationCommandTool(Tool):
    """Run this Mission's exact pre-authorized check against its bound workspace."""

    name = "run_verification"
    tier = "always"
    # No properties, and no additional ones: the schema itself says that the
    # command and the working directory are not the model's to choose.
    schema = {"type": "object", "properties": {}, "additionalProperties": False}

    def __init__(self, command, workspace, timeout_seconds=300, receipts=None,
                 on_event=None, max_runs=MAX_IN_SLICE_CHECKS):
        self.command = str(command or "").strip()
        self.workspace = os.path.realpath(os.path.abspath(str(workspace or "")))
        try:
            timeout = int(timeout_seconds or 300)
        except (TypeError, ValueError, OverflowError):
            timeout = 300
        self.timeout_seconds = max(1, min(3600, timeout))
        # Host-owned, one list per slice.  The caller keeps the reference; the
        # tool only appends.
        self.receipts = receipts if isinstance(receipts, list) else []
        self.on_event = on_event
        self.max_runs = max(1, int(max_runs or 1))
        self._lock = threading.Lock()
        self.description = (
            "Run this workspace's verification check and see its real output. "
            "Takes NO arguments: it always runs exactly the command the user "
            "pre-authorized for this Mission (%s) in this workspace, and neither "
            "can be changed from here. Use it after you edit, read the output, "
            "fix what failed, and run it again. Finish only once it passes, or "
            "say plainly that it does not." % (self.command or "none configured"))

    def run(self, args, ctx):
        if args:
            keys = (sorted(str(key)[:40] for key in args)[:8]
                    if isinstance(args, dict) else [type(args).__name__])
            return ("ERROR(verification): this tool takes no arguments, and refused "
                    "the ones supplied (%s). The command and the directory are fixed "
                    "by the Mission the user authorized; they are not call "
                    "parameters. Call it again with no arguments." % ", ".join(keys))
        if not self.command:
            return ("ERROR(verification): this Mission has no configured verification "
                    "command, so there is nothing this tool may run.")
        # The bound workspace is host state, not ctx state.  If the loop is
        # somehow pointed somewhere else, that is a wiring fault and the check
        # must not run: a check that certifies the wrong directory is worse than
        # no check at all.
        cwd = str(getattr(ctx, "cwd", "") or "")
        try:
            same = bool(cwd) and os.path.realpath(os.path.abspath(cwd)) == self.workspace
        except (OSError, ValueError):
            same = False
        if not same:
            return ("ERROR(verification): the coding loop is not running in this "
                    "Mission's bound workspace, so the check was not started.")
        with self._lock:
            if len(self.receipts) >= self.max_runs:
                return ("ERROR(verification): this slice has already run the check %d "
                        "times, which is its limit. Stop editing speculatively: "
                        "explain what still fails and what you would change next."
                        % self.max_runs)
            from .verification import run_verification_command
            evidence = run_verification_command(
                self.command, self.workspace, timeout=self.timeout_seconds,
                source=CHECK_SOURCE, after_last_edit=True,
                cancelled=getattr(ctx, "cancelled", None), on_event=self.on_event)
            evidence["invoked_by"] = IN_SLICE_INVOKER
            self.receipts.append(evidence)
        if evidence.get("executed") and not evidence.get("process_tree_terminated"):
            # Something this call started may still be writing files.  The loop's
            # journal fence retires when this returns, so the only honest thing
            # is to tell the host the run's extent is unknown.
            ctx.tool_effect_uncertain = True
        return render_check_result(evidence)


def render_check_result(evidence) -> str:
    """What the model is allowed to see: the real verdict and the real output."""
    evidence = evidence if isinstance(evidence, dict) else {}
    command = str(evidence.get("command") or "")
    if evidence.get("cancelled"):
        headline = ("VERIFICATION STOPPED: the check was cancelled before it "
                    "finished. This is not a verdict about the code.")
    elif not evidence.get("executed"):
        headline = ("VERIFICATION DID NOT RUN: the command never started, so "
                    "nothing here says anything about the code.")
    elif evidence.get("timed_out"):
        # A killed check has no exit code, and the old headline printed that as
        # "exited None" under the word FAILED — which spends a repair round
        # hunting for a broken test nobody has evidence of.  The partial output
        # below is a transcript of an unfinished run: what it already printed
        # happened, what it never reached is simply unknown.  Older receipts
        # carry no deadline, and "stopped after Nones" would be worse than
        # saying only what is known.
        limit = evidence.get("timeout_s")
        stopped = ("was stopped after %ss without finishing" % limit
                   if isinstance(limit, int) and not isinstance(limit, bool)
                   and limit > 0 else "was stopped before it finished")
        headline = ("VERIFICATION TIMED OUT: the check %s, so it did not judge "
                    "this code. Failures the partial output below already "
                    "prints are real — fix those. The check never reached the "
                    "rest, so its silence there is neither a pass nor a "
                    "failure: do not invent a cause for one. If nothing shown "
                    "is fixable, make the check cheaper to run, or say plainly "
                    "that it needs longer than this Mission allows." % stopped)
    elif evidence.get("passed"):
        headline = "VERIFICATION PASSED: the configured check exited 0 on these exact bytes."
    elif evidence.get("command_passed"):
        headline = ("VERIFICATION INCONCLUSIVE: the command exited 0, but the "
                    "workspace changed while it was running (%s), so it does not "
                    "certify these bytes. Do not treat this as a pass — make no "
                    "edit and run it again." % (evidence.get("freshness") or "unknown"))
    else:
        headline = ("VERIFICATION FAILED: the configured check exited %s. Read the "
                    "output below, fix the cause, and run this check again."
                    % evidence.get("exit_code"))
    output = str(evidence.get("output") or "").strip() or "(the check produced no output)"
    return "%s\ncommand: %s\nduration_ms: %s\n--- check output ---\n%s" % (
        headline, command, evidence.get("duration_ms"), output)


def check_window_mutated(receipts) -> bool:
    """Did any in-slice check change represented workspace bytes itself?

    A check runs project code and may write build output.  Those bytes are not
    the agent's patch, and — exactly like the end-of-slice verifier — a check
    that overwrites represented source invalidates this slice's patch ownership
    rather than being laundered into it.
    """
    for receipt in receipts or []:
        if not isinstance(receipt, dict):
            continue
        before, after = (str(receipt.get("tree_digest") or ""),
                         str(receipt.get("post_tree_digest") or ""))
        if not (receipt.get("snapshot_complete") and
                receipt.get("post_snapshot_complete")):
            # An incomplete snapshot cannot rule the change out, and "we could
            # not tell" must never read as "nothing happened".
            return True
        if before != after:
            return True
    return False


def agent_mutated_outside_checks(pre_digest, receipts, post_digest):
    """Did the AGENT change the tree, ignoring what its own checks changed?

    The slice is a sequence of digests: the pre-slice tree, then for each check
    the tree before and after it, then the agent boundary at the end.  Every
    other interval belongs to the agent, and every interval between one check's
    two digests belongs to that check.  Comparing only the agent's intervals is
    what stops a check's ``.coverage`` file from being reported as a patch, and
    stops a check that ran after the last edit from hiding one.

    Returns ``None`` when a boundary digest is missing or its snapshot was
    incomplete, because then the question cannot be answered from digests at all
    and the caller must fall back to its own conservative rule.
    """
    marks = [str(pre_digest or "")]
    for receipt in receipts or []:
        if not isinstance(receipt, dict):
            return None
        if not (receipt.get("snapshot_complete") and
                receipt.get("post_snapshot_complete")):
            return None
        marks.append(str(receipt.get("tree_digest") or ""))
        marks.append(str(receipt.get("post_tree_digest") or ""))
    marks.append(str(post_digest or ""))
    if not all(marks):
        return None
    return any(marks[index] != marks[index + 1]
               for index in range(0, len(marks) - 1, 2))


def reusable_receipt(receipts, command, current_tree_digest):
    """The in-slice receipt that already certifies exactly these bytes, if any.

    Re-running an identical command over an identical tree costs the user real
    minutes for an answer the host already holds.  But a receipt may only stand
    in for the end-of-slice check when every one of these is true, because each
    of them is a way the two could differ:

    * it came from this host's own check runner, invoked by the in-slice tool;
    * its command is character-for-character the configured command;
    * it actually executed, was not cancelled, and PASSED;
    * ``ran_after_last_edit`` — the tree did not move under it while it ran;
    * both of its snapshots are complete, so its digests mean something;
    * and the tree it saw, before and after, is still the tree that exists now.

    The digests are the important ones: they are what "after the last edit"
    means here.  If the agent touched a single represented byte after the check,
    they differ and this returns ``None``, so the host runs the check itself.

    A failing receipt is deliberately NOT reused even though it satisfies
    everything else.  Reuse only exists to avoid paying twice for an answer the
    host already has, and a red result closes nothing, so there is no saving
    worth the chance that a flaky suite locks in a failure the run could have
    disproved.  The host reruns it, and the fresh failure is what the next slice
    is shown.
    """
    wanted = str(command or "").strip()
    current = str(current_tree_digest or "")
    if not wanted or not current:
        return None
    for receipt in reversed(list(receipts or [])):
        if not isinstance(receipt, dict):
            continue
        if (receipt.get("source") != CHECK_SOURCE or
                receipt.get("invoked_by") != IN_SLICE_INVOKER):
            continue
        if str(receipt.get("command") or "").strip() != wanted:
            continue
        if not receipt.get("executed") or receipt.get("cancelled"):
            continue
        if not receipt.get("passed"):
            continue
        if not receipt.get("ran_after_last_edit"):
            continue
        if not (receipt.get("snapshot_complete") and
                receipt.get("post_snapshot_complete")):
            continue
        if (str(receipt.get("tree_digest") or "") != current or
                str(receipt.get("post_tree_digest") or "") != current):
            continue
        return dict(receipt)
    return None
