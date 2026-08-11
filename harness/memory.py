"""Memory subsystem — the differentiator vs Claude Code's linear-scan index.

Three tiers (Letta x Hermes), one SQLite file, scoped per project:

  CORE      pinned blocks, char-capped, loaded every turn (in the VOLATILE tail
            of the prompt so they can update mid-session without busting cache).
  ARCHIVAL  facts store: text + keys + embedding, retrieved ON DEMAND via a
            HYBRID query (BM25 over FTS5  +  dense cosine  ->  RRF fusion).
  (RECALL   verbatim message log lives in the recorder's events; a future
            messages_fts seam can be added the same way as facts_fts.)

The retrieval that fixes pain #1 is `recall()`: sparse + dense + RRF. With
HashEmbedding the dense arm is weak but the pipeline is real; drop in bge-m3 and
precision jumps with zero changes above this file.

Char-cap consolidation (pain #3): `set_block` refuses to overflow a CORE block —
the caller (or a consolidation model) must merge/evict. That is the LLM-in-loop
GC that stops the 118-file balloon.
"""
from __future__ import annotations
import json
import sqlite3
import time

from .embeddings import EmbeddingProvider, make_embedding, cosine


# ``proposed`` claims are deliberately absent from normal recall.  A host must
# promote them after user attestation or independent verification; keeping the
# accepted set here (rather than sprinkling status checks through the query
# paths) makes that trust boundary auditable.
RECALLABLE_STATUSES = frozenset(("active", "attested", "verified"))
MEMORY_STATUSES = RECALLABLE_STATUSES | frozenset(("proposed", "rejected", "invalidated"))


def _now() -> int:
    # NOTE: time.time() is fine here; determinism handled by callers/tests.
    return int(time.time())


class BlockOverflow(Exception):
    """Raised when a CORE block write would exceed its char cap."""


