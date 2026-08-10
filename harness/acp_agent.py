"""collie as an ACP (Agent Client Protocol) agent — one implementation that plugs into Zed,
JetBrains, neovim, OpenCode, and VS Code (via the community ACP client), reusing collie's
Harness loop verbatim. The distribution multiplier over a per-editor extension.

collie's streaming `emit` events map straight onto ACP's native session/update primitives, so
the SIGNATURE verification gate renders in every editor for free:
  emit("tool")    -> ToolCallStart (human titles: "Read foo.py" / "Search \"bug\"" / "$ pytest")
                     with jump-to-file `locations` so read/search/edit are clickable.
  emit("edit")    -> ToolCallStart(kind=edit) with a native FileEditToolCallContent DIFF.
  emit("repro")   -> the VERIFICATION GATE: the reproduction's execute call is upgraded in place
                     (ToolCallProgress) from "in_progress" to completed/failed — the executed
                     pass/fail is the signal (no synthesized "model thought").
  emit("receipt") -> UsageUpdate(cost) + a final AgentMessageChunk — the honest receipt
                     (verified · tokens · turns · time · $), rendered AFTER the answer.

Every newer ACP primitive (ToolCallProgress, AgentThoughtChunk, ToolCallLocation) is feature-
detected, so an older `acp` build renders a still-clean fallback instead of crashing.

Run:  collie acp        (the editor spawns this over stdio)
"""
import asyncio
import os
import uuid

import acp
import acp.schema as s

# Feature-detect the richer session/update primitives so older `acp` builds degrade gracefully
# (absence never crashes the agent — we fall back to plain ToolCallStart / no locations).
_HAS_PROGRESS = hasattr(s, "ToolCallProgress")
_HAS_THOUGHT = hasattr(s, "AgentThoughtChunk")
_HAS_LOC = hasattr(s, "ToolCallLocation")

# read/search verbs + ACP tool `kind` for the tools collie streams. Human titles, not raw
# function names — an editor sidebar should read like a colleague's activity, not a call log.
_VERBS = {
    "read_file": "Read", "code_search": "Search", "grep": "Grep", "ripgrep": "Search",
    "list_files": "List", "glob": "Find", "web_search": "Web search",
    "web_fetch": "Fetch", "fetch": "Fetch",
}
_KINDS = {
    "read_file": "read", "code_search": "search", "grep": "search", "ripgrep": "search",
    "list_files": "search", "glob": "search", "web_search": "fetch",
    "web_fetch": "fetch", "fetch": "fetch",
}


def _tb(text):
    return s.TextContentBlock(text=text, type="text")


