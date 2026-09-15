"""Progress tracking — every run and turn is recorded to runs.db.

This is the substrate for the dashboard and for the CC comparison. Both `collie` and
`cc` runs are recorded with the SAME schema so they are directly comparable, and
so you can watch metrics move as you evolve the harness (prefix_tokens trending
down is the whole game for pain #2).
"""
from __future__ import annotations
import sqlite3
import threading
import time
from dataclasses import dataclass, field


@dataclass
class RunResult:
    run_id: int = 0
    parent_run_id: int | None = None
    task_id: str = ""
    harness: str = "collie"
    model: str = ""
    provider: str = ""
    prefix_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read: int = 0
    cache_creation: int = 0   # Anthropic cache-WRITE tokens (billed ~1.25x input; DeepSeek ~0)
    cache_miss_tokens: int = 0   # tokens re-billed that SHOULD have cache-hit (prefix-bust ledger)
    cache_waste_usd: float = 0.0  # $ those misses cost — a prefix-busting regression shows up here
    prefix_measured: int | None = None  # provider-usage-measured prefix (None = unmeasured; est-only)
    turns: int = 0
    # Physical provider requests, including transport retries, format repair,
    # critic, and synthesis calls.  Distinct from logical loop turns.
    model_calls: int = 0
    # True only when the harness consumed its declared turn ceiling.  Evaluators use this to
    # distinguish a normal unresolved attempt from a provider or adapter failure.
    turns_exhausted: bool = False
    budget_exhausted: bool = False
    budget_limits: dict = field(default_factory=dict)
    canceled: bool = False
    stop_reason: str = ""
    retry_at: int = 0              # upstream quota reset; Mission persists its wait separately
    # Set by `note_host_error` when a failure is recorded AFTER run() returned (save,
    # required check, effect boundary).  It withdraws the provider-wait reading of
    # `retry_at`: the run is no longer merely early.
    host_error: bool = False
    tool_calls: int = 0
    arg_repairs: int = 0     # model-quirk arg repairs applied this run (point 7)
    contract_repairs: int = 0  # bounded structured-response corrections (not transport retries)
    steer_count: int = 0     # mid-run user steering messages injected (point 13)
    input_failures: list[dict] = field(default_factory=list)
    denied_calls: int = 0    # tool calls the gate refused (denied, or asked with nobody to answer)
    mem_recalls: int = 0
    wall_ms: int = 0
    success: bool = False
    verified: bool = False   # edited + a repro ran on the fixed code & passed (the gate's verdict)
    edited: bool = False
    # Claims distilled from this run begin as proposals.  An outer host can use these ids to
    # promote/reject them after a verification command that necessarily runs after Harness.run().
    memory_claim_ids: list[int] = field(default_factory=list)
    quality: float = 0.0     # LLM-judge 0-10 (task completion quality)
    cost_usd: float = 0.0    # estimated $ from tokens x model price
    checkpoint_ref: str = ""  # tree snapshot taken before this run; "" when one could not be taken
    answer: str = ""
    error: str = ""
    messages: list = None    # the full conversation thread (for --continue / repl session save)


