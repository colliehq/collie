"""delegate — hand a noisy sub-investigation to a single-depth child agent that has its OWN
clean context, returning only the child's final summary. A token-discipline mechanism (Hermes'
framing), NOT Claude-Code-style fan-out: the parent's context window never sees the child's
dozens of read/grep/tool messages — only the distilled answer comes back.

Capped at ONE level: a delegated agent runs with COLLIE_SUBAGENT=1, and both default_registry
(no delegate tool registered under that flag) AND this tool's own guard refuse further nesting,
so total tree cost can't blow up — the documented Hermes footgun where independent child budgets
make the tree exceed the parent cap.
"""
import os

from .tools import Tool


class DelegateTool(Tool):
    name, tier = "delegate", "always"
    description = (
        "Delegate a focused, read-heavy sub-task to a fresh child agent with its own CLEAN "
        "context — e.g. 'find every call site of parse_config and summarize the signatures', or "
        "'investigate why test_x fails and report the root cause'. You get back ONLY the child's "
        "final summary; its exploration never enters your context, saving tokens. Best for noisy "
        "investigations whose intermediate tool output you don't need. Args: task (required), "
        "optional max_turns (default 12).")
    schema = {"type": "object", "properties": {
        "task": {"type": "string"}, "max_turns": {"type": "integer"}},
        "required": ["task"]}

    def run(self, args, ctx):
        task = (args.get("task") or "").strip()
        if not task:
            return "ERROR: empty task"
        if os.environ.get("COLLIE_SUBAGENT") == "1":
            return ("ERROR: delegation is single-depth — a delegated agent cannot itself "
                    "delegate (prevents runaway sub-sub-agent cost). Do this part directly.")
        from .cli import make_harness
        from . import settings
        provider = settings.get("PROVIDER", "anthropic")   # env > settings.json > API default
        try:
            max_turns = max(1, min(30, int(args.get("max_turns", 12))))
        except (TypeError, ValueError):
            max_turns = 12
        prev = os.environ.get("COLLIE_SUBAGENT")
        # set the flag BEFORE make_harness so the child's registry is built WITHOUT the delegate
        # tool (single-depth); otherwise the child advertises a tool it can only be refused on.
        os.environ["COLLIE_SUBAGENT"] = "1"
        h = None
        try:
            h = make_harness(ctx.cwd, provider=provider, model=os.environ.get("COLLIE_MODEL"),
                             project=ctx.project, code_search=True)
            h.max_turns = max_turns
            res = h.run("delegate", task, consolidate=False)
            return (res.answer or res.error or "(child agent produced no answer)")[:6000]
        except Exception as e:                       # make_harness or run failure — never leak/escape
            return "ERROR(delegate): %s" % e
        finally:
            if prev is None:
                os.environ.pop("COLLIE_SUBAGENT", None)
            else:
                os.environ["COLLIE_SUBAGENT"] = prev
            if h is not None:                        # only close what we actually opened
                try:
                    h.memory.close()
                    h.recorder.close()
                except Exception:
                    pass


def register_delegate(registry):
    registry.register(DelegateTool())
    return True
