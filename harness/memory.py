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
import os
import sqlite3
import time

from .embeddings import EmbeddingProvider, make_embedding, cosine


def _now() -> int:
    # NOTE: time.time() is fine here; determinism handled by callers/tests.
    return int(time.time())


def project_scope(cwd: str = "") -> str:
    """The memory scope for a working directory: the CODEBASE, not the surface it was reached from.

    Every entry point used to name this after itself — the web app passed "web", every argparse
    default passed "demo" — so one machine, one checkout and one dog kept two memories that could
    not see each other, divided by which window the person happened to type into. Nothing about a
    project changes when you move from a chat panel to Slack, so nothing about its memory should:
    what was learned answering in one place is exactly what is missing in the other.

    A git checkout is scoped by its ROOT, so a subdirectory is the same project as the repo above
    it. Outside a checkout the directory itself is the project.

    Deliberate limitation: the key is the basename, so two checkouts of one repo (a fork beside its
    upstream) share a memory. That is usually what is wanted — they are the same project — and
    `--project` overrides it when it is not.
    """
    start = os.path.abspath(cwd or os.getcwd())
    root = start
    while True:
        if os.path.exists(os.path.join(root, ".git")):
            break
        parent = os.path.dirname(root)
        if parent == root:                  # walked to the filesystem root: not a checkout
            root = start
            break
        root = parent
    # never "global": that is the read-by-everyone tier, and a scope that fell back into it would
    # quietly publish one project's facts to every other.
    return os.path.basename(root).lower() or "default"


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
            embed_model TEXT, embedding TEXT)""")
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
    def _nearest(self, vec, project: str):
        """(id, cosine) of the most similar non-superseded fact in the project, or (None, 0)."""
        best_id, best = None, 0.0
        for r in self.db.execute(
                "SELECT id, embedding FROM facts WHERE project=? AND superseded_by IS NULL",
                (project,)).fetchall():
            try:
                s = cosine(vec, json.loads(r["embedding"]))
            except Exception:
                continue
            if s > best:
                best, best_id = s, r["id"]
        return best_id, best

    def remember(self, text: str, keys: str = "", project: str = "global",
                 importance: float = 0.5, consolidate: bool = True,
                 dedup_at: float = 0.93, created_at: int | None = None) -> int:
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
        near_id, sim = self._nearest(vec, project) if (consolidate and vec) else (None, 0.0)
        emb = json.dumps(vec)
        cur = self.db.execute(
            """INSERT INTO facts(project,text,keys,importance,access_count,
                 last_access,created_at,embed_model,embedding)
               VALUES(?,?,?,?,0,?,?,?,?)""",
            # created_at defaults to now; importers pass the SOURCE's original timestamp so
            # recency weighting sees when the fact was true, not when it was migrated.
            (project, text, keys, importance, _now(), int(created_at or _now()),
             self.embed_model, emb))
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
    def _sparse(self, query: str, project: str, limit: int) -> list[tuple[int, float]]:
        rows = []
        if self.has_fts:
            try:
                match = " OR ".join(_fts_terms(query)) or query
                rows = self.db.execute(
                    """SELECT f.id, bm25(facts_fts) AS score
                       FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid
                       WHERE facts_fts MATCH ? AND (f.project=? OR f.project='global')
                             AND f.superseded_by IS NULL
                       ORDER BY score LIMIT ?""",
                    (match, project, limit)).fetchall()
                # bm25 lower == better; return as (id, rank_score) ascending handled by RRF
                return [(r["id"], -r["score"]) for r in rows]
            except sqlite3.OperationalError:
                pass
        # LIKE fallback: match ANY of the first few query tokens (not just the first word)
        toks = [t for t in query.strip().split() if len(t) > 2][:4] or [query.strip()]
        clause = " OR ".join(["text LIKE ? OR keys LIKE ?"] * len(toks))
        params = [project]
        for t in toks:
            params += ["%" + t + "%", "%" + t + "%"]
        params.append(limit)
        rows = self.db.execute(
            "SELECT id FROM facts WHERE (project=? OR project='global') AND superseded_by IS NULL "
            "AND (%s) LIMIT ?" % clause, params).fetchall()
        return [(r["id"], 1.0) for r in rows]

    def _dense(self, query: str, project: str, limit: int) -> list[tuple[int, float]]:
        if self.embedder is None:                          # BM25-only mode — no dense arm
            return []
        qv = self.embedder.embed(query, kind="query")
        rows = self.db.execute(
            "SELECT id, embedding FROM facts WHERE (project=? OR project='global') "
            "AND superseded_by IS NULL", (project,)).fetchall()
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
               pool: int = 50) -> list[dict]:
        """Hybrid: BM25 + dense cosine, fused with Reciprocal Rank Fusion."""
        sparse = self._sparse(query, project, pool)
        dense = self._dense(query, project, pool)
        fused = rrf([[i for i, _ in sparse], [i for i, _ in dense]], k=60)
        # With a reranker, fuse to a LARGER candidate pool and let the cross-encoder pick
        # the final top-k (it scores query+doc jointly — sharper than RRF's rank fusion).
        cand = fused[: max(k, 24)]                       # headroom for rerank/recency re-ordering; A/B'd 2026-07-17: pools of 36/50 LOWER strict@10 (59%/55% vs 62%) — deeper candidates only feed the cross-encoder topically-close-but-answerless distractors
        if not cand:
            return []
        q = ",".join("?" * len(cand))
        rows = self.db.execute(
            "SELECT id,text,keys,importance,created_at FROM facts WHERE id IN (%s)" % q,
            [i for i, _ in cand]).fetchall()
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
                            "score": round(float(score), 4)})
                self.db.execute(
                    "UPDATE facts SET access_count=access_count+1, last_access=? WHERE id=?",
                    (_now(), rid))
        self.db.commit()
        return out

    def close(self) -> None:
        self.db.close()


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