class Recorder:
    def __init__(self, path: str):
        # WAL + busy_timeout so many isolated connections can write concurrently
        # (parallel comparison runner). check_same_thread off for pool workers.
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        # ONE shared connection with check_same_thread off (execute_code RPC handler threads and the
        # parallel comparison runner both log through the same Recorder). A single connection is NOT
        # safe for concurrent execute+commit ("Recursive use of cursors" / interleaved txns), so
        # serialize every write behind this lock.
        self._lock = threading.Lock()
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self._init()

    def _init(self):
        c = self.db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS runs(
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts INTEGER, task_id TEXT, harness TEXT, model TEXT, provider TEXT,
            prefix_tokens INTEGER, input_tokens INTEGER, output_tokens INTEGER,
            total_tokens INTEGER, cache_read INTEGER, turns INTEGER,
            tool_calls INTEGER, contract_repairs INTEGER DEFAULT 0,
            mem_recalls INTEGER, wall_ms INTEGER,
            success INTEGER, verified INTEGER DEFAULT 0,
            quality REAL DEFAULT 0, cost_usd REAL DEFAULT 0,
            answer TEXT, error TEXT, note TEXT)""")
        c.execute("""CREATE TABLE IF NOT EXISTS turns(
            run_id INTEGER, idx INTEGER, kind TEXT, detail TEXT,
            tokens_in INTEGER, tokens_out INTEGER, prefix_tokens INTEGER, ms INTEGER)""")
        # Guarded migrations: ALTER an existing DB in place (older runs.db predates these columns).
        # Each column is added independently so a partially-migrated DB completes; existing rows get
        # NULL — dashboard/SQL reading them must COALESCE(x,0).
        for tbl, col, decl in [
            ("turns", "cache_read", "INTEGER"), ("turns", "cache_miss", "INTEGER"),
            ("turns", "miss_cause", "TEXT"),
            ("runs", "cache_creation", "INTEGER"),   # RunResult had this field but it was never persisted
            ("runs", "cache_miss_tokens", "INTEGER"), ("runs", "cache_waste_usd", "REAL"),
            ("runs", "prefix_measured", "INTEGER"),
            ("runs", "verified", "INTEGER DEFAULT 0"),
            ("runs", "contract_repairs", "INTEGER DEFAULT 0"),
            ("runs", "parent_run_id", "INTEGER"),
            ("runs", "stop_reason", "TEXT"),
        ]:
            try:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (tbl, col, decl))
            except sqlite3.OperationalError:
                pass                                  # column already exists — idempotent
        self.db.commit()

    def start_run(self, task_id, harness, model, provider, note="") -> int:
        with self._lock:
            cur = self.db.execute(
                """INSERT INTO runs(ts,task_id,harness,model,provider,prefix_tokens,
                     input_tokens,output_tokens,total_tokens,cache_read,turns,tool_calls,
                     mem_recalls,wall_ms,success,answer,error,note)
                   VALUES(?,?,?,?,?,0,0,0,0,0,0,0,0,0,0,'','',?)""",
                (int(time.time()), task_id, harness, model, provider, note))
            self.db.commit()
            return cur.lastrowid

    def log_turn(self, run_id, idx, kind, detail, tokens_in, tokens_out,
                 prefix_tokens, ms, cache_read=0, cache_miss=0, miss_cause=""):
        with self._lock:
            # NAMED columns (not positional VALUES(?×8)) — the table now has 11 columns after the
            # ALTER migration, and a positional insert would misalign / error against it.
            # Telemetry must NEVER crash the actual run/web-request: a schema drift (an older DB, or a
            # process running stale code against a migrated runs.db) degrades to a warning, not a 500.
            try:
                self.db.execute(
                    """INSERT INTO turns(run_id,idx,kind,detail,tokens_in,tokens_out,prefix_tokens,ms,
                         cache_read,cache_miss,miss_cause)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, idx, kind, (detail or "")[:500], tokens_in, tokens_out, prefix_tokens, ms,
                     cache_read, cache_miss, (miss_cause or "")[:40]))
                self.db.commit()
            except sqlite3.OperationalError as e:
                import warnings
                warnings.warn("recorder.log_turn skipped (telemetry, non-fatal): %s" % e)

    def finish_run(self, res: RunResult):
        with self._lock:
            try:                                     # telemetry: never crash the run on schema drift
                self.db.execute(
                    """UPDATE runs SET prefix_tokens=?,input_tokens=?,output_tokens=?,
                         total_tokens=?,cache_read=?,cache_creation=?,cache_miss_tokens=?,
                         cache_waste_usd=?,prefix_measured=?,turns=?,tool_calls=?,contract_repairs=?,mem_recalls=?,
                         wall_ms=?,success=?,verified=?,quality=?,cost_usd=?,answer=?,error=?,
                         parent_run_id=?,stop_reason=?
                         WHERE run_id=?""",
                    (res.prefix_tokens, res.input_tokens, res.output_tokens, res.total_tokens,
                     res.cache_read, res.cache_creation, res.cache_miss_tokens, res.cache_waste_usd,
                      res.prefix_measured, res.turns, res.tool_calls, res.contract_repairs,
                      res.mem_recalls, res.wall_ms,
                     int(res.success), int(res.verified), res.quality, res.cost_usd,
                     (res.answer or "")[:2000], (res.error or "")[:500],
                     getattr(res, "parent_run_id", None), run_stop_reason(res), res.run_id))
                self.db.commit()
            except sqlite3.OperationalError as e:
                import warnings
                warnings.warn("recorder.finish_run skipped (telemetry, non-fatal): %s" % e)

    def close(self):
        self.db.close()


def run_stop_reason(result):
    """Execution outcome, separate from whether the result was independently verified.

    Re-evaluate host errors: a check or transcript save can fail after run().
    """
    if getattr(result, "canceled", False):
        return "canceled"
    if getattr(result, "error", ""):
        return "error"
    if getattr(result, "turns_exhausted", False):
        return "turn_limit"
    if getattr(result, "budget_exhausted", False):
        return "budget_limit"
    return getattr(result, "stop_reason", "") or "completed"


def root_run_filter(db):
    """Avoid counting child usage twice; tolerate unmigrated read-only databases."""
    columns = {row[1] for row in db.execute("PRAGMA table_info(runs)")}
    return "parent_run_id IS NULL" if "parent_run_id" in columns else "1=1"


