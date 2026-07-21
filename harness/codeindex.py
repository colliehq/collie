"""Semantic code navigation — echomem-style, over a repo's source files.

Fixes collie's real-repo failure mode (25 turns grepping blind, empty patch). On
first use in a repo it walks source files, chunks them, BATCH-embeds every chunk
with collie's local embedder (seconds, not minutes), and keeps an in-memory hybrid
index. `code_search(query)` returns the top `path:line` snippets for a natural-
language query so the agent locates *where* to edit before reading/editing.
"""
import array
import hashlib
import math
import os
import pickle
import re
import sqlite3

from .tools import Tool
from .embeddings import make_embedding, cosine, tokenize

SRC_EXT = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
           ".go", ".java", ".rb", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".php", ".cs",
           ".kt", ".swift", ".scala", ".lua",
           # config files a fix sometimes must touch (pylint-4661 needs setup.cfg — it was
           # structurally invisible to code_search before). Few per repo; low noise.
           ".cfg", ".ini", ".toml"}
SKIP_DIR = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
            ".tox", ".mypy_cache", ".pytest_cache", "site-packages", ".idea", ".vscode",
            "tests", "test", "testing"}   # skip tests (never the fix target); keep docs/examples


def _is_test_file(fn):
    return fn.startswith("test_") or fn.endswith(("_test.py", "_tests.py", ".test.js"))


def _iter_files(root, max_bytes=200_000):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIR and not d.startswith(".")]
        for fn in fns:
            if os.path.splitext(fn)[1] in SRC_EXT and not _is_test_file(fn):
                p = os.path.join(dp, fn)
                # Skip symlinks: an untrusted repo could point a source-looking name at a host file
                # (e.g. secrets.py -> ~/.ssh/id_rsa) and pull its contents into the searchable index.
                # os.walk already does not descend symlinked DIRS (followlinks=False); this covers files.
                if os.path.islink(p):
                    continue
                try:
                    if os.path.getsize(p) <= max_bytes:
                        yield p
                except OSError:
                    pass


def _code_density(text):
    """Fraction of non-blank lines that look like CODE (not docstring/comment/prose).
    Demotes module-header docstrings (e.g. a package re-export file) that embed near
    every query yet contain nothing to edit — the objects.py:1-46 domination on seaborn."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return 0.0
    prose = 0
    for l in lines:
        # Only clear DOC/COMMENT markers. Dropped "*"/"|"/"= "/"- ": they demote real
        # code (C `*ptr`, Rust `|x|` closures, assignments) as if it were prose.
        if (l.startswith(("#", "//", '"""', "'''", ":param", ":return", ":rtype", ">>>"))
                or l in ('"""', "'''")):
            prose += 1
    return 1.0 - prose / len(lines)


# Structure-aware chunking: start a NEW chunk at each def/class/function boundary so a semantic
# unit (a function/method + its body) stays whole. The old blind 50-line window split a function's
# signature from its body and averaged ~10 unrelated small functions into one mush vector; measured
# on rebench 2026_03 (n=20, gold-file localization) this boundary chunker lifted bge recall@5 65%->75%
# at ZERO added cost (same model). Files with no def/class structure (config/data/prose) fall back to
# the line window so a structureless 2000-line file isn't one giant chunk.
_DEF_RE = re.compile(r'^\s{0,8}(async\s+def |def |class |function |func\s|public |private |protected |'
                     r'export (default )?(async )?function |impl |fn |module |type )')


def _chunks(text, win=50, step=40, maxlines=90):
    lines = text.splitlines()
    if not any(_DEF_RE.match(l) for l in lines):          # structureless -> original line window
        for i in range(0, max(1, len(lines)), step):
            seg = lines[i:i + win]
            if any(s.strip() for s in seg):
                yield (i + 1, i + len(seg), "\n".join(seg))
        return
    cur, start = [], 0                                    # structure-aware -> one unit per chunk
    for i, ln in enumerate(lines):
        if _DEF_RE.match(ln) and len(cur) >= 3 and any(s.strip() for s in cur):
            yield (start + 1, i, "\n".join(cur)); cur, start = [], i
        cur.append(ln)
        if len(cur) >= maxlines:                          # bound a giant function
            yield (start + 1, i + 1, "\n".join(cur)); cur, start = [], i + 1
    if any(s.strip() for s in cur):
        yield (start + 1, len(lines), "\n".join(cur))


