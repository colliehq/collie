"""The Inbox — where a question waits when the person who has to answer it is elsewhere.

A run needs a human for one of two things: an approval, or an answer. Whether they are
sitting at the terminal or out of the house, it is the SAME record — parked, awaitable,
and resolvable from any surface. Only the visibility differs:

    VIS_INLINE   someone is attending this run; the prompt appears where they are
    VIS_INBOX    nobody is; it joins the cross-session queue and the phone gets a nudge

Keeping one record instead of two code paths is the whole trick. It means "unattended"
never becomes a second, laxer policy — it only changes who can reach the answer. The
autonomy ceiling is the gate's mode; this module has no opinion about it.

The contract that makes answering from anywhere safe: an item goes ``pending -> resolved``
exactly once, first responder wins, and later attempts are no-ops rather than errors. The
desktop dialog, the web card and a reply from a phone can race; the loser is simply told
nothing happened. collie already solved this shape once for device pairing
(remote.py `_ask_on_screen`) — this is the same rule written down.

Storage is its own SQLite file rather than a table inside actions.db. The main loop's
approvals are deliberately NOT actions.py propose/confirm records: that path is a step
BOUNDARY (the proposing step stops and the run exits `needs_you`), which is right for a
delegated job and wrong for a coding run that may click twenty times. Nothing here shares
a transaction with that store, so sharing its file would only couple two sets of
migrations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field

KIND_APPROVAL = "approval"
KIND_QUESTION = "question"

STATE_PENDING = "pending"
STATE_RESOLVED = "resolved"

VIS_INLINE = "inline"
VIS_INBOX = "inbox"

# Resolutions. Anything not recognised is a refusal — see `outcome_of`.
R_ALLOW = "allow"
R_ALWAYS = "always"
R_DENY = "deny"
R_NEVER = "never"
R_ORPHANED = "orphaned"      # the run went away; nobody can meaningfully answer now


@dataclass
class InboxItem:
    id: str
    session: str
    kind: str = KIND_APPROVAL
    title: str = ""
    body: str = ""
    tool: str = ""
    target: str = ""
    risk: str = ""
    rule_offer: str = ""     # the standing rule "always" would create, "" if none can be
    state: str = STATE_PENDING
    resolution: str = ""
    visibility: str = VIS_INBOX
    call_id: str = ""
    created_at: int = 0
    resolved_at: int = 0

    @property
    def pending(self) -> bool:
        return self.state == STATE_PENDING


def args_preview(args, limit: int = 240) -> str:
    """A compact one-line summary for the card body, so a mirrored "Run browser_type?"
    shows WHAT — never just the tool name, which authorises nothing meaningful.

    Values are already placeholder-form when they came through the loop (the gate runs
    before `_redact.restore`); this only shortens them.
    """
    parts = []
    for k, v in (args or {}).items():
        s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
        s = " ".join(str(s).split())
        if len(s) > 80:
            s = s[:79] + "…"
        parts.append("%s: %s" % (k, s))
    out = " · ".join(parts)
    return (out[:limit - 1] + "…") if len(out) > limit else out


def outcome_of(resolution: str):
    """Map a stored resolution onto a gate Outcome. Everything unrecognised — an empty
    string, a stale value, a garbled reply from a phone — is a refusal. Consent has to be
    stated, never inferred from the absence of a no."""
    from .gate import Outcome
    return {R_ALLOW: Outcome.ALLOW_ONCE,
            R_ALWAYS: Outcome.ALLOW_ALWAYS,
            R_NEVER: Outcome.REJECT_ALWAYS}.get(resolution, Outcome.REJECT_ONCE)


class InboxStore:
    def __init__(self, path: str = None, on_new=None):
        """`on_new(item)` fires once per newly created pending item — the hook a phone
        push or a desktop notification hangs off. It must never raise into the caller: a
        broken notifier would otherwise take down the run it was meant to tell you about.
        """
        path = path or os.path.join(
            os.environ.get("COLLIE_STATE_DIR") or os.path.expanduser("~/.collie"),
            "inbox.db")
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.on_new = on_new
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._waiters: dict = {}
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.db.execute("""CREATE TABLE IF NOT EXISTS inbox_items(
            id TEXT PRIMARY KEY, session TEXT, kind TEXT, title TEXT, body TEXT,
            tool TEXT, target TEXT, risk TEXT, rule_offer TEXT, state TEXT,
            resolution TEXT, visibility TEXT, call_id TEXT,
            created_at INTEGER, resolved_at INTEGER)""")
        # Idempotent by (session, call_id): a reconnecting surface, or a retry of the same
        # tool call, must find the SAME item rather than mint a second one that the user
        # would have to answer twice.
        self.db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS inbox_call
            ON inbox_items(session, call_id) WHERE call_id != ''""")
        self.db.commit()

    # -- writing ------------------------------------------------------------
    def add(self, session, *, kind=KIND_APPROVAL, title="", body="", tool="", target="",
            risk="", rule_offer="", visibility=VIS_INBOX, call_id="") -> InboxItem:
        now = int(time.time())
        item = InboxItem(id=uuid.uuid4().hex[:12], session=session, kind=kind, title=title,
                         body=body, tool=tool, target=target or "", risk=risk,
                         rule_offer=rule_offer or "", visibility=visibility,
                         call_id=call_id or "", created_at=now)
        with self._lock:
            try:
                self.db.execute(
                    "INSERT INTO inbox_items VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (item.id, item.session, item.kind, item.title, item.body, item.tool,
                     item.target, item.risk, item.rule_offer, item.state, item.resolution,
                     item.visibility, item.call_id, item.created_at, item.resolved_at))
                self.db.commit()
            except sqlite3.IntegrityError:
                # Same (session, call_id) already parked — return the existing one, which
                # may ALREADY be resolved (a restart re-raising a prompt that was answered
                # while the process was gone). wait() then returns immediately.
                existing = self._by_call(session, call_id)
                if existing is not None:
                    return existing
                raise
        if self.on_new is not None:
            try:
                self.on_new(item)
            except Exception:
                pass
        return item

    def resolve(self, item_id: str, resolution: str) -> bool:
        """Resolve exactly once. False when the item is unknown or already answered —
        the losing surface is told nothing happened, not handed an error."""
        with self._lock:
            cur = self.db.execute(
                "UPDATE inbox_items SET state=?, resolution=?, resolved_at=? "
                "WHERE id=? AND state=?",
                (STATE_RESOLVED, resolution, int(time.time()), item_id, STATE_PENDING))
            self.db.commit()
            won = cur.rowcount > 0
            ev = self._waiters.get(item_id)
        if won and ev is not None:
            ev.set()
        return won

    def resolve_session(self, session: str, resolution: str = R_ORPHANED) -> int:
        """Close every still-pending item of a run that has ended. An approval whose run
        is gone can never be meaningfully granted, and leaving it pending would show the
        user a decision that no longer does anything."""
        n = 0
        for item in self.pending(session):
            if self.resolve(item.id, resolution):
                n += 1
        return n

    # -- waiting ------------------------------------------------------------
    def wait(self, item_id: str, timeout: float = None) -> str:
        """Block until the item is answered; returns the resolution ("" on timeout).

        threading.Event, not asyncio: collie's loop is synchronous, and the surfaces that
        answer (a TUI thread, an HTTP handler, the relay socket) are threads too.
        """
        cur = self.get(item_id)
        if cur is None:
            return ""
        if not cur.pending:
            return cur.resolution
        with self._lock:
            ev = self._waiters.setdefault(item_id, threading.Event())
            again = self.get(item_id)          # re-read under the lock: it may have been
            if again is not None and not again.pending:   # resolved between the two reads
                return again.resolution
        try:
            ev.wait(timeout)
        finally:
            with self._lock:
                self._waiters.pop(item_id, None)
        done = self.get(item_id)
        return (done.resolution if done else "") or ""

    # -- reading ------------------------------------------------------------
    def get(self, item_id: str):
        r = self.db.execute("SELECT * FROM inbox_items WHERE id=?", (item_id,)).fetchone()
        return self._row(r)

    def _by_call(self, session, call_id):
        r = self.db.execute("SELECT * FROM inbox_items WHERE session=? AND call_id=?",
                            (session, call_id)).fetchone()
        return self._row(r)

    def list(self, session=None, state=None, visibility=None, limit=200) -> list:
        sql, params = "SELECT * FROM inbox_items", []
        where = []
        for col, val in (("session", session), ("state", state), ("visibility", visibility)):
            if val is not None:
                where.append("%s=?" % col)
                params.append(val)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at ASC LIMIT ?"
        params.append(int(limit))
        return [self._row(r) for r in self.db.execute(sql, params).fetchall()]

    def pending(self, session=None) -> list:
        return self.list(session=session, state=STATE_PENDING)

    def reconcile_on_resume(self, session: str) -> dict:
        """What to show when a person takes attended control back: what is still waiting,
        and what was decided while they were away. One authoritative record per question,
        so the recap cannot disagree with what actually happened."""
        return {"pending": [asdict(i) for i in self.pending(session)],
                "recap": [asdict(i) for i in
                          self.list(session=session, state=STATE_RESOLVED)]}

    @staticmethod
    def _row(r):
        return InboxItem(**{k: r[k] for k in r.keys()}) if r is not None else None

    def close(self):
        try:
            self.db.close()
        except Exception:
            pass


