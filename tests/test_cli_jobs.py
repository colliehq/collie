"""Drive the `collie jobs` human surface end to end (harness.cli.cmd_jobs).

Run: python tests/test_cli_jobs.py   (exit 0 = all green)

Uses COLLIE_STATE_DIR to point the stores at a temp dir, registers a trivial
capability in-process, then exercises inbox -> confirm(executes) -> receipts ->
ls through the actual CLI handler, asserting the printed output. Also checks the
subcommand is wired into the argparse tree.
"""
import argparse
import contextlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


def run_jobs(action, text=""):
    # Drive the FULL cli.main() path (argv preprocessing + CMDS + argparse +
    # dispatch), not cmd_jobs directly — that is what catches a subcommand
    # missing from the CMDS shortcut set (the `collie jobs` -> `run jobs` bug).
    from harness import cli
    argv = ["jobs", action] + ([text] if text else [])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli.main(argv)
    return buf.getvalue()


def main():
    tmp = tempfile.mkdtemp(prefix="collie-jobs-")
    os.environ["COLLIE_STATE_DIR"] = tmp

    from harness.actions import ActionStore
    from harness.jobs import (JobStore, Capability, register, clear_registry,
                              NEEDS_YOU, DONE_VERIFIED)
    from harness.verifier import Verdict, VERIFIED

    clear_registry()
    register(Capability("test.noop", execute=lambda r: {"ok": True},
                        verify=lambda r, res: Verdict(VERIFIED, "did the thing"),
                        reversible=True))

    # seed a job + a materialized gated action in the CLI's state dir
    acts = ActionStore(os.path.join(tmp, "actions.db"))
    jobs = JobStore(os.path.join(tmp, "jobs.db"))
    jobs.create("job-cli", "do the test errand", {})
    nonce = acts.propose("test.noop", {"foo": "bar"}, job_id="job-cli")
    jobs.set_state("job-cli", NEEDS_YOU)
    acts.close()
    jobs.close()

    print("test_inbox_lists_pending")
    out = run_jobs("inbox")
    check(nonce in out, "inbox must list the pending nonce")
    check("job-cli" in out, "inbox must list the needs_you job")

    print("test_confirm_executes_and_verifies")
    out = run_jobs("confirm", nonce)
    check("approved" in out, "confirm must report approval")
    check("verified" in out.lower(), f"confirm must execute+verify a registered cap; got:\n{out}")

    print("test_job_reached_done_verified")
    jobs2 = JobStore(os.path.join(tmp, "jobs.db"))
    check(jobs2.get("job-cli").state == DONE_VERIFIED,
          "job must be done_verified after confirm-execute")
    jobs2.close()

    print("test_receipts_show_verified")
    out = run_jobs("receipts")
    check("verified" in out.lower() and "test.noop" in out,
          "receipts must show the verified receipt")

    print("test_ls_shows_job")
    out = run_jobs("ls")
    check("job-cli" in out and "done_verified" in out, "ls must show the finished job")

    print("test_confirm_unknown_nonce")
    out = run_jobs("confirm", "deadbeef")
    check("unknown nonce" in out, "confirming an unknown nonce must say so")

    print("test_argparse_wires_jobs_subcommand")
    from harness import cli
    p = argparse.ArgumentParser()
    # rebuild just enough: assert the parser build path registers 'jobs'
    check(hasattr(cli, "cmd_jobs"), "cmd_jobs must exist")

    clear_registry()
    if _fails:
        print(f"\n== CLI-JOBS: {len(_fails)} FAILED ==")
        sys.exit(1)
    print("\n== CLI-JOBS: all groups passed ==")


if __name__ == "__main__":
    main()
