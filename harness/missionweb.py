"""MissionService — drive missions over HTTP for `collie web` (the NL front door).

`collie web` (webui/index.html) is the interaction surface the user asked to keep;
this is the thin service behind its mission commands. The user types a goal in
plain words; this starts a campaign, drives it with the REAL model
(mission.ModelDecider over the configured provider), and exposes
status / confirm / resume / tick so the chat UI can show progress, gate an
irreversible action, and carry the campaign on.

It owns no policy of its own: the container (mission.py) keeps durability, the
gate, authority (leash), and evidence; this only marshals goal-in / status-out.
It uses the SAME ~/.collie stores as `collie jobs` / jobsweb, so a mission started
by mouth is visible on every surface. The decider is injectable so tests run
deterministically at $0 (a scripted decider); production builds
ModelDecider(make_provider(<the configured provider>)).
"""

from __future__ import annotations

import os
import secrets

from .actions import ActionStore, RefusedError
from .jobs import NEEDS_YOU
from .mission import (MissionDriver, MissionStore, ModelDecider, create_mission,
                      world_leash)
from .primitives import register_primitives


def _provider_name() -> str:
    # mirrors webapp._provider(): the Settings-panel provider is applied into the
    # env before a request, so this is the same provider the chat GUI runs on.
    return os.environ.get("COLLIE_PROVIDER", "mock")


def _clean(d: dict) -> dict:
    """Drop the injected `_case` context from args/case before it hits the UI."""
    return {k: v for k, v in (d or {}).items() if k != "_case"}


class MissionService:
    def __init__(self, base: str = None, decider=None, provider: str = None, stub=None):
        # base isolates tests; production uses the shared ~/.collie stores (so a
        # mission is visible to `collie jobs` / jobsweb too).
        self.store = MissionStore((base + ".missions") if base else None)
        self.actions = ActionStore((base + ".actions") if base else None)
        self._decider = decider
        name = provider or _provider_name()
        # REAL hands only when a real provider is configured; mock stays on the safe
        # canned stubs (a demo can't drive a real browser / model meaningfully). An
        # explicit stub=True/False overrides.
        self._stub = (name == "mock") if stub is None else bool(stub)
        # build the provider ONCE — shared by the decider and the compose primitive —
        # but only when it's actually needed (an injected decider + stub skips it).
        self._prov = None
        if (decider is None) or (not self._stub):
            from .providers import make_provider
            self._prov = make_provider(name)
        if self._stub:
            register_primitives(stub=True)
        else:
            from .webact import get_actuator
            register_primitives(stub=False, actuator=get_actuator(), provider=self._prov)

    def _driver(self) -> MissionDriver:
        dec = self._decider or ModelDecider(self._prov)
        return MissionDriver(self.store, self.actions, dec)

    # ── commands ──
    def start(self, goal: str, autonomous: bool = False, case: dict = None, **bounds) -> dict:
        mid = "msn_" + secrets.token_hex(6)
        create_mission(self.store, mid, goal, case=case or {},
                       leash=world_leash(autonomous=autonomous, **bounds))
        self._driver().advance(mid)
        return self.status(mid)

    def confirm(self, mid: str, nonce: str) -> dict:
        try:
            self.actions.confirm(nonce)           # human approves the concrete payload
        except RefusedError as e:
            return {**self.status(mid), "error": f"confirm refused: {e}"}
        self._driver().resume(mid)
        return self.status(mid)

    def resume(self, mid: str) -> dict:
        """Accept a hand-off / carry a parked mission on (no gated action to run)."""
        self._driver().resume(mid)
        return self.status(mid)

    def tick(self, mid: str = None) -> dict:
        """Fire any due durable re-checks now (the 'check inbox now' button; also
        what colliejobd calls on wake)."""
        n = self._driver().tick_missions()
        return self.status(mid) if mid else {"advanced": n}

    # ── read ──
    def status(self, mid: str) -> dict:
        m = self.store.get(mid)
        if not m:
            return {"error": "unknown mission", "mission_id": mid}
        inbox = None
        if m.state == NEEDS_YOU:
            name, nonce = self.store.last_parked(mid)
            if nonce:                             # a gated action awaiting confirm
                rec = self.actions.get(nonce)
                inbox = {"nonce": nonce, "capability": name,
                         "args": _clean(rec.args if rec else {})}
        return {
            "mission_id": mid, "goal": m.goal, "state": m.state, "result": m.result,
            "case": _clean(m.case),
            "steps": [{"name": s["name"], "verdict": s["verdict"]}
                      for s in self.store.steps(mid)],
            "inbox": inbox,                       # non-null -> render a Confirm button
            "needs_human": m.state == NEEDS_YOU and inbox is None,  # -> Accept hand-off
            "receipts": [{"capability": r["capability"], "verdict": r["verdict"],
                          "fired": bool(r["fired"])}
                         for r in self.actions.receipts() if r.get("job_id") == mid],
        }

    def missions(self) -> list:
        return [{"mission_id": m.mission_id, "goal": m.goal, "state": m.state}
                for m in self.store.list()]

    def close(self):
        self.store.close()
        self.actions.close()