class SqliteMemory:
    def __init__(self, path: str, embedder: EmbeddingProvider | None = None,
                 reranker=None, distiller=None):
        self.path = path
        # embedder=None -> BM25-only (dense arm disabled). This is the low-spec / offline default:
        # a REAL embedder (granite) is added when available, but we NEVER fall back to HashEmbedding —
        # measured on LOCOMO, hash-dense (0.346) is WORSE than pure BM25 (0.526): its bag-of-words
        # cosines inject noise into RRF and actively hurt recall. So no embedder => sparse-only.
        self.embedder = embedder
        self.embed_model = self.embedder.name if self.embedder else "bm25-only"
        self.reranker = reranker          # optional cross-encoder over the fused top-k
        self.distiller = distiller        # optional (text,keys)->clean fact str, write-time
        # check_same_thread=False: the ACP path builds the harness on the asyncio event-loop
        # thread but runs it (recall/remember) inside a run_in_executor worker thread — the default
        # same-thread guard would raise ProgrammingError on the first memory access and break every
        # ACP prompt. Access stays sequential (one run at a time), and each web request uses its own
        # connection, so relaxing the check introduces no real concurrent-write hazard.
        # timeout=30 + WAL + busy_timeout so CONCURRENT real-provider runs (two web tabs at once)
        # don't lose their answer to `database is locked`: every non-mock run WRITES memory at
        # consolidation, and the default busy_timeout=0 makes the loser raise immediately, which the
        # loop reports as res.error and the UI then discards the (already-computed) answer. Mirrors
        # recorder.py, which was given this treatment but memory was not.
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA busy_timeout=30000")
        except sqlite3.OperationalError:
            pass
        self.has_fts = True
        self._init_schema()

    # ------------------------------------------------------------------ #
    def _init_schema(self) -> None:
        c = self.db.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS blocks(
            id INTEGER PRIMARY KEY, scope TEXT, label TEXT,
            value TEXT, char_limit INTEGER, updated_at INTEGER,
            UNIQUE(scope, label))""")
        c.execute("""CREATE TABLE IF NOT EXISTS facts(
            id INTEGER PRIMARY KEY, project TEXT, text TEXT, keys TEXT,
            importance REAL DEFAULT 0.5, access_count INTEGER DEFAULT 0,
            last_access INTEGER, created_at INTEGER, superseded_by INTEGER,
            embed_model TEXT, embedding TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            source TEXT NOT NULL DEFAULT 'host', evidence TEXT NOT NULL DEFAULT '',
            provenance TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT '',
            review_source TEXT NOT NULL DEFAULT '',
            review_evidence TEXT NOT NULL DEFAULT '',
            review_provenance TEXT NOT NULL DEFAULT '', reviewed_at INTEGER)""")
        # In-place migration for every pre-claim memory.db.  Existing rows were
        # already eligible for recall, so changing them to ``proposed`` would be
        # a destructive trust downgrade.  They stay recallable and are marked
        # ``legacy`` so a future review UI can distinguish them from evidenced
        # claims.  The re-check handles two Collie processes racing this same
        # idempotent migration.
        fact_cols = {r[1] for r in c.execute("PRAGMA table_info(facts)")}
        for name, decl in (
                ("status", "TEXT NOT NULL DEFAULT 'active'"),
                ("source", "TEXT NOT NULL DEFAULT 'legacy'"),
                ("evidence", "TEXT NOT NULL DEFAULT ''"),
                ("provenance", "TEXT NOT NULL DEFAULT ''"),
                ("scope", "TEXT NOT NULL DEFAULT ''"),
                ("review_source", "TEXT NOT NULL DEFAULT ''"),
                ("review_evidence", "TEXT NOT NULL DEFAULT ''"),
                ("review_provenance", "TEXT NOT NULL DEFAULT ''"),
                ("reviewed_at", "INTEGER")):
            if name in fact_cols:
                continue
            try:
                c.execute("ALTER TABLE facts ADD COLUMN %s %s" % (name, decl))
            except sqlite3.OperationalError:
                current = {r[1] for r in c.execute("PRAGMA table_info(facts)")}
                if name not in current:
                    raise
            fact_cols.add(name)
        # ``scope`` is a claim's trust boundary.  For old rows the only scope
        # that existed was ``project``, so preserve that exact meaning.
        c.execute("UPDATE facts SET scope=project WHERE scope IS NULL OR scope='' ")
        # Older builds consolidated by project alone.  That could leave an
        # allowed-scope predecessor hidden behind a successor the caller is not
        # authorized to retrieve.  Repair those historical links once (and
        # harmlessly on every open); valid same-project/same-scope chains stay
        # intact.
        c.execute("""UPDATE facts SET superseded_by=NULL
                     WHERE superseded_by IS NOT NULL AND NOT EXISTS(
                         SELECT 1 FROM facts AS successor
                         WHERE successor.id=facts.superseded_by
                           AND COALESCE(successor.project,'')=COALESCE(facts.project,'')
                           AND COALESCE(successor.scope,'')=COALESCE(facts.scope,''))""")
        c.execute("""CREATE INDEX IF NOT EXISTS facts_recall_scope
                     ON facts(project,status,superseded_by)""")
        # ``facts_recall_scope`` predates claim scopes and cannot be changed in
        # place on existing databases.  Keep it for compatibility and add a
        # scope-aware index under a new name for every scoped read path below.
        c.execute("""CREATE INDEX IF NOT EXISTS facts_scope_recall_v2
                     ON facts(project,scope,status,superseded_by)""")
        try:
            c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS facts_fts
                USING fts5(text, keys, content='facts', content_rowid='id')""")
        except sqlite3.OperationalError:
            self.has_fts = False  # FTS5 not compiled in -> LIKE fallback
        self.db.commit()

    # ------------------------------------------------------------------ #
    #  CORE blocks
    # ------------------------------------------------------------------ #
    def set_block(self, scope: str, label: str, value: str, char_limit: int = 1500) -> None:
        if len(value) > char_limit:
            raise BlockOverflow(
                "block %s/%s: %d > %d chars — consolidate before writing"
                % (scope, label, len(value), char_limit))
        self.db.execute(
            """INSERT INTO blocks(scope,label,value,char_limit,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(scope,label) DO UPDATE SET
                 value=excluded.value, char_limit=excluded.char_limit,
                 updated_at=excluded.updated_at""",
            (scope, label, value, char_limit, _now()))
        self.db.commit()

    def core_blocks(self, scopes: list[str]) -> list[sqlite3.Row]:
        q = ",".join("?" * len(scopes))
        return self.db.execute(
            "SELECT * FROM blocks WHERE scope IN (%s) ORDER BY scope, label" % q,
            scopes).fetchall()

    # ------------------------------------------------------------------ #
    #  ARCHIVAL write
    # ------------------------------------------------------------------ #
    @staticmethod
    def _statuses(statuses=None) -> tuple[str, ...]:
        values = tuple(statuses or RECALLABLE_STATUSES)
        invalid = set(values) - MEMORY_STATUSES
        if invalid:
            raise ValueError("invalid memory status: %s" % ", ".join(sorted(invalid)))
        return values

    @staticmethod
    def _allowed_scopes(project: str, allowed_scopes=None) -> tuple[str, ...]:
        """Normalize the trust scopes a caller has explicitly been given.

        Historically ``project`` was the only boundary.  Migrated rows use it
        as their scope, while globally shared rows use ``global``; accepting
        those two scopes by default preserves that API without making any
        other scope in the same project implicitly readable.
        """
        if allowed_scopes is None:
            values = (str(project or "global"), "global")
        elif isinstance(allowed_scopes, str):
            values = (allowed_scopes,)
        else:
            values = tuple(allowed_scopes)
        # Stable de-duplication keeps SQL parameters deterministic.  Empty or
        # None scope names grant no authority rather than becoming wildcards.
        return tuple(dict.fromkeys(
            str(value) for value in values if value is not None and str(value)))

    @staticmethod
    def claim_boundary(project: str) -> dict[str, str]:
        """Return the physical project/scope used for a logical claim write."""
        value = str(project or "global")
        return {"project": value, "scope": value}

    def _nearest(self, vec, project: str, statuses=None, *, embed_model: str | None = None,
                 exclude_id: int | None = None, scope: str | None = None):
        """Nearest accepted fact inside one project *and* trust scope."""
        best_id, best = None, 0.0
        statuses = self._statuses(statuses)
        q = ",".join("?" * len(statuses))
        scope = str(scope or project)
        where = ["project=?", "scope=?", "superseded_by IS NULL",
                 "status IN (%s)" % q]
        params = [project, scope, *statuses]
        # Embeddings from different models are not in the same vector space.
        # Promotion can happen in a later process whose current embedder differs,
        # so match using the proposal's stored model, not self.embed_model.
        if embed_model is not None:
            where.append("embed_model=?")
            params.append(embed_model)
        if exclude_id is not None:
            where.append("id<>?")
            params.append(int(exclude_id))
        for r in self.db.execute(
                "SELECT id, embedding FROM facts WHERE " + " AND ".join(where),
                params).fetchall():
            try:
                s = cosine(vec, json.loads(r["embedding"]))
            except Exception:
                continue
            if s > best:
                best, best_id = s, r["id"]
        return best_id, best

    def remember(self, text: str, keys: str = "", project: str = "global",
                 importance: float = 0.5, consolidate: bool = True,
                 dedup_at: float = 0.93, created_at: int | None = None,
                 status: str = "active", source: str = "host", evidence: str = "",
                 provenance: str = "", scope: str | None = None) -> int:
        """Store a memory claim.

        Direct host callers retain the historic ``active`` default.  Model-facing
        tools must explicitly pass ``status='proposed'``; this split keeps old
        importers and verified consolidation compatible without allowing an
        agent assertion to silently become a durable fact.
        """
        if status not in MEMORY_STATUSES:
            raise ValueError("invalid memory status: %s" % status)
        source = str(source or "host")
        evidence = _metadata_text(evidence)
        provenance = _metadata_text(provenance)
        scope = str(scope or project)
        # EXTRACTION: distil noisy/raw input into a clean atomic fact before storing
        # (Mem0/A-MEM lesson — raw turns retrieve worse than distilled facts). Opt-in.
        if self.distiller:
            try:
                d = self.distiller(text, keys)
                if d is None:
                    return -1          # distiller judged it not worth storing (chit-chat)
                text = d
            except Exception:
                pass
        vec = self.embedder.embed(text + " " + keys, kind="passage") if self.embedder else []
        # A proposal must not supersede anything before review. If proposal B replaced A and B
        # were later rejected, a verified A would remain hidden behind the rejected row forever.
        # Accepted host writes may still consolidate within the accepted set.
        near_id, sim = self._nearest(
            vec, project, RECALLABLE_STATUSES, embed_model=self.embed_model,
            scope=scope) \
            if (consolidate and vec and status in RECALLABLE_STATUSES) else (None, 0.0)
        emb = json.dumps(vec)
        cur = self.db.execute(
            """INSERT INTO facts(project,text,keys,importance,access_count,
                 last_access,created_at,embed_model,embedding,status,source,
                 evidence,provenance,scope)
               VALUES(?,?,?,?,0,?,?,?,?,?,?,?,?,?)""",
            # created_at defaults to now; importers pass the SOURCE's original timestamp so
            # recency weighting sees when the fact was true, not when it was migrated.
            (project, text, keys, importance, _now(), int(created_at or _now()),
             self.embed_model, emb, status, source, evidence, provenance, scope))
        rid = cur.lastrowid
        # CONSOLIDATION: a near-identical prior fact is superseded by this one, so recall
        # doesn't accumulate duplicates (keeps the newer wording; supersession already
        # filters superseded rows out of _sparse/_dense).
        # HashEmbedding is bag-of-words: two DISTINCT facts with high token overlap ("deploy prod
        # Friday" vs "…Monday") score ~1.0 and would falsely supersede each other. Require
        # near-identical similarity before merging under a weak embedder.
        eff_dedup = max(dedup_at, 0.985) if str(self.embed_model).startswith("hash") else dedup_at
        if consolidate and near_id and sim >= eff_dedup:
            self.db.execute("UPDATE facts SET superseded_by=? WHERE id=?", (rid, near_id))
        if self.has_fts:
            self.db.execute("INSERT INTO facts_fts(rowid,text,keys) VALUES(?,?,?)",
                            (rid, text, keys))
        self.db.commit()
        return rid

    def propose(self, text: str, keys: str = "", project: str = "global",
                source: str = "agent", evidence: str = "", provenance: str = "",
                scope: str | None = None, **kwargs) -> int:
        """Create a non-recallable claim for later host review."""
        return self.remember(text, keys=keys, project=project, status="proposed",
                             source=source, evidence=evidence, provenance=provenance,
                             scope=scope, **kwargs)

    def promote(self, memory_id: int, status: str = "active", *, evidence=None,
                source=None, provenance=None, scope=None, review_source=None,
                review_provenance=None, reviewed_at: int | None = None,
                consolidate: bool = True, dedup_at: float = 0.93) -> bool:
        """Promote one proposal into a recallable state.

        Only a proposal can be promoted.  A rejected claim is terminal: a host
        that later receives better evidence should create a fresh proposal so
        the audit history remains unambiguous.
        """
        if status not in RECALLABLE_STATUSES:
            raise ValueError("promotion status must be active, attested, or verified")
        # ``source``/``provenance`` are accepted as ergonomic aliases for the
        # reviewer metadata, never as permission to rewrite who produced the
        # claim.  Producer provenance is immutable after INSERT.
        reviewer = review_source if review_source is not None else source
        reviewer_provenance = (review_provenance if review_provenance is not None
                               else provenance)
        changes = ["status=?", "review_source=?", "review_provenance=?",
                   "reviewed_at=?", "superseded_by=NULL"]
        params = [status, _metadata_text(reviewer or "host"),
                  _metadata_text(reviewer_provenance), int(reviewed_at or _now())]
        if evidence is not None:
            changes.append("review_evidence=?")
            params.append(_metadata_text(evidence))
        memory_id = int(memory_id)
        params.append(memory_id)
        try:
            # Serialize accepted-set selection and status transition.  No fact
            # is superseded unless this exact proposal successfully promotes in
            # the same transaction.
            self.db.execute("BEGIN IMMEDIATE")
            proposal = self.db.execute(
                """SELECT project,scope,embedding,embed_model FROM facts
                   WHERE id=? AND status='proposed'""", (memory_id,)).fetchone()
            if proposal is None:
                self.db.rollback()
                return False
            # ``scope`` remains in the public signature as a compatibility
            # assertion, but a reviewer cannot widen or rewrite the producer's
            # trust boundary.  A mismatch fails closed and leaves the proposal
            # pending for an explicitly authorized review path.
            if (scope is not None
                    and str(scope or proposal["project"]) != str(proposal["scope"])):
                self.db.rollback()
                return False
            near_id, sim = None, 0.0
            try:
                vec = json.loads(proposal["embedding"] or "[]")
            except Exception:
                vec = []
            if consolidate and vec:
                near_id, sim = self._nearest(
                    vec, proposal["project"], RECALLABLE_STATUSES,
                    embed_model=proposal["embed_model"], exclude_id=memory_id,
                    scope=proposal["scope"])
            cur = self.db.execute(
                "UPDATE facts SET %s WHERE id=? AND status='proposed'" % ", ".join(changes),
                params)
            if cur.rowcount != 1:
                self.db.rollback()
                return False
            model = str(proposal["embed_model"] or "")
            threshold = max(float(dedup_at), 0.985) if model.startswith("hash") \
                else float(dedup_at)
            if near_id and sim >= threshold:
                self.db.execute("UPDATE facts SET superseded_by=? WHERE id=?",
                                (memory_id, near_id))
            self.db.commit()
            return True
        except Exception:
            self.db.rollback()
            raise

    def reject(self, memory_id: int, *, evidence=None, source=None,
               provenance=None, review_source=None, review_provenance=None,
               reviewed_at: int | None = None) -> bool:
        """Reject one proposal without deleting its provenance/audit record."""
        reviewer = review_source if review_source is not None else source
        reviewer_provenance = (review_provenance if review_provenance is not None
                               else provenance)
        changes = ["status='rejected'", "review_source=?", "review_provenance=?",
                   "reviewed_at=?"]
        params = [_metadata_text(reviewer or "host"),
                  _metadata_text(reviewer_provenance), int(reviewed_at or _now())]
        if evidence is not None:
            changes.append("review_evidence=?")
            params.append(_metadata_text(evidence))
        params.append(int(memory_id))
        cur = self.db.execute(
            "UPDATE facts SET %s WHERE id=? AND status='proposed'" % ", ".join(changes),
            params)
        self.db.commit()
        return cur.rowcount == 1

    def invalidate(self, memory_id: int, *, evidence=None, source=None,
                   provenance=None, review_source=None, review_provenance=None,
                   reviewed_at: int | None = None) -> bool:
        """Remove an accepted claim from recall while retaining its audit record."""
        reviewer = review_source if review_source is not None else source
        reviewer_provenance = (review_provenance if review_provenance is not None
                               else provenance)
        changes = ["status='invalidated'", "review_source=?", "review_provenance=?",
                   "reviewed_at=?"]
        params = [_metadata_text(reviewer or "host"),
                  _metadata_text(reviewer_provenance), int(reviewed_at or _now())]
        if evidence is not None:
            changes.append("review_evidence=?")
            params.append(_metadata_text(evidence))
        params.append(int(memory_id))
        accepted = ",".join("?" * len(RECALLABLE_STATUSES))
        params.extend(tuple(RECALLABLE_STATUSES))
        cur = self.db.execute(
            "UPDATE facts SET %s WHERE id=? AND status IN (%s)" %
            (", ".join(changes), accepted), params)
        if cur.rowcount:
            # If an older accepted fact was consolidated under this now-invalid
            # row, revive it.  Invalidating the newest claim must not erase the
            # last known-good memory.
            self.db.execute("UPDATE facts SET superseded_by=NULL WHERE superseded_by=?",
                            (int(memory_id),))
        self.db.commit()
        return cur.rowcount == 1

    # Verbose aliases make the lifecycle API self-documenting for host layers;
    # the short forms above remain convenient for direct use and tests.
    promote_memory = promote
    reject_memory = reject
    invalidate_memory = invalidate

    def get_claim(self, memory_id: int) -> dict | None:
        row = self.db.execute(
            """SELECT id,project,text,keys,importance,created_at,superseded_by,
                      status,source,evidence,provenance,scope,review_source,
                      review_evidence,review_provenance,reviewed_at
               FROM facts WHERE id=?""", (int(memory_id),)).fetchone()
        return dict(row) if row else None

    def list_claims(self, status: str | None = None, project: str | None = None,
                    limit: int = 100, *, allowed_scopes=None) -> list[dict]:
        """Review surface for hosts; rejected/proposed claims stay out of recall."""
        where, params = [], []
        if status is not None:
            if status not in MEMORY_STATUSES:
                raise ValueError("invalid memory status: %s" % status)
            where.append("status=?")
            params.append(status)
        if project is not None:
            where.append("project=?")
            params.append(project)
        # An unscoped all-project listing is the existing local-admin review
        # surface (``collie mem pending``).  Once a project or explicit scope
        # capability is supplied, however, list obeys the same trust boundary
        # as recall.
        scopes = None
        if project is not None or allowed_scopes is not None:
            scopes = self._allowed_scopes(project or "global", allowed_scopes)
            if not scopes:
                return []
            where.append("scope IN (%s)" % ",".join("?" * len(scopes)))
            params.extend(scopes)
        sql = "SELECT * FROM facts"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(1000, int(limit))))
        return [dict(r) for r in self.db.execute(sql, params).fetchall()]

    def rebuild_fts(self) -> int:
        """Repopulate the FTS index from facts (recovery after index corruption or an
        index-time transform experiment; external-content table indexes what WE insert)."""
        if not self.has_fts:
            return 0
        self.db.execute("INSERT INTO facts_fts(facts_fts) VALUES('delete-all')")
        rows = self.db.execute("SELECT id,text,keys FROM facts").fetchall()
        for r in rows:
            self.db.execute("INSERT INTO facts_fts(rowid,text,keys) VALUES(?,?,?)",
                            (r["id"], r["text"] or "", r["keys"] or ""))
        self.db.commit()
        return len(rows)

    def reembed_all(self) -> int:
        """Re-embed every fact with the current embedder (after a model swap).
        Embeddings from different models live in different spaces, so a switch
        requires this pass; store `embed_model` so we know what's stale."""
        if self.embedder is None:                          # BM25-only: nothing to (re)embed
            return 0
        rows = self.db.execute("SELECT id, text, keys FROM facts").fetchall()
        for r in rows:
            emb = json.dumps(self.embedder.embed(
                (r["text"] or "") + " " + (r["keys"] or ""), kind="passage"))
            self.db.execute("UPDATE facts SET embedding=?, embed_model=? WHERE id=?",
                            (emb, self.embed_model, r["id"]))
        self.db.commit()
        return len(rows)

    def count(self, project: str | None = None) -> int:
        if project:
            return self.db.execute(
                "SELECT COUNT(*) FROM facts WHERE project=? AND superseded_by IS NULL",
                (project,)).fetchone()[0]
        return self.db.execute(
            "SELECT COUNT(*) FROM facts WHERE superseded_by IS NULL").fetchone()[0]

    # ------------------------------------------------------------------ #
    #  ARCHIVAL read — HYBRID retrieval (the pain-#1 fix)
    # ------------------------------------------------------------------ #
    def _sparse(self, query: str, project: str, limit: int,
                statuses=None, *, allowed_scopes=None) -> list[tuple[int, float]]:
        statuses = self._statuses(statuses)
        scopes = self._allowed_scopes(project, allowed_scopes)
        if not scopes:
            return []
        sq = ",".join("?" * len(statuses))
        scope_q = ",".join("?" * len(scopes))
        rows = []
        if self.has_fts:
            try:
                match = " OR ".join(_fts_terms(query)) or query
                rows = self.db.execute(
                    """SELECT f.id, bm25(facts_fts) AS score
                       FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
                       WHERE facts_fts MATCH ? AND (f.project=? OR f.project='global')
                             AND f.superseded_by IS NULL AND f.status IN (%s)
                             AND f.scope IN (%s)
                       ORDER BY score LIMIT ?""" % (sq, scope_q),
                    (match, project, *statuses, *scopes, limit)).fetchall()
                # bm25 lower == better; return as (id, rank_score) ascending handled by RRF
                return [(r["id"], -r["score"]) for r in rows]
            except sqlite3.OperationalError:
                pass
        # LIKE fallback: match ANY of the first few query tokens (not just the first word)
        toks = [t for t in query.strip().split() if len(t) > 2][:4] or [query.strip()]
        clause = " OR ".join(["text LIKE ? OR keys LIKE ?"] * len(toks))
        params = [project, *statuses, *scopes]
        for t in toks:
            params += ["%" + t + "%", "%" + t + "%"]
        params.append(limit)
        rows = self.db.execute(
            "SELECT id FROM facts WHERE (project=? OR project='global') AND status IN (%s) "
            "AND superseded_by IS NULL AND scope IN (%s) "
            "AND (%s) LIMIT ?" % (sq, scope_q, clause), params).fetchall()
        return [(r["id"], 1.0) for r in rows]

    def _dense(self, query: str, project: str, limit: int,
               statuses=None, *, allowed_scopes=None) -> list[tuple[int, float]]:
        if self.embedder is None:                          # BM25-only mode — no dense arm
            return []
        statuses = self._statuses(statuses)
        scopes = self._allowed_scopes(project, allowed_scopes)
        if not scopes:
            return []
        sq = ",".join("?" * len(statuses))
        scope_q = ",".join("?" * len(scopes))
        qv = self.embedder.embed(query, kind="query")
        rows = self.db.execute(
            "SELECT id, embedding FROM facts WHERE (project=? OR project='global') "
            "AND superseded_by IS NULL AND status IN (%s) AND scope IN (%s)" %
            (sq, scope_q), (project, *statuses, *scopes)).fetchall()
        # HashEmbedding (bag-of-words) produces spurious positive cosines on token overlap, so we
        # abstain on non-positive for it; a REAL semantic embedder's weakly-related passage (cosine
        # near 0) is genuine signal — keep it so cross-lingual/paraphrase matches enter RRF.
        floor = 0.0 if str(self.embed_model).startswith("hash") else -1.0
        scored = []
        for r in rows:
            try:
                s = cosine(qv, json.loads(r["embedding"]))
                if s > floor:
                    scored.append((r["id"], s))
            except Exception:
                continue
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def recall(self, query: str, project: str = "global", k: int = 8,
               pool: int = 50, statuses=None, *, allowed_scopes=None) -> list[dict]:
        """Hybrid: BM25 + dense cosine, fused with Reciprocal Rank Fusion."""
        project = str(project or "global")
        statuses = self._statuses(statuses)
        scopes = self._allowed_scopes(project, allowed_scopes)
        if not scopes:
            return []
        sparse = self._sparse(
            query, project, pool, statuses, allowed_scopes=scopes)
        dense = self._dense(
            query, project, pool, statuses, allowed_scopes=scopes)
        fused = rrf([[i for i, _ in sparse], [i for i, _ in dense]], k=60)
        # With a reranker, fuse to a LARGER candidate pool and let the cross-encoder pick
        # the final top-k (it scores query+doc jointly — sharper than RRF's rank fusion).
        cand = fused[: max(k, 24)]                       # headroom for rerank/recency re-ordering; A/B'd 2026-07-17: pools of 36/50 LOWER strict@10 (59%/55% vs 62%) — deeper candidates only feed the cross-encoder topically-close-but-answerless distractors
        if not cand:
            return []
        q = ",".join("?" * len(cand))
        sq = ",".join("?" * len(statuses))
        scope_q = ",".join("?" * len(scopes))
        rows = self.db.execute(
            """SELECT id,text,keys,importance,created_at,status,source,evidence,
                      provenance,scope,review_source,review_evidence,
                      review_provenance,reviewed_at FROM facts WHERE id IN (%s)
                      AND (project=? OR project='global')
                      AND superseded_by IS NULL AND status IN (%s)
                      AND scope IN (%s)""" % (q, sq, scope_q),
            [i for i, _ in cand] + [project, *statuses, *scopes]).fetchall()
        by_id = {r["id"]: r for r in rows}

        ranked = cand                                    # default: RRF order
        if self.reranker:
            ids = [rid for rid, _ in cand if rid in by_id]
            docs = [(by_id[rid]["text"] or "") + " " + (by_id[rid]["keys"] or "")
                    for rid in ids]
            try:
                scores = self.reranker.rerank(query, docs)
                ranked = sorted(zip(ids, scores), key=lambda x: x[1], reverse=True)
            except Exception:
                pass                                     # cross-encoder failed -> keep RRF

        # RECENCY: newer facts are more likely to describe the CURRENT state (ports move,
        # decisions get reversed), so give them a mild multiplicative edge — relevance still
        # dominates. Scores are rebuilt on the rank scale (1/(60+pos)) so the same rule works
        # over RRF and cross-encoder output alike; a uniform-age corpus (evals) multiplies
        # every row by the same factor and keeps its order. Half-life in days via Settings
        # (RECENCY_HALFLIFE, default 90); 0 disables.
        try:
            from . import settings as _settings
            half = float(_settings.get("RECENCY_HALFLIFE", "90") or 0)
        except Exception:
            half = 90.0
        if half > 0:
            now = _now()
            rescored = []
            for pos, (rid, _s) in enumerate(ranked):
                r = by_id.get(rid)
                age_days = max(0, now - ((r["created_at"] if r else None) or now)) / 86400.0
                boost = 1.0 + 0.5 * (0.5 ** (age_days / half))
                # Multiply the ACTUAL fused/reranker relevance score by the recency boost (≤1.5x), so
                # relevance keeps dominating and margins are preserved. Rebuilding on pure rank position
                # (1/(60+pos)) threw away the relevance gaps, letting a fresh low-relevance distractor
                # leapfrog the true top hit — and, since top-k truncation runs after this, evict it.
                rescored.append((rid, float(_s) * boost))
            ranked = sorted(rescored, key=lambda x: x[1], reverse=True)
        ranked = ranked[:k]

        out = []
        for rid, score in ranked:
            r = by_id.get(rid)
            if r:
                out.append({"id": rid, "text": r["text"], "keys": r["keys"],
                            "score": round(float(score), 4), "status": r["status"],
                            "source": r["source"], "evidence": r["evidence"],
                            "provenance": r["provenance"], "scope": r["scope"],
                            "review_source": r["review_source"],
                            "review_evidence": r["review_evidence"],
                            "review_provenance": r["review_provenance"],
                            "reviewed_at": r["reviewed_at"]})
                self.db.execute(
                    "UPDATE facts SET access_count=access_count+1, last_access=? WHERE id=?",
                    (_now(), rid))
        self.db.commit()
        return out

    def close(self) -> None:
        self.db.close()


def _metadata_text(value) -> str:
    """Persist host metadata as deterministic text while accepting JSON-shaped values."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _fts_terms(query: str) -> list[str]:
    """Sanitize a free-text query into safe FTS5 terms (avoids syntax errors).
    NOTE unicode61 keeps a CJK run as ONE token, so the sparse leg only matches Chinese
    on identical runs — a bigram index+query expansion was A/B'd 2026-07-17 and did NOT
    help (strict@10 59% vs 62% baseline on 29 real queries; the dense leg already carries
    Chinese, extra bigram candidates only displaced strict hits). Revisit only if
    Chinese-keyword misses show up in practice."""
    import re
    toks = re.findall(r"[A-Za-z0-9_]+|[一-鿿]+", query)
    return ['"%s"' % t for t in toks if len(t) > 1][:12]


def rrf(rank_lists: list[list[int]], k: int = 60) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion. rank_lists = list of id-lists ordered best-first."""
    scores: dict[int, float] = {}
    for lst in rank_lists:
        for rank, rid in enumerate(lst):
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: x[1], reverse=True)
