"""collie tui — a rich, friendly interactive chat for the collie harness.

    python -m harness.cli tui [--provider …] [--model M] [--resume ID | --continue]

Why this exists: collie is "lean" in the PROMPT, not the interface. This is the interface
investment — a genuinely nice terminal chat that keeps the FULL conversation thread across
turns (persisted as a session, so you can --resume later) and, while the agent works, renders
LIVE what it's doing off the harness `emit` bus:

  • a tool / edit / repro TIMELINE that grows as actions fire,
  • the VERIFICATION GATE front-and-center — ✗ FAILING flips to ✓ PASSING, colored,
  • syntax-highlighted diffs straight from the edit event (old → new),
  • a final RECEIPT line — verified · tokens · $ · time — the honest tally collie promises.

Slash commands: /exit /quit, /new (fresh thread), /resume <id>, /sessions, /help.

If `rich` isn't installed it degrades to a clean plain-text REPL (same session continuity,
same live event stream) with a one-line hint to `pip install rich` for the full experience.
Nothing here is required by the core; it's a pure UI layer over Harness.run + sessions.
"""
from __future__ import annotations
import os
import queue
import sys
import threading


class _StdinFeed:
    """Single owner of stdin for the TUI's whole lifetime — kills the two-readers-race between the
    REPL prompt and mid-run steering (point 13). A daemon thread pumps lines into a queue;
    readline_blocking() serves the prompt, drain() serves mid-run steering. Only armed on a real
    TTY: piped stdin (scripts, tests) must NOT be slurped as mid-run hints."""

    def __init__(self, stream=None):
        self._stream = stream if stream is not None else sys.stdin
        try:
            self.tty = bool(self._stream.isatty())
        except Exception:
            self.tty = False
        self._q = queue.Queue()
        self._t = threading.Thread(target=self._pump, daemon=True)
        self._t.start()

    def _pump(self):
        try:
            for line in self._stream:
                self._q.put(line.rstrip("\n"))
        except Exception:
            pass
        self._q.put(None)                 # EOF sentinel

    def readline_blocking(self, prompt=""):
        """Blocking prompt read (Ctrl-C stays responsive via the 0.2s poll). None on EOF."""
        if prompt:
            try:
                sys.stdout.write(prompt); sys.stdout.flush()
            except Exception:
                pass
        while True:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                self._q.put(None)         # EOF is sticky
                return None
            return item

    def drain(self):
        """Non-blocking: queued NON-slash lines become steering; slash lines are re-queued for the
        REPL to honor after the run (not injected mid-run); the EOF sentinel is preserved."""
        steer, deferred = [], []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None or item.startswith("/"):
                deferred.append(item)     # EOF or slash-command: honor post-run, don't inject
            else:
                steer.append(item)
        for d in deferred:
            self._q.put(d)
        return steer

# ---- optional dependency: rich. Absence must never crash — fall back to plain text. -------
try:
    from rich.console import Console, Group
    from rich.live import Live
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.syntax import Syntax
    from rich.text import Text
    from rich.table import Table
    from rich.rule import Rule
    from rich.markdown import Markdown
    _HAVE_RICH = True
except Exception:                                    # pragma: no cover - trivial import guard
    _HAVE_RICH = False


# --------------------------------------------------------------------------- #
# Shared helpers (used by both the rich and the plain paths)
# --------------------------------------------------------------------------- #
def _fmt_cost(c):
    if not c:
        return "$0"
    return "$%.4f" % c if c < 1 else "$%.2f" % c


def _fmt_tokens(n):
    n = int(n or 0)
    if n >= 1000:
        return "%.1fk" % (n / 1000.0)
    return str(n)


def _short_args(args):
    """A compact one-line summary of a tool call's args for the timeline."""
    if not isinstance(args, dict):
        return ""
    for k in ("command", "path", "query", "pattern", "url", "text", "file"):
        v = args.get(k)
        if v:
            v = str(v).replace("\n", " ")
            return v if len(v) <= 72 else v[:69] + "…"
    # fall back to the first value
    for v in args.values():
        s = str(v).replace("\n", " ")
        if s:
            return s if len(s) <= 72 else s[:69] + "…"
    return ""


