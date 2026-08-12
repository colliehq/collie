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
    tool_calls: int = 0
    arg_repairs: int = 0     # model-quirk arg repairs applied this run (point 7)
    steer_count: int = 0     # mid-run user steering messages injected (point 13)
    denied_calls: int = 0    # tool calls the gate refused (denied, or asked with nobody to answer)
    mem_recalls: int = 0
    wall_ms: int = 0
    success: bool = False
    verified: bool = False   # edited + a repro ran on the fixed code & passed (the gate's verdict)
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
            tool_calls INTEGER, mem_recalls INTEGER, wall_ms INTEGER,
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
                         cache_waste_usd=?,prefix_measured=?,turns=?,tool_calls=?,mem_recalls=?,
                         wall_ms=?,success=?,verified=?,quality=?,cost_usd=?,answer=?,error=?
                         WHERE run_id=?""",
                    (res.prefix_tokens, res.input_tokens, res.output_tokens, res.total_tokens,
                     res.cache_read, res.cache_creation, res.cache_miss_tokens, res.cache_waste_usd,
                     res.prefix_measured, res.turns, res.tool_calls, res.mem_recalls, res.wall_ms,
                     int(res.success), int(res.verified), res.quality, res.cost_usd,
                     (res.answer or "")[:2000], (res.error or "")[:500], res.run_id))
                self.db.commit()
            except sqlite3.OperationalError as e:
                import warnings
                warnings.warn("recorder.finish_run skipped (telemetry, non-fatal): %s" % e)

    def close(self):
        self.db.close()