# -- the approver ---------------------------------------------------------------
def inbox_approver(store: InboxStore, session: str, *, visibility=VIS_INBOX,
                   timeout: float = None, on_timeout=None):
    """An approver that parks the question and suspends the run until it is answered.

    A timeout is available but defaults to None — waiting forever. That is deliberate:
    turning "nobody answered yet" into "go ahead" is the failure this whole design
    exists to prevent, and turning it into a denial silently loses work a person would
    have approved on their way home. A caller that genuinely needs a bound passes one,
    and `on_timeout` decides what the silence meant.
    """
    def approve(tool_name, args, decision):
        item = store.add(
            session,
            title="Run %s?" % tool_name,
            body=args_preview(args),
            tool=tool_name,
            target=getattr(decision, "target", "") or "",
            risk=getattr(decision, "risk", "") or "",
            rule_offer=getattr(decision, "rule_offer", "") or "",
            visibility=visibility,
            call_id=getattr(decision, "call_id", "") or "")
        if not item.pending:
            # Already answered — a restart re-raising a prompt resolved while we were gone.
            return outcome_of(item.resolution)
        res = store.wait(item.id, timeout=timeout)
        if not res:
            if on_timeout is not None:
                return on_timeout(tool_name, args, decision)
            # Nobody answered inside the bound. Refuse, and close the item so it stops
            # showing as a live decision that no longer has a run behind it.
            store.resolve(item.id, R_DENY)
            return outcome_of(R_DENY)
        return outcome_of(res)

    return approve