def _oneline(t, n):
    t = " ".join((t or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _basename(path):
    p = str(path or "")
    return os.path.basename(p.rstrip("/")) or p


def _loc(path, line=None):
    """A clickable jump-to-file target for the editor, if the schema supports it."""
    if not _HAS_LOC or not path:
        return None
    try:
        return [s.ToolCallLocation(path=str(path), line=line)]
    except Exception:
        return None


def _tool_title(name, args):
    """(title, kind, path) — a human, editor-friendly label for a read/search tool call."""
    args = args or {}
    q = args.get("query") or args.get("pattern")
    path = args.get("path") or args.get("file")
    kind = _KINDS.get(name, "other")
    if name == "read_file":
        return ("Read " + (_basename(path) or "file"), kind, path)
    if name in ("code_search", "grep", "ripgrep"):
        term = _oneline(str(q or path or ""), 60)
        return ('%s "%s"' % (_VERBS.get(name, "Search"), term),
                kind, path if not q else None)
    verb = _VERBS.get(name)
    if verb:
        return ((verb + " " + _oneline(str(q or path or ""), 60)).strip(), kind, path)
    return ((name + " " + _oneline(str(q or path or ""), 60)).strip(), kind, path)


class CollieAgent(acp.Agent):
    def __init__(self):
        self.conn = None
        self.sessions = {}          # session_id -> {"cwd": ...}
        self._n = 0
        self._last_bash = None      # (tool_call_id, command) — upgraded in place by the gate

    def _tcid(self):
        self._n += 1
        return "tc-%d" % self._n

    def on_connect(self, conn):
        self.conn = conn

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kw):
        return acp.InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=s.AgentCapabilities(
                load_session=False,
                prompt_capabilities=s.PromptCapabilities(image=False, audio=False,
                                                         embedded_context=True)),
            agent_info=s.Implementation(name="collie", version="1")
            if hasattr(s, "Implementation") else None)

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kw):
        sid = "collie-" + uuid.uuid4().hex[:12]
        self.sessions[sid] = {"cwd": cwd or os.getcwd()}
        return acp.NewSessionResponse(session_id=sid)

    async def prompt(self, session_id, prompt, **kw):
        task = " ".join(getattr(b, "text", "") for b in prompt if getattr(b, "text", "")).strip()
        sess = self.sessions.setdefault(session_id, {"cwd": os.getcwd()})
        cwd = sess.get("cwd", os.getcwd())
        loop = asyncio.get_running_loop()
        self._last_bash = None

        from .cli import (apply_turn_decision, make_harness, resolve_turn_decision,
                          turn_decision_receipt)
        from . import settings
        # env > settings.json > API default — the ACP entry never calls settings.apply(), so read
        # settings directly to keep the web Settings panel authoritative here too.
        provider = settings.get("PROVIDER", "anthropic")
        model = settings.get("MODEL") or None      # settings.get is env > settings.json > default;
        #                                            reading COLLIE_MODEL directly ignored the panel
        # Bridge the SYNC emit callback (fired from the worker thread) onto THIS event loop.
        # The receipt is captured, not streamed inline, so we can render it AFTER the final
        # answer (metadata reads better below the substance it summarizes).
        receipt = {}

        def _bridge(kind, dd):
            if kind == "receipt":
                receipt.update(dd)
                return
            asyncio.run_coroutine_threadsafe(self._send(session_id, kind, dd), loop)

        h = None
        try:
            history = sess.get("messages")   # ACP is a LONG session (many prompts) -> carry thread
            decision = resolve_turn_decision(
                task, provider, configured_model=model, history=history,
                receipts=sess.get("run_receipts") or [])
            # build INSIDE the try — make_harness -> AnthropicOAuth raises on a missing token, and
            # that must reach the user as a chat message, not an escaped JSON-RPC error.
            from .cli import default_gate
            _gate = default_gate(cwd)
            h = make_harness(cwd, provider=provider, model=decision.model,
                             effort=decision.effort, speed=decision.speed,
                             project="acp", code_search=True, embed="hash",
                             exec_code=True, delegate=True, gate=_gate)
            apply_turn_decision(h, decision, _gate)
            h.emit = _bridge
            # ACP has a permission request of its own, so the editor renders its NATIVE
            # approval UI and collie writes no interface at all. Until now this adapter
            # auto-approved everything it was asked to do, which in an editor that offers
            # to ask is the worst of both: the affordance exists and is never used.
            h.approve = self._acp_approver(session_id, loop, _gate)
            await self._send(session_id, "decision", decision.to_dict())
            res = await loop.run_in_executor(
                None, lambda: h.run("acp", task, consolidate=False, history=history))
            sess["messages"] = res.messages  # remember for the next prompt in this session
            outcome = turn_decision_receipt(decision, res, getattr(h, "provider", None))
            sess.setdefault("run_receipts", []).append(outcome)
            sess["run_receipts"] = sess["run_receipts"][-40:]
            receipt["decision"] = outcome["decision"]
            receipt["model"] = outcome["model"]
            receipt["actual_speed"] = outcome["actual_speed"]
            if res.answer:
                await self.conn.session_update(session_id, s.AgentMessageChunk(
                    session_update="agent_message_chunk", content=_tb(res.answer)))
            if receipt:
                await self._send(session_id, "receipt", receipt)
        except Exception as e:
            await self.conn.session_update(session_id, s.AgentMessageChunk(
                session_update="agent_message_chunk",
                content=_tb("collie could not run: %s: %s" % (type(e).__name__, e))))
        finally:
            if h is not None:
                try:
                    h.memory.close(); h.recorder.close()
                except Exception:
                    pass
        return acp.PromptResponse(stop_reason="end_turn")

    def _acp_approver(self, session_id, loop, gate):
        """Turn a gate "ask" into ACP's `session/request_permission`.

        The harness runs on a worker thread while the JSON-RPC connection lives on the
        event loop, so the request is scheduled onto the loop and this thread blocks on
        the future — which is exactly the semantics wanted: the tool call does not proceed
        until the editor's user answers.

        Option ids ARE collie's outcome values (both sides use ACP's four kinds), so the
        response maps straight back with no translation table to drift.
        """
        import asyncio as _a

        from .gate import Outcome

        async def _ask(tool_name, args, decision):
            from .approve import describe
            opts = [s.PermissionOption(optionId=Outcome.ALLOW_ONCE.value,
                                       name="Allow", kind="allow_once")]
            offer = gate.standing_rule_offer(tool_name, decision.target) if gate else None
            if offer:
                # Only offered when a rule can actually be pinned to something concrete.
                # An "always" that silently meant "always, anywhere" would be a lie told
                # in the editor's own UI.
                opts.append(s.PermissionOption(optionId=Outcome.ALLOW_ALWAYS.value,
                                               name="Always allow %s" % offer,
                                               kind="allow_always"))
            opts.append(s.PermissionOption(optionId=Outcome.REJECT_ONCE.value,
                                           name="Don't", kind="reject_once"))
            title, kind, path = _tool_title(tool_name, args)
            res = await self.conn.request_permission(
                session_id,
                s.ToolCallUpdate(toolCallId="gate-%s" % uuid.uuid4().hex[:8],
                                 title=title or describe(tool_name, args),
                                 kind=kind, status="pending", locations=_loc(path)),
                opts)
            outcome = getattr(res, "outcome", None)
            # DeniedOutcome carries outcome="cancelled" and no option_id: the user closed
            # the prompt or the client cancelled. Not consent.
            chosen = getattr(outcome, "option_id", None)
            return chosen or Outcome.REJECT_ONCE.value

        def approve(tool_name, args, decision):
            try:
                fut = _a.run_coroutine_threadsafe(_ask(tool_name, args, decision), loop)
                return fut.result()
            except Exception:
                # A client that does not implement request_permission, a dropped
                # connection, a cancelled turn — every one of them means nobody said yes.
                return Outcome.REJECT_ONCE.value

        return approve

    async def _send(self, sid, kind, d):
        import sys as _sys
        if os.environ.get("COLLIE_ACP_DEBUG"):
            print("[_send] kind=%s conn=%s" % (kind, self.conn is not None), file=_sys.stderr, flush=True)
        try:
            if kind == "tool":
                name, ok = d.get("name"), d.get("ok", True)
                args = d.get("args") or {}
                status = "completed" if ok else "failed"
                # edits arrive as a first-class diff event; skip the bare call here.
                if name in ("edit_file", "write_file"):
                    return
                # bash: render every shell command (pytest, pip, git, ls — previously invisible)
                # as an execute call. A post-edit reproduction is a bash call too; we stash its
                # id so the repro/gate event can upgrade THIS very call in place (no duplicate).
                if name == "bash":
                    cmd = str(args.get("command") or "").strip()
                    tcid = self._tcid()
                    self._last_bash = (tcid, cmd)
                    await self.conn.session_update(sid, s.ToolCallStart(
                        session_update="tool_call", tool_call_id=tcid,
                        title="$ " + _oneline(cmd, 72), kind="execute", status=status,
                        content=[s.ContentToolCallContent(content=_tb(cmd), type="content")]
                        if cmd else None))
                    return
                title, akind, path = _tool_title(name, args)
                await self.conn.session_update(sid, s.ToolCallStart(
                    session_update="tool_call", tool_call_id=self._tcid(),
                    title=title, kind=akind, status=status, locations=_loc(path)))

            elif kind == "edit":
                path = d.get("path", "")
                await self.conn.session_update(sid, s.ToolCallStart(
                    session_update="tool_call", tool_call_id=self._tcid(),
                    title="Edit " + (_basename(path) or "file"), kind="edit",
                    status="completed", locations=_loc(path),
                    content=[s.FileEditToolCallContent(
                        path=path, old_text=d.get("old", ""), new_text=d.get("new", ""),
                        type="diff")]))

            elif kind == "repro":
                # THE VERIFICATION GATE. The reproduction just ran on the edited code; flip the
                # reproduction's own execute call from "in_progress" to completed/failed so the
                # gate visibly gates. (We do NOT synthesize a fake "model thought" here — collie's
                # brand is honest signals; the executed pass/fail IS the signal.)
                passed = d.get("passed")
                asserted = d.get("asserted")
                cmd = d.get("cmd", "")
                badge = " (assert-checked)" if asserted else ""
                title = ("Verification gate · reproduction "
                         + ("passed ✓" if passed else "failed ✗") + badge)
                status = "completed" if passed else "failed"
                content = [s.ContentToolCallContent(content=_tb(cmd), type="content")] if cmd else None

                lb = self._last_bash
                if _HAS_PROGRESS and lb:
                    # Upgrade the bash call we just rendered into the labelled gate: name it,
                    # show it "running", then flip to the verdict — one call, no duplicate.
                    tcid = lb[0]
                    self._last_bash = None
                    await self.conn.session_update(sid, s.ToolCallProgress(
                        session_update="tool_call_update", tool_call_id=tcid,
                        title=title, kind="execute", status="in_progress", content=content))
                    await self.conn.session_update(sid, s.ToolCallProgress(
                        session_update="tool_call_update", tool_call_id=tcid, status=status))
                else:
                    # Fallback (older schema / no tracked bash): a single labelled execute call.
                    await self.conn.session_update(sid, s.ToolCallStart(
                        session_update="tool_call", tool_call_id=self._tcid(),
                        title=title, kind="execute", status=status, content=content))

            elif kind == "receipt":
                tok = d.get("total_tokens", 0) or 0
                cost = float(d.get("cost_usd", 0.0) or 0.0)
                await self.conn.session_update(sid, s.UsageUpdate(
                    session_update="usage_update", used=tok, size=200000,
                    cost=s.Cost(amount=cost, currency="USD")))
                decision = d.get("decision") if isinstance(d.get("decision"), dict) else {}
                setup = "%s/%s" % (d.get("model") or decision.get("model") or "model",
                                     decision.get("effort") or "default")
                if d.get("error"):
                    line = "collie · %s · ⚠ %s · %s tok · $%.4f" % (
                        setup, _oneline(str(d["error"]), 80), "{:,}".format(tok), cost)
                else:
                    gate = "✓ verified" if d.get("verified") else "· unverified"
                    sec = (d.get("wall_ms", 0) or 0) / 1000.0
                    line = ("collie · %s · %s · %s tok · %d turns · "
                            "%d tools · %.1fs · $%.4f") % (
                        setup, gate, "{:,}".format(tok), d.get("turns", 0) or 0,
                        d.get("tool_calls", 0) or 0, sec, cost)
                await self.conn.session_update(sid, s.AgentMessageChunk(
                    session_update="agent_message_chunk", content=_tb(line)))

            elif kind == "decision":
                line = "Auto route · %s · %s · %s/%s/%s" % (
                    d.get("model", ""), d.get("effort", "default"),
                    d.get("intent", "build"), d.get("quality", "balanced"),
                    d.get("verification", "auto"))
                cls = s.AgentThoughtChunk if _HAS_THOUGHT else s.AgentMessageChunk
                await self.conn.session_update(sid, cls(
                    session_update=("agent_thought_chunk" if _HAS_THOUGHT else
                                    "agent_message_chunk"), content=_tb(line)))
        except Exception as e:
            if os.environ.get("COLLIE_ACP_DEBUG"):
                import sys as _sys
                print("[_send ERROR] %s: %s" % (kind, e), file=_sys.stderr, flush=True)


def main():
    asyncio.run(acp.run_agent(CollieAgent()))


if __name__ == "__main__":
    main()