# An epoch older than this cannot be a live quota window — it is a default, a
# millisecond value divided wrong, or a receipt from another era.  Eight days is the
# same horizon `providers.provider_retry_at` uses: it covers weekly plan windows
# without accepting an unbounded wait.
PROVIDER_WAIT_FLOOR = 1_600_000_000     # 2020-09-13 UTC
PROVIDER_WAIT_HORIZON = 8 * 86400


def provider_wait_at(value, now=None) -> int:
    """A provider-attested quota reset a receipt may carry, or 0 for anything else.

    ``providers.provider_retry_at`` is the gate at the upstream boundary and accepts only
    a reset still in the future, which is right there: a reset already past is not a
    reason to stop calling.  A DURABLE receipt is read back long after it was written, so
    the same fact has a second reading — the reset has since arrived and the run is ready
    to be retried by hand.  That is why this validator accepts a past epoch and the
    upstream one does not; callers tell the two apart with ``now``, never by guessing.

    Everything the host cannot READ as an epoch is refused outright, because the value
    decides whether a person is told to wait: ``True`` is not 1, a float (NaN, inf and
    1.7e9 alike) is not an epoch, and a JSON string of digits is not one either.
    """
    now = time.time() if now is None else now
    if type(value) is not int:          # bool/float/str/None never grant a wait
        return 0
    return value if PROVIDER_WAIT_FLOOR <= value <= now + PROVIDER_WAIT_HORIZON else 0


def provider_wait_state(result, now=None, recovery_required=False) -> dict | None:
    """The quota reset this run is genuinely stopped on, or None.

    A wait is a claim that nothing is wrong except the clock, so it is made only when
    every other verdict agrees.  A cancellation, a budget or turn or output cap, and a
    run that finished all outrank it: those are what actually ended the run, and the
    reset time left on the result is then only metadata.  So does a HOST failure recorded
    after the run returned (`note_host_error`) — a transcript that would not save or a
    required check that would not certify is not something waiting for a provider fixes,
    and it must never be dressed up as one because an old `retry_at` is still lying around.

    ``recovery_required`` is passed IN rather than read off the result, because the result
    genuinely does not know it: a fence lives in the session journal and in the worker's
    own settlement report, and each host learns it at a different moment (`webapp` from
    `_durable_external_fence` and `recovery_state`, the CLI from the worker receipt and
    the check boundary, `web_tasks` from its caller).  A fenced thread may have fired a
    tool outside this process; "come back at 14:00" is the wrong headline for it, whatever
    the provider said about quota.  A caller that cannot know passes nothing and gets the
    old reading — which is why every composition site below is explicit.
    """
    if recovery_required:
        return None
    if getattr(result, "host_error", False):
        return None
    if run_stop_reason(result) != "error":
        return None
    retry_at = provider_wait_at(getattr(result, "retry_at", 0), now=now)
    return {"retry_at": retry_at} if retry_at else None


def note_host_error(result, message):
    """Append a host-side failure to a finished run and retire its provider-wait state.

    Everything appended after ``Harness.run`` returns — a required check's verdict, a
    receipt or transcript that would not persist, an effect boundary that would not
    close — belongs to the host, not to the provider.  A run that stopped on a quota
    reset and then failed to save is not "waiting until 14:00"; it needs a person now.
    The reset time stays on the result for whoever wants to read it, but this run has
    stopped being an ordinary wait, and every surface reading `run_outcome` learns that
    from the same place rather than from the shape of an error string.
    """
    text = str(message or "")
    if not text:
        return getattr(result, "error", "")
    result.error = ((result.error + "; ") if result.error else "") + text
    try:
        result.host_error = True
    except Exception:                     # a frozen/slotted stand-in still gets the text
        pass
    return result.error


def run_outcome(result, now=None, recovery_required=False):
    """Shared terminal fields for CLI, live events and durable run receipts.

    ``recovery_required`` is the thread's fence as the calling host knows it at the moment
    the row is composed.  It never appears in the returned row — the hosts write their own
    ``recovery_required`` key beside this one — it only withdraws the wait reading.
    """
    reason = run_stop_reason(result)
    wait = provider_wait_state(result, now=now, recovery_required=recovery_required)
    return {"stop_reason": reason, "completed": reason == "completed",
            "edited": bool(getattr(result, "edited", False)),
            "turns_exhausted": bool(getattr(result, "turns_exhausted", False)),
            "budget_exhausted": bool(getattr(result, "budget_exhausted", False)),
            "budget_limits": dict(getattr(result, "budget_limits", {}) or {}),
            "canceled": bool(getattr(result, "canceled", False)),
            "model_calls": getattr(result, "model_calls", 0),
            # Present only on a real wait, so a legacy receipt and a receipt for a run
            # that stopped for any other reason are the same thing to a reader: absent.
            **({"provider_wait": True, "retry_at": wait["retry_at"]} if wait else {}),
            "parent_run_id": getattr(result, "parent_run_id", None)}