def _lang_for(path):
    ext = os.path.splitext(path or "")[1].lower()
    return {".py": "python", ".js": "javascript", ".ts": "typescript", ".jsx": "javascript",
            ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".c": "c",
            ".cpp": "cpp", ".h": "c", ".rb": "ruby", ".sh": "bash", ".html": "html",
            ".css": "css", ".json": "json", ".md": "markdown", ".sql": "sql",
            ".toml": "toml", ".yaml": "yaml", ".yml": "yaml"}.get(ext, "text")


# --------------------------------------------------------------------------- #
# Live render state — accumulated from emit events, rendered by RichRun
# --------------------------------------------------------------------------- #
class _RunState:
    """Mutable view-model for one agent turn, driven by h.emit callbacks."""
    def __init__(self):
        self.events = []          # list of ("tool"|"edit"|"repro", payload)
        self.gate = None          # None (no repro yet) | False (failing) | True (passing)
        self.receipt = None       # dict from the receipt event
        self.spinner_i = 0

    def on_event(self, kind, d):
        if kind == "receipt":
            self.receipt = d
            return
        if kind == "repro":
            self.gate = bool(d.get("passed"))
        self.events.append((kind, d))


# --------------------------------------------------------------------------- #
# Rich TUI
# --------------------------------------------------------------------------- #
_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class RichTUI:
    def __init__(self, console):
        self.c = console

    # ----- static chrome ---------------------------------------------------- #
    def banner(self, sid, provider, model, prior_turns, cwd):
        head = Text()
        head.append("collie", style="bold cyan")
        head.append("  ·  interactive chat\n", style="dim")
        head.append("session ", style="dim")
        head.append(sid, style="yellow")
        head.append("   provider ", style="dim")
        head.append(str(provider), style="green")
        if model:
            head.append("   model ", style="dim")
            head.append(str(model), style="green")
        head.append("\n%d prior turn(s)  ·  " % prior_turns, style="dim")
        head.append(os.path.abspath(cwd), style="dim")
        head.append("\n/help for commands  ·  /exit to quit", style="dim italic")
        self.c.print(Panel(head, border_style="cyan", padding=(0, 2)))

    def help(self):
        t = Table.grid(padding=(0, 2))
        t.add_column(style="bold cyan")
        t.add_column()
        for cmd, desc in [
            ("/exit  /quit", "leave (session is saved)"),
            ("/new", "start a fresh conversation thread"),
            ("/model [name]", "list models / switch (e.g. /model terra)"),
            ("/resume <id>", "load a previous session by id"),
            ("/sessions", "list recent sessions"),
            ("/help", "show this"),
        ]:
            t.add_row(cmd, desc)
        self.c.print(Panel(t, title="commands", border_style="dim", padding=(0, 1)))

    def sessions(self, rows):
        if not rows:
            self.c.print("[dim]no saved sessions yet[/dim]")
            return
        t = Table(box=None, pad_edge=False)
        t.add_column("id", style="yellow")
        t.add_column("turns", justify="right", style="cyan")
        t.add_column("last answer", style="dim", overflow="ellipsis", max_width=60)
        for r in rows:
            t.add_row(r["id"], str(r["turns"]), r.get("last", ""))
        self.c.print(Panel(t, title="recent sessions", border_style="dim", padding=(0, 1)))

    # ----- the live turn ---------------------------------------------------- #
    def _gate_line(self, st):
        if st.gate is None:
            return Text("○ verification: no reproduction run yet", style="dim")
        if st.gate:
            return Text("✓ VERIFICATION PASSING", style="bold white on green")
        return Text("✗ VERIFICATION FAILING", style="bold white on red")

    def _timeline(self, st, running=True):
        rows = []
        from rich.markup import escape as _esc   # model/tool text with "[/…]" would else MarkupError
        for kind, d in st.events:
            if kind == "tool":
                ok = d.get("ok", True)
                mark = "[green]•[/green]" if ok else "[red]✗[/red]"
                name = _esc(str(d.get("name", "?")))
                rows.append(Text.from_markup(
                    "%s [bold]%s[/bold] [dim]%s[/dim]" % (mark, name, _esc(_short_args(d.get("args"))))))
            elif kind == "edit":
                path = _esc(str(d.get("path") or "(file)"))
                rows.append(Text.from_markup("[magenta]✎ edit[/magenta] [bold]%s[/bold]" % path))
                diff = self._diff(d)
                if diff is not None:
                    rows.append(diff)
            elif kind == "repro":
                passed = d.get("passed")
                asserted = " (assert)" if d.get("asserted") else ""
                cmd = _esc(str(d.get("cmd", "")))
                if passed:
                    rows.append(Text.from_markup(
                        "[green]▸ repro passed%s[/green] [dim]%s[/dim]" % (asserted, cmd)))
                else:
                    rows.append(Text.from_markup(
                        "[red]▸ repro failed%s[/red] [dim]%s[/dim]" % (asserted, cmd)))
            elif kind == "steer":                          # mid-run user steering (point 13)
                rows.append(Text.from_markup("[yellow]↳ you:[/yellow] %s" % _esc(str(d.get("text", "")))))
            elif kind == "retry":                          # bounded transient-error retry (point 5)
                rows.append(Text.from_markup(
                    "[dim]↻ retry %s/%s in %ss — %s[/dim]" % (d.get("attempt"), d.get("max"),
                    d.get("delay_s"), _esc(str(d.get("error", ""))[:60]))))
            elif kind == "overflow_recovery":              # context-overflow shrink+retry (point 9)
                rows.append(Text.from_markup("[dim]⤵ context overflow — shrinking history, retrying[/dim]"))
        if running:
            st.spinner_i = (st.spinner_i + 1) % len(_SPIN)
            rows.append(Text.from_markup(
                "[cyan]%s[/cyan] [dim]working…[/dim]" % _SPIN[st.spinner_i]))
        elif not rows:
            rows.append(Text("(no tool activity)", style="dim"))
        return Group(*rows)

    def _diff(self, d):
        """Syntax-highlighted old→new from an edit event. Trimmed so it never floods."""
        old, new = d.get("old") or "", d.get("new") or ""
        lang = _lang_for(d.get("path"))

        def block(label, code, style):
            code = code.rstrip("\n")
            if not code:
                return None
            lines = code.split("\n")
            if len(lines) > 14:
                lines = lines[:13] + ["… (%d more lines)" % (len(lines) - 13)]
                code = "\n".join(lines)
            syn = Syntax(code, lang, theme="ansi_dark", word_wrap=True,
                         background_color="default")
            return Panel(syn, title=label, title_align="left", border_style=style,
                         padding=(0, 1))

        parts = []
        ob = block("− old", old, "red") if old else None
        nb = block("+ new", new, "green")
        if ob is not None:
            parts.append(ob)
        if nb is not None:
            parts.append(nb)
        if not parts:
            return None
        return Group(*parts)

    def _panel(self, st, running=True):
        body = Group(self._gate_line(st), Rule(style="dim"), self._timeline(st, running))
        title = "agent working…" if running else "turn complete"
        return Panel(body, title=title, title_align="left",
                     border_style="cyan" if running else "green", padding=(1, 2))

    def run_turn(self, h, task_id, line, history):
        """Run one agent turn with a Live panel wired to h.emit. Returns RunResult."""
        st = _RunState()
        prev_emit = h.emit
        h.emit = st.on_event
        result = {}
        try:
            with Live(self._panel(st, True), console=self.c, refresh_per_second=12,
                      transient=False) as live:
                # emit fires synchronously inside h.run (same thread) — refresh on each event
                def emit(kind, d):
                    st.on_event(kind, d)
                    try:
                        live.update(self._panel(st, kind != "receipt"))
                    except Exception:
                        pass
                h.emit = emit
                res = h.run(task_id, line, consolidate=True, history=history)
                result["res"] = res
                live.update(self._panel(st, False))
        finally:
            h.emit = prev_emit
        res = result.get("res")
        self._answer(res)
        self._receipt(st, res)
        return res

    def _answer(self, res):
        if res is None:
            return
        text = res.answer or res.error or "(no output)"
        style = "red" if (res.error and not res.answer) else "cyan"
        try:
            body = Markdown(text) if res.answer else Text(text, style="red")
        except Exception:
            body = Text(text)
        self.c.print(Panel(body, title="collie", title_align="left",
                           border_style=style, padding=(1, 2)))

    def _receipt(self, st, res):
        d = st.receipt or {}
        verified = d.get("verified")
        if res is not None and not st.receipt:
            d = {"total_tokens": res.total_tokens, "turns": res.turns,
                 "tool_calls": res.tool_calls, "wall_ms": res.wall_ms, "cost_usd": res.cost_usd}
        line = Text()
        if verified is True:
            line.append("✓ verified", style="bold green")
        elif verified is False:
            line.append("○ unverified", style="dim")
        else:
            line.append("· done", style="dim")
        line.append("   ")
        line.append("%s tok" % _fmt_tokens(d.get("total_tokens")), style="cyan")
        line.append("  ·  ")
        line.append(_fmt_cost(d.get("cost_usd")), style="yellow")
        line.append("  ·  ")
        line.append("%d turns" % (d.get("turns") or 0), style="dim")
        line.append("  ·  ")
        line.append("%d tools" % (d.get("tool_calls") or 0), style="dim")
        line.append("  ·  ")
        line.append("%.1fs" % ((d.get("wall_ms") or 0) / 1000.0), style="dim")
        waste = d.get("cache_waste_usd") or 0
        if waste > 0:
            line.append("  ·  ")
            line.append("cache waste $%.4f (%d)" % (waste, d.get("cache_misses") or 0), style="red")
        self.c.print(line)