# A FAST small embedder for bulk indexing — jina-v3 (~950ms/embed) is far too slow
# to index a whole repo; bge-small (~15ms) is plenty for navigation.
FAST_CODE_EMBED = "local:BAAI/bge-small-en-v1.5"
_CS_LEAN = os.environ.get("COLLIE_CS_LEAN", "0") in ("1", "true", "on")   # code_search returns locations not bodies


def _fast_embedder():
    # Cap the ONNX intra-op pool. Measured (bge-small, 3000 chunks): 8 threads = 98s, ALL 28 cores
    # = 110s — past ~8 threads it is SLOWER (thread-coordination + memory-bandwidth saturation) while
    # burning 3.5x the cores. So default to <=8 unless the operator overrode COLLIE_EMBED_THREADS:
    # strictly faster AND lighter, zero downside. (fastembed ignores OMP_NUM_THREADS; this env is the
    # only lever — see FastEmbedProvider.)
    os.environ.setdefault("COLLIE_EMBED_THREADS", str(min(8, os.cpu_count() or 8)))
    try:
        return make_embedding(FAST_CODE_EMBED)
    except Exception:
        return make_embedding("hash")               # offline fallback


# ---- file-hash embedding cache (COLLIE_EMBED_CACHE=1) -----------------------------------------
class _EmbedCache:
    """(model, file-content-sha) -> that file's chunk vectors. Content-hash keying self-invalidates
    (changed file = new key = miss), so there is no stale-read risk; the model tag namespaces it so
    swapping the embedder never mixes vector spaces."""
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS f(model TEXT, fh TEXT, data BLOB, "
                        "PRIMARY KEY(model, fh))")
        self.db.commit()

    def get(self, model, fh):
        row = self.db.execute("SELECT data FROM f WHERE model=? AND fh=?", (model, fh)).fetchone()
        return _unpack(row[0]) if row else None

    def put(self, model, fh, lst):
        try:
            self.db.execute("INSERT OR REPLACE INTO f(model, fh, data) VALUES(?,?,?)",
                            (model, fh, _pack(lst)))
            self.db.commit()
        except sqlite3.Error:
            pass


def _pack(lst):                          # [(s,e,t,vec)] -> bytes (f32 vecs packed compactly)
    meta = [(s, e, t) for (s, e, t, v) in lst]
    dim = len(lst[0][3]) if lst else 0
    blob = b"".join(array.array("f", v).tobytes() for (s, e, t, v) in lst)
    return pickle.dumps((meta, dim, blob), protocol=4)


def _unpack(b):
    # SECURITY: pickle.loads executes arbitrary code on a malicious payload. This only ever reads the
    # on-disk embedding cache at ~/.collie/code_embed_cache.db, which must stay USER-OWNED and not be
    # shared/writable by other users — never feed untrusted bytes here. Format left as-is on purpose.
    meta, dim, blob = pickle.loads(b)
    out, w = [], dim * 4
    for i, (s, e, t) in enumerate(meta):
        out.append((s, e, t, list(array.array("f", blob[i * w:(i + 1) * w]))))
    return out


_CACHE = None


def _embed_cache():
    global _CACHE
    if _CACHE is None:
        path = os.path.expanduser("~/.collie/code_embed_cache.db")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _CACHE = _EmbedCache(path)
    return _CACHE


class CodeIndex:
    def __init__(self, root, embedder=None, cap_chunks=3000, reranker=None, model_tag=None):
        self.root = root
        self.embedder = embedder or _fast_embedder()
        self.cap = cap_chunks
        self.reranker = reranker           # optional cross-encoder 2nd stage (the proven win)
        self.model_tag = model_tag or FAST_CODE_EMBED   # cache namespace: which embedder produced the vecs
        self.chunks, self.vecs, self.built = [], [], False

    def build(self):
        # Cache ON by default (COLLIE_EMBED_CACHE=0 to disable) — tested vector-identical to the
        # uncached path, so retrieval results are unchanged; it only skips re-embedding files it has
        # already seen (re-run / same-repo-other-commit ~free).
        if os.environ.get("COLLIE_EMBED_CACHE", "1") not in ("0", "false", "off"):
            return self._build_cached()
        files = list(_iter_files(self.root))
        # per-file budget so a global cap can't zero out whole files late in os.walk order
        # (that made "find where to edit" a coin-flip on walk order). Every file gets a
        # fair share; big files are sampled rather than dropped.
        # per_file = cap // files (min 1) so per_file * files <= cap: EVERY file contributes and the
        # total stays under cap. The old max(4, …) forced ≥4/file, so cap*4-worth overflowed the cap
        # and the global break zeroed out whole files late in walk order (the bug this comment warns
        # about). The len(recs)>=cap break is now only a hard memory ceiling (reachable when
        # files > cap — more source files than the whole chunk budget).
        per_file = max(1, self.cap // max(1, len(files)))
        recs = []
        for p in files:
            try:
                text = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            rel = os.path.relpath(p, self.root)
            n = 0
            for s, e, t in _chunks(text):
                recs.append({"path": rel, "start": s, "end": e, "text": t,
                             "dens": _code_density(t)})
                n += 1
                if n >= per_file:
                    break
            if len(recs) >= self.cap:
                break
        self.chunks = recs
        self.vecs = self.embedder.embed_batch([r["text"] for r in recs], "passage") if recs else []
        self.built = True
        return len(recs)

    # ---- cached build (COLLIE_EMBED_CACHE=1) ---------------------------------------------------
    # Embedding a file is a pure function of (model, file-content). Cache each unique file's chunk
    # vectors keyed by (model, sha256(content)); a re-run of the same instance (v1->v2->v3, best-of-k)
    # or the same repo at another commit re-embeds only the files that actually CHANGED. Measured
    # cost: ~41ms/chunk, ~100s/repo uncached -> ~0 on a cache hit. Batching is preserved: all
    # cache-MISS files are embedded in one embed_batch call. Disk is trivial (384-d f32 = 1.5KB/chunk;
    # a full rebench pass ~180-360MB). The per-file cap bounds a giant file; the global cap + per_file
    # sampling stay at ASSEMBLY so cross-repo file reuse is unaffected by repo size.
    def _build_cached(self, per_file_cache_cap=60):
        cache = _embed_cache()
        files = list(_iter_files(self.root))
        per_file = max(1, self.cap // max(1, len(files)))
        by_file = {}                       # rel -> [(start,end,text,vec), ...]  (full, capped per_file_cache_cap)
        miss = []                          # (rel, start, end, text) needing embedding
        for p in files:
            try:
                text = open(p, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            rel = os.path.relpath(p, self.root)
            fh = hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()
            hit = cache.get(self.model_tag, fh)
            if hit is not None:
                by_file[rel] = hit
                continue
            by_file[rel] = ("MISS", fh, [])   # placeholder; fill after batch embed
            n = 0
            for s, e, t in _chunks(text):
                miss.append((rel, s, e, t)); n += 1
                if n >= per_file_cache_cap:
                    break
        if miss:
            vecs = self.embedder.embed_batch([m[3] for m in miss], "passage")
            fill = {}
            for (rel, s, e, t), v in zip(miss, vecs):
                fill.setdefault(rel, []).append((s, e, t, v))
            for rel, lst in fill.items():
                fh = by_file[rel][1]
                cache.put(self.model_tag, fh, lst)
                by_file[rel] = lst
        # any placeholder left with no chunks (empty file) -> empty list
        for rel, v in list(by_file.items()):
            if isinstance(v, tuple):
                by_file[rel] = v[2]
        # assemble: per_file sampling + global cap (same searchable-set semantics as uncached)
        recs, vecs = [], []
        for rel, lst in by_file.items():
            for (s, e, t, v) in lst[:per_file]:
                recs.append({"path": rel, "start": s, "end": e, "text": t, "dens": _code_density(t)})
                vecs.append(v)
                if len(recs) >= self.cap:
                    break
            if len(recs) >= self.cap:
                break
        self.chunks, self.vecs, self.built = recs, vecs, True
        return len(recs)

    def search(self, query, k=6):
        if not self.built:
            self.build()
        if not self.chunks:
            return []
        qv = self.embedder.embed(query, kind="query")
        qtok = set(tokenize(query))
        scored = []
        for i, r in enumerate(self.chunks):
            dense = cosine(qv, self.vecs[i])
            kw = len(qtok & set(tokenize(r["text"]))) / (len(qtok) or 1)   # keyword overlap
            # demote prose/docstring-only chunks (a header docstring embeds near every
            # query but has nothing to edit) — scale relevance by code density.
            dens = r.get("dens", 1.0)
            scored.append(((0.7 * dense + 0.3 * kw) * (0.45 + 0.55 * dens), i))
        scored.sort(reverse=True)
        pool = scored[: max(k * 3, 30)]        # room to dedup overlapping same-file windows below
        # 2nd stage: cross-encoder re-scores (query, chunk) JOINTLY over the bi-encoder's
        # candidate pool. This is the highest-ROI retrieval upgrade the memory research found,
        # and code_search is the one spot that lacked it. Keep the density demotion so a
        # header docstring the cross-encoder likes can't out-rank real code to edit.
        if self.reranker and len(pool) > 1:
            try:
                docs = [self.chunks[i]["text"][:1000] for _, i in pool]
                rr = self.reranker.rerank(query, docs)
                adj = [1.0 / (1.0 + math.exp(-s)) * (0.45 + 0.55 * self.chunks[pool[j][1]]["dens"])
                       for j, s in enumerate(rr)]
                pool = [pool[j] for j in sorted(range(len(pool)), key=lambda j: adj[j], reverse=True)]
            except Exception:
                pass
        out, seen = [], set()
        for _, i in pool:                      # one hit per FILE — win=50/step=40 windows overlap,
            r = self.chunks[i]                 # so without this two near-duplicate spans of the same
            if r["path"] in seen:              # file would crowd real other-file matches out of top-k.
                continue
            seen.add(r["path"])
            if _CS_LEAN:
                # LEAN result (COLLIE_CS_LEAN=1): return the LOCATION + the signature line only, not
                # the 400-char body. The controlled dig showed code_search's body-dumping is why
                # embedding burns ~1.7x tokens vs grep (bodies persist in context across every turn).
                # Returning locations makes code_search a cheap "semantic grep" — the agent read_files
                # the ones it wants, controlling how much code enters context (exactly what makes grep
                # token-efficient). Signature = first non-blank line (func-chunking puts the def/class
                # there), so the agent still sees WHAT each hit is.
                sig = next((ln.strip() for ln in r["text"].splitlines() if ln.strip()), "")
                out.append("%s:%d-%d  %s" % (r["path"], r["start"], r["end"], sig[:120]))
            else:
                out.append("%s:%d-%d\n%s" % (r["path"], r["start"], r["end"], r["text"][:400]))
            if len(out) >= k:
                break
        return out


    def related(self, query_text, edited_path, exclude_paths, k=4):
        """Surface OTHER files that a multi-file fix likely also needs. Key insight from
        pylint-4551: the sibling files (inspector/utils/writer) live in the SAME package
        as the edit (pyreverse/) and COLLABORATE — they are not the files most *topically*
        similar to the change (those were unrelated checkers about "type hints"). So rank
        by embedding similarity but strongly boost same-directory siblings (structure >
        topic for coverage). One hit per file; never an already-edited file."""
        if not self.built:
            self.build()
        if not self.chunks:
            return []
        qv = self.embedder.embed(query_text[:2000], kind="query")
        excl = set(exclude_paths)
        edir = os.path.dirname(edited_path or "")
        scored = []
        for i, r in enumerate(self.chunks):
            if r["path"] in excl:
                continue
            sim = cosine(qv, self.vecs[i]) * (0.45 + 0.55 * r.get("dens", 1.0))
            if os.path.dirname(r["path"]) == edir:   # same package (incl. repo-root, dir="") -> collaborators
                sim *= 3.0
            scored.append((sim, i))
        scored.sort(reverse=True)
        out, seen = [], set()
        for _, i in scored:
            r = self.chunks[i]
            if r["path"] in seen:
                continue
            seen.add(r["path"])
            out.append("%s:%d-%d" % (r["path"], r["start"], r["end"]))
            if len(out) >= k:
                break
        return out


    def related_scored(self, query_text, edited_path, exclude_paths, k=8, min_score=0.0):
        """Like related() but returns [(path:line, score), ...], one per file, only ABOVE
        min_score. Used by the coverage-gated finish: keep re-surfacing high-confidence
        same-package siblings until edited_files covers them (or they drop below threshold).
        Same scoring as related() (embedding sim x density, x3 same-directory boost)."""
        if not self.built:
            self.build()
        if not self.chunks:
            return []
        qv = self.embedder.embed(query_text[:2000], kind="query")
        excl = set(exclude_paths)
        edir = os.path.dirname(edited_path or "")
        scored = []
        for i, r in enumerate(self.chunks):
            if r["path"] in excl:
                continue
            sim = cosine(qv, self.vecs[i]) * (0.45 + 0.55 * r.get("dens", 1.0))
            if os.path.dirname(r["path"]) == edir:   # same package (incl. repo-root, dir="")
                sim *= 3.0
            scored.append((sim, i))
        scored.sort(reverse=True)
        out, seen = [], set()
        for sc, i in scored:
            if sc < min_score:                # sorted desc: nothing else qualifies
                break
            r = self.chunks[i]
            if r["path"] in seen:
                continue
            seen.add(r["path"])
            out.append(("%s:%d-%d" % (r["path"], r["start"], r["end"]), round(sc, 3)))
            if len(out) >= k:
                break
        return out


_INDEX = {}   # per-repo-root, lazy + cached within a process


def get_index(root, embedder=None):
    if root not in _INDEX:
        _INDEX[root] = CodeIndex(root, embedder)
    return _INDEX[root]


def invalidate(root=None):
    """Drop the cached index so the next code_search/related rebuilds against current file
    contents. Called by the edit/write tools — otherwise the process-global index keeps serving
    PRE-EDIT line numbers and snippets (and never sees newly-created files) for the whole run."""
    if root is None:
        _INDEX.clear()
    else:
        _INDEX.pop(root, None)


def related_locations(root, edited_text, edited_path, exclude_paths, k=4):
    """Embedding-driven multi-file coverage: sibling spots that may need the same change."""
    try:
        return get_index(root).related(edited_text, edited_path, exclude_paths, k)
    except Exception:
        return []


def related_scored(root, edited_text, edited_path, exclude_paths, k=8, min_score=0.0):
    """[(path:line, score), ...] siblings above min_score — for the coverage-gated finish."""
    try:
        return get_index(root).related_scored(edited_text, edited_path, exclude_paths, k, min_score)
    except Exception:
        return []


class CodeSearchTool(Tool):
    name, tier = "code_search", "always"
    description = ("Semantically locate WHERE in the repo to look or edit. Returns "
                   "the top path:line code snippets for a natural-language query "
                   "(e.g. 'where a Blueprint name is validated'). Use this FIRST to "
                   "find the right file before reading/editing. Args: query, optional k.")
    schema = {"type": "object", "properties": {
        "query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]}

    def run(self, args, ctx):
        try:
            idx = get_index(ctx.cwd)                 # uses the fast code embedder, not jina
            hits = idx.search(args["query"], k=int(args.get("k", 6)))
            return "\n\n".join(hits) or "(no code matches)"
        except Exception as e:
            return "ERROR(code_search): %s" % e


def register_code_search(registry):
    registry.register(CodeSearchTool())
    return True