# --------------------------------------------------------------------------- #
# Plain-text fallback (no rich) — same continuity + same live event stream
# --------------------------------------------------------------------------- #
class PlainTUI:
    def __init__(self):
        self.w = sys.stdout

    def _p(self, s=""):
        # strip C0/C1 control chars — model/tool output could otherwise inject ANSI (clear screen,
        # set window title, forge a prompt) into the plain terminal. Rich path sanitizes on render.
        import re as _re
        print(_re.sub(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]', '', str(s)), file=self.w, flush=True)

    def banner(self, sid, provider, model, prior_turns, cwd):
        self._p("pip install rich for the full TUI (live timeline, diffs, colored gate)")
        self._p("collie chat · session %s · %s%s · %d prior turn(s)"
                % (sid, provider, ("/" + model) if model else "", prior_turns))
        self._p("/exit quit · /new fresh thread · /resume <id> · /sessions · /help")

    def help(self):
        self._p("commands: /exit /quit  /new  /model [name]  /resume <id>  /sessions  /help")

    def sessions(self, rows):
        if not rows:
            self._p("(no saved sessions)")
            return
        for r in rows:
            self._p("  %s  turns=%d  %s" % (r["id"], r["turns"], r.get("last", "")))

    def run_turn(self, h, task_id, line, history):
        prev_emit = h.emit

        def emit(kind, d):
            if kind == "tool":
                self._p("  · %s %s%s" % (d.get("name"), "" if d.get("ok", True) else "[ERR] ",
                                         _short_args(d.get("args"))))
            elif kind == "edit":
                self._p("  ✎ edit %s" % (d.get("path") or ""))
            elif kind == "repro":
                self._p("  %s repro%s %s" % ("✓" if d.get("passed") else "✗",
                                             " (assert)" if d.get("asserted") else "",
                                             d.get("cmd", "")))
            elif kind == "receipt":
                v = d.get("verified")
                self._p("  %s · %s tok · %s · %d turns · %d tools · %.1fs" % (
                    "verified" if v else "unverified", _fmt_tokens(d.get("total_tokens")),
                    _fmt_cost(d.get("cost_usd")), d.get("turns") or 0,
                    d.get("tool_calls") or 0, (d.get("wall_ms") or 0) / 1000.0))
        h.emit = emit
        try:
            res = h.run(task_id, line, consolidate=True, history=history)
        finally:
            h.emit = prev_emit
        self._p("\n" + (res.answer or res.error or "(no output)"))
        return res


# --------------------------------------------------------------------------- #
# The REPL driver — shared control flow over whichever UI backend is active
# --------------------------------------------------------------------------- #
def _read_line(console, have_rich):
    if have_rich:
        try:
            return Prompt.ask("\n[bold cyan]›[/bold cyan]", console=console).strip()
        except (EOFError, KeyboardInterrupt):
            return None
    try:
        return input("\n› ").strip()
    except (EOFError, KeyboardInterrupt):
        return None


def run_tui(cwd, provider, model, project="", resume=None, cont=False, goal=None):
    """Entry used by cli.py's `tui` subcommand. Builds a harness, runs the interactive loop."""
    from .cli import make_harness
    from . import sessions as sess
    from .memory import project_scope

    project = project or project_scope(cwd)   # the codebase, not the surface it was reached from

    have_rich = _HAVE_RICH
    console = Console() if have_rich else None
    ui = RichTUI(console) if have_rich else PlainTUI()

    from .cli import default_gate
    _gate = default_gate(cwd)
    h = make_harness(cwd, provider=provider, model=model, project=project,
                     code_search=True, web_search=True, exec_code=True, delegate=True,
                     gate=_gate)

    sid = resume or (sess.latest() if cont else None) or sess.new_id()
    loaded = sess.load(sid) if (resume or cont) else None
    history = (loaded or {}).get("messages") or []
    if goal:
        h.memory.set_block("project:" + project, "goal", goal[:390], char_limit=400)

    prior = sum(1 for m in history if m.get("role") == "user")
    ui.banner(sid, provider, model, prior, cwd)

    # ONE stdin owner for the whole session (only on a real TTY — piped input stays on _read_line so
    # scripted stdin isn't slurped by the background thread). Enables typing to steer a live run.
    feed = _StdinFeed() if sys.stdin.isatty() else None
    if feed is not None and have_rich:
        console.print("[dim]tip: type while the agent works to steer it; Ctrl-C aborts the turn[/dim]")

    saved = bool(history)          # a resumed session already has a file; a fresh one has nothing yet
    try:
        while True:
            if feed is not None:
                raw = feed.readline_blocking("\n› ")
                line = raw.strip() if raw is not None else None
            else:
                line = _read_line(console, have_rich)
            if line is None:                          # EOF / Ctrl-C at the prompt
                break
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/help":
                ui.help(); continue
            if line == "/sessions":
                ui.sessions(sess.recent(10)); continue
            if line == "/new":
                history, sid = [], sess.new_id()
                if have_rich:
                    console.print("[dim]new session[/dim] [yellow]%s[/yellow]" % sid)
                else:
                    print("[new session %s]" % sid)
                continue
            if line.startswith("/resume"):
                parts = line.split(None, 1)
                rid = parts[1].strip() if len(parts) > 1 else sess.latest()
                s = sess.load(rid) if rid else None
                if s:
                    history, sid = s.get("messages") or [], rid
                    msg = "resumed %s (%d prior turns)" % (
                        sid, sum(1 for m in history if m.get("role") == "user"))
                else:
                    msg = "no such session: %s" % rid
                if have_rich:
                    console.print("[dim]%s[/dim]" % msg)
                else:
                    print(msg)
                continue
            if line.startswith("/model"):
                # switch the live model/provider mid-session. `/model` lists the catalog;
                # `/model terra` fuzzy-matches; `/model codex-oauth:gpt-5.6-terra` is explicit.
                from .providers import make_provider
                from . import catalog, settings as _st
                parts = line.split(None, 1)
                arg = parts[1].strip() if len(parts) > 1 else ""
                ents = catalog.list_entries(discover_live=False)
                cur = "%s:%s" % (h.provider.name, h.provider.model)
                if not arg:
                    rows = ["current: %s" % cur]
                    for e in ents:
                        mark = "*" if e["id"] == cur else " "
                        badge = e["via"] if e["auth"] == "ok" else ("[%s]" % e["auth"])
                        rows.append("  %s %-32s %s" % (mark, e["id"], badge))
                    body = "\n".join(rows)
                    console.print("[dim]%s[/dim]" % body) if have_rich else print(body)
                    continue
                match = None
                if ":" in arg:
                    p, m = catalog.resolve(arg)
                    match = {"id": arg, "provider": p, "model": m}
                else:
                    al = arg.lower()
                    cands = [e for e in ents
                             if al in (e["label"] + " " + e["model"] + " " + e["id"]).lower()]
                    match = cands[0] if cands else None
                if not match:
                    msg = "no model matches %r (run /model to list)" % arg
                    console.print("[red]%s[/red]" % msg) if have_rich else print(msg)
                    continue
                try:
                    h.provider = make_provider(match["provider"], match.get("model"))
                    provider, model = h.provider.name, h.provider.model
                    _st.update({"PROVIDER": match["provider"], "MODEL": match.get("model") or ""})
                    msg = "switched to %s · %s" % (provider, model)
                    console.print("[green]%s[/green]" % msg) if have_rich else print(msg)
                except Exception as ex:
                    msg = "cannot switch to %s: %s" % (match["id"], ex)
                    console.print("[red]%s[/red]" % msg) if have_rich else print(msg)
                continue

            try:
                if feed is not None and feed.tty:
                    h.steering = feed.drain     # let mid-run keystrokes steer the agent
                    # The approval prompt reads through the SAME pump, or it would fight the
                    # steering thread for stdin and neither would get a whole line.
                    from .approve import tty_approver
                    _w = (lambda s: console.print("[yellow]%s[/yellow]" % s)) if have_rich else print
                    h.approve = tty_approver(
                        read_line=lambda: feed.readline_blocking(
                            "  allow? [y]es / [a]lways / [N]o: "),
                        write=_w, gate=_gate)
                res = ui.run_turn(h, "tui", line, history)
            except KeyboardInterrupt:
                # Ctrl-C DURING a turn aborts just this turn, not the whole session — h.run only
                # catches Exception, and an uncaught KeyboardInterrupt (a BaseException) would
                # otherwise print a traceback and tear down the interactive loop.
                msg = "⏹ turn interrupted — back to the prompt (Ctrl-C again at an empty prompt to exit)"
                console.print("\n[dim]%s[/dim]" % msg) if have_rich else print("\n" + msg)
                continue
            finally:
                h.steering = None               # steering only during a run
                h.approve = None                # and nobody is at the prompt between turns
            history = res.messages
            sess.save(sid, history, project=project, cwd=cwd, answer=res.answer or "")
            saved = True
    finally:
        try:
            h.memory.close(); h.recorder.close()
        except Exception:
            pass
        # only advertise --resume if a turn actually completed + saved; a fresh open->/exit leaves no
        # file, so the resume hint would load None and start empty.
        tail = ("session saved: %s   ·   resume: collie tui --resume %s" % (sid, sid)
                if saved else "(no turns — nothing saved)")
        if have_rich:
            console.print("\n[dim]%s[/dim]" % tail)
        else:
            print("\n" + tail)
    return 0


def main(argv=None):
    """Standalone entry: `python -m harness.tui [task-flags]`. Mirrors the cli `tui` subcommand
    so the module is runnable on its own for testing."""
    import argparse
    p = argparse.ArgumentParser(prog="collie tui", description="collie interactive chat (rich TUI)")
    p.add_argument("--provider", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--cwd", default=None)
    p.add_argument("--project", default=None)
    p.add_argument("--goal", default=None)
    p.add_argument("--continue", dest="cont", action="store_true",
                   help="continue the latest session's thread")
    p.add_argument("--resume", default=None, metavar="ID", help="resume a session by id")
    args = p.parse_args(list(sys.argv[1:] if argv is None else argv))
    provider = args.provider or os.environ.get("COLLIE_PROVIDER", "mock")
    return run_tui(args.cwd or os.getcwd(), provider, args.model, project=args.project,
                   resume=args.resume, cont=args.cont, goal=args.goal)


if __name__ == "__main__":
    sys.exit(main())
