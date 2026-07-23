"""Embedding seam — the "internalized embedding" the design calls for.

v1 ships HashEmbedding: a deterministic, $0, dependency-free bag-of-words hashing
embedding. It is NOT semantically strong, but it makes the ENTIRE hybrid-retrieval
pipeline (dense cosine + BM25 + RRF) run end-to-end today so the plumbing is real
and testable. Swapping in a real local model (bge-m3 / fastembed) is a one-class
change behind this interface — nothing above it changes.

    class LocalEmbedding(EmbeddingProvider):        # future
        dim = 1024
        def __init__(self): self.m = TextEmbedding("BAAI/bge-m3")   # fastembed, local, $0
        def embed(self, text): return next(self.m.embed([text])).tolist()

Because embedding is in-process (not a 6.5s network hop to a hosted service), the
harness can afford to AUTO-PREFETCH memory every turn instead of waiting for the
model to decide to search — see context.ContextComposer.
"""
from __future__ import annotations
import hashlib
import math
import os
import re

from . import plat

_TOKEN_RE = re.compile(r"[a-z0-9_]+|[一-鿿]")


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


_DIM_WARNED = False


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:                       # empty vec (zero-text) → no signal, legitimately 0
        return 0.0
    if len(a) != len(b):
        # a genuine dimension mismatch means write-time and query-time used DIFFERENT embedders
        # (e.g. daemon died → in-process fallback of a different model). Every dense score then
        # silently collapses to 0 and ranking degrades to keyword-only with no error. Don't raise
        # (that kills retrieval), but warn ONCE so the misconfiguration is visible.
        global _DIM_WARNED
        if not _DIM_WARNED:
            _DIM_WARNED = True
            import sys
            print("WARN(embeddings): vector dim mismatch %d vs %d — dense scores collapsing to 0 "
                  "(embedder mismatch between write-time and query-time; rebuild the index/DB with "
                  "one embedder)." % (len(a), len(b)), file=sys.stderr)
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    return dot  # vectors are L2-normalized at creation, so dot == cosine


class EmbeddingProvider:
    name = "base"
    dim = 0

    def embed(self, text: str, kind: str = "passage") -> list[float]:
        """kind = 'passage' (stored fact) | 'query' (search) — some models
        (e5, jina-v3) encode the two asymmetrically."""
        raise NotImplementedError

    def embed_batch(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        """Embed many texts at once (much faster for indexing a repo)."""
        return [self.embed(t, kind) for t in texts]


class HashEmbedding(EmbeddingProvider):
    """Feature-hashing embedding. Deterministic, $0, no download. Weak semantics
    (no paraphrase/cross-lingual) — the pipeline-proving default; not for prod."""
    name = "hash"

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, text: str, kind: str = "passage") -> list[float]:
        v = [0.0] * self.dim
        for tok in tokenize(text):
            # md5 here is a feature-hash (bucketing tokens into dims), NOT security. The digest
            # value must stay stable or already-persisted hash-vectors break, so we keep md5 and
            # only flag it non-security to silence scanners.
            h = int(hashlib.md5(tok.encode("utf-8"), usedforsecurity=False).hexdigest(), 16)
            v[h % self.dim] += 1.0
            v[(h // self.dim) % self.dim] -= 0.5     # sign variety -> less collision
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]


# --------------------------------------------------------------------------- #
#  HF download resilience — model weights come from huggingface.co, which is
#  UNREACHABLE for a large slice of users (mainland China blocks it; corporate
#  intranets too). Without a backup the first-use download hangs, the run
#  silently degrades to hash, and `pipx inject fastembed` looks broken.
#  Ladder: user-set endpoint (COLLIE_HF_ENDPOINT / HF_ENDPOINT — respected, no
#  second-guessing) > default huggingface.co with ONE automatic retry through
#  hf-mirror.com (the de-facto China mirror) > the caller's hash fallback.
# --------------------------------------------------------------------------- #
_HF_MIRROR = "https://hf-mirror.com"


def _hf_endpoint(url: str):
    """Point huggingface_hub at `url`. Setting the env var is NOT enough once hf is
    imported — it computes ENDPOINT + the URL template at import time — so patch both."""
    os.environ["HF_ENDPOINT"] = url
    try:
        import huggingface_hub.constants as _c
        _c.ENDPOINT = url
        _c.HUGGINGFACE_CO_URL_TEMPLATE = url + "/{repo_id}/resolve/{revision}/{filename}"
    except ImportError:
        pass


def _hf_build(make, what: str):
    """Run `make()` (a fastembed model load — downloads weights on first use). On failure
    with the DEFAULT endpoint, retry once via hf-mirror.com. A missing fastembed install
    (ImportError) and a user-chosen endpoint both propagate untouched."""
    custom = os.environ.get("COLLIE_HF_ENDPOINT")
    if custom:
        _hf_endpoint(custom)
    if custom or os.environ.get("HF_ENDPOINT"):
        return make()                                    # user chose the endpoint — their call
    try:
        return make()
    except ImportError:
        raise                                            # not a download problem — no retry
    except Exception as e:
        import sys
        print("[embed] %s load failed (%s: %s) — retrying via %s"
              % (what, type(e).__name__, str(e)[:120], _HF_MIRROR), file=sys.stderr)
        _hf_endpoint(_HF_MIRROR)
        return make()


class LocalEmbedding(EmbeddingProvider):
    """Real local semantic embedding via fastembed (ONNX, CPU, $0, offline).

    Default = jinaai/jina-embeddings-v3 (1024-d, matryoshka, 89 langs + code) —
    picked by an on-machine acid test (5/5 on paraphrase + zh<->en cross-lingual;
    e5-large 3/5, mpnet 4/5). Accuracy-first because retrieval quality is pain #1.
    Tradeoff: ~0.4s/embed warm on CPU (the daemon amortizes cold load). Profiles:
        local:sentence-transformers/paraphrase-multilingual-mpnet-base-v2  # ~10ms, 4/5, fast auto-prefetch
        local:intfloat/multilingual-e5-large                              # 145ms, 3/5

    FAILURE MODE that bit us (2026-07): a TRANSIENT tokenizer error
    (`TypeError: TextEncodeInput must be Union[...]` in encode_batch, from an
    incomplete jina model download) made a run silently fall back to the 256-d
    hash embedder mid-session. That poisoned data/memory.db with a MIX of
    hash(256-d) + jina(1024-d) rows; at query time the dim mismatch collapses
    every dense score to 0, so recall silently degrades to BM25-only. The model
    is fine once fully cached — the real hazard is the mixed-embedder DB, and the
    built-in cure is `collie mem reembed` (re-embeds every row with the current
    model so the whole store shares one space). If you ever change the default
    here, run that reembed pass or the old-space rows go dark.

    Quality upgrade path (max MTEB, needs a free GPU + torch, not deployed here):
        # from sentence_transformers import SentenceTransformer
        # SentenceTransformer("Qwen/Qwen3-Embedding-0.6B", device="cuda")  # or bge-m3 / Qwen3-8B
    fastembed keeps us torch-free and off the GPU so it never contends with other
    local ML jobs (dota/skyreels) for VRAM.
    """
    # e5 family needs "query:"/"passage:" prefixes; jina-v3/mpnet do not.
    _PREFIXED = ("e5",)

    def __init__(self, model: str = "jinaai/jina-embeddings-v3", threads: int | None = None):
        self.model = model
        self.name = model.split("/")[-1]
        # ONNX ignores OMP_NUM_THREADS for its intra-op pool and grabs every core — cap it
        # here so a big ingest (e.g. LongMemEval) doesn't saturate the box.
        if threads is None:
            _t = os.environ.get("COLLIE_EMBED_THREADS")
            threads = int(_t) if _t else None

        def mk():
            from fastembed import TextEmbedding      # optional dep; lazy import
            return TextEmbedding(model_name=model, threads=threads) if threads \
                else TextEmbedding(model_name=model)
        self._m = _hf_build(mk, model)               # first use downloads — mirror retry inside
        self._prefix = any(k in model.lower() for k in self._PREFIXED)
        self.dim = len(self.embed("dimension probe"))

    def embed(self, text: str, kind: str = "passage") -> list[float]:
        return self.embed_batch([text], kind)[0]

    def embed_batch(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        if self._prefix:
            pfx = "query: " if kind == "query" else "passage: "
            texts = [pfx + t for t in texts]
        out = []
        for v in self._m.embed(texts):                  # fastembed batches internally
            v = v.tolist()
            n = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([x / n for x in v])
        return out


class STEmbedding(EmbeddingProvider):
    """sentence-transformers backend — for models fastembed lacks, notably
    Qwen/Qwen3-Embedding-0.6B (2025-26 MTEB leader, big code-retrieval edge, Apache-2.0).
    Heavier than fastembed/ONNX (pulls torch) — use when the retrieval gain is worth it."""
    def __init__(self, model: str = "Qwen/Qwen3-Embedding-0.6B"):
        from sentence_transformers import SentenceTransformer
        # Default to CPU: the box's GPU runs other jobs, and code-chunk batches OOM'd a
        # 32GB card. Override with COLLIE_ST_DEVICE=cuda when the GPU is free.
        dev = os.environ.get("COLLIE_ST_DEVICE", "cpu")
        self._m = SentenceTransformer(model, device=dev)
        self.name = model.split("/")[-1]
        self._batch = int(os.environ.get("COLLIE_ST_BATCH", "8"))
        self._has_qprompt = "query" in (getattr(self._m, "prompts", {}) or {})

    def embed_batch(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        kw = {"normalize_embeddings": True, "batch_size": self._batch}
        if kind == "query" and self._has_qprompt:       # Qwen3 uses a query instruction prompt
            kw["prompt_name"] = "query"
        return [v.tolist() for v in self._m.encode(list(texts), **kw)]

    def embed(self, text: str, kind: str = "passage") -> list[float]:
        return self.embed_batch([text], kind)[0]


class DaemonEmbedding(EmbeddingProvider):
    """Client to the resident embed daemon (embed_server) — keeps the model warm ACROSS `collie`
    invocations so each call embeds in ~50-150ms instead of paying the ~1.3s cold load. Policy:
    if the daemon is up, use it. If not, spawn it and WAIT for it to load (the daemon binds its
    socket only after the model is ready, so a successful ping == warm) — the model loads ONCE,
    in the daemon, not twice. First-ever call is ~as slow as today; every call after is fast.
    If the daemon can't come up at all, fall back to an in-process model so nothing breaks."""

    def __init__(self, model: str = "jinaai/jina-embeddings-v3"):
        import json
        import socket
        import time
        self._json = json
        self._socket = socket
        self._time = time
        self.model = model
        self.name = model.split("/")[-1]
        from .embed_server import sock_path
        self._path = sock_path(model)
        self._fallback = None                 # in-process LocalEmbedding, only if daemon fails
        try:
            self.dim = self._request({"op": "ping"}, timeout=3)["dim"]
            return                            # daemon already warm
        except Exception:
            pass
        self._spawn()                         # not up -> start it and wait for it to load once
        for _ in range(140):                  # up to ~21s for the model to warm
            try:
                self.dim = self._request({"op": "ping"}, timeout=3)["dim"]
                return
            except Exception:
                # if the daemon PROCESS already died (model load failed), stop waiting the full 21s
                # and degrade in-process immediately instead of stalling then re-crashing.
                if getattr(self, "_proc", None) is not None and self._proc.poll() is not None:
                    break
                time.sleep(0.15)
        self._use_fallback()                  # daemon never came up -> in-process

    def _request(self, obj, timeout=130):
        s = self._socket.socket(self._socket.AF_UNIX)
        s.settimeout(timeout)
        s.connect(self._path)
        try:
            s.sendall((self._json.dumps(obj) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                ch = s.recv(1 << 20)
                if not ch:
                    break
                buf += ch
        finally:
            s.close()
        # A truncated frame (daemon killed mid-send) has no newline and non-empty bytes ->
        # json.loads would raise JSONDecodeError (a ValueError, NOT OSError) and escape the
        # callers' fallback. Normalize EVERY malformed reply to OSError so the in-process
        # fallback always engages instead of crashing the run.
        if b"\n" not in buf and buf:
            raise OSError("truncated daemon response")
        try:
            r = self._json.loads(buf.split(b"\n", 1)[0] or b"{}")
        except ValueError as e:
            raise OSError("garbled daemon response: %s" % e)
        if not r.get("ok"):
            raise OSError("daemon error: %s" % r.get("error"))
        return r

    def _spawn(self):
        import subprocess
        self._proc = None
        try:
            self._proc = subprocess.Popen(
                [__import__("sys").executable, "-m", "harness.embed_server", "--model", self.model],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **plat.new_group_kwargs())
        except Exception:
            pass

    def _use_fallback(self):
        if self._fallback is None:
            self._fallback = LocalEmbedding(self.model)
            self.dim = self._fallback.dim

    def embed(self, text: str, kind: str = "passage") -> list[float]:
        return self.embed_batch([text], kind)[0]

    def embed_batch(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        if self._fallback is not None:
            return self._fallback.embed_batch(texts, kind)
        try:
            return self._request({"texts": texts, "kind": kind})["vectors"]
        except Exception:                     # any daemon failure -> in-process (never crash)
            self._use_fallback()
            return self._fallback.embed_batch(texts, kind)


_EMB_CACHE = {}


def make_embedding(name: str = "hash") -> EmbeddingProvider:
    # CACHE the instance per name. A LocalEmbedding/STEmbedding loads a multi-GB ONNX/torch model
    # whose C++ arena memory is NOT returned to the OS by Python gc (see swe_predict_one). The web
    # server builds a fresh harness (hence a fresh embedder) PER request; without this cache every
    # query re-loaded jina-v3 and leaked ~2GB, climbing to 12GB+ and OOM-killing WSL. Embedders are
    # stateless (embed_batch is pure), so one shared instance per name is correct.
    if name in _EMB_CACHE:
        return _EMB_CACHE[name]
    _EMB_CACHE[name] = _build_embedding(name)
    return _EMB_CACHE[name]


def _build_embedding(name: str) -> EmbeddingProvider:
    if name == "daemon":
        return DaemonEmbedding()
    if name == "hash":
        return HashEmbedding()
    if name in ("local", "e5", "prod"):
        return LocalEmbedding()
    if name in ("qwen3", "qwen3-embed"):
        return STEmbedding("Qwen/Qwen3-Embedding-0.6B")
    if name.startswith("st:"):                          # st:<hf-model-id> via sentence-transformers
        return STEmbedding(name.split(":", 1)[1])
    if name.startswith("local:"):                       # local:<hf-model-id> via fastembed
        return LocalEmbedding(name.split(":", 1)[1])
    raise ValueError("unknown embedding: %s" % name)


# --------------------------------------------------------------------------- #
#  Reranker — a cross-encoder that scores (query, doc) JOINTLY, unlike the
#  bi-encoder embeddings above. 2025-26 memory research (LOCOMO / LongMemEval)
#  found a small local reranker over the fused top-k is the single highest-ROI
#  retrieval upgrade — worth more than enlarging the base embedder (~4-8 MAP pts).
#  Off by default (keeps the lean, no-extra-model path); opt in to trade a few
#  ms/candidate for accuracy. Local + $0 + offline via fastembed.
# --------------------------------------------------------------------------- #
class Reranker:
    name = "reranker"

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        """Return one relevance score per doc (higher = better)."""
        raise NotImplementedError


class LocalReranker(Reranker):
    def __init__(self, model: str = "jinaai/jina-reranker-v2-base-multilingual"):
        def mk():
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            return TextCrossEncoder(model_name=model)
        self._m = _hf_build(mk, model)               # same first-use download + mirror retry
        self.name = "rerank:" + model.split("/")[-1]

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        return list(self._m.rerank(query, docs)) if docs else []


_RERANK_CACHE = {}


def make_reranker(name: str | None):
    if not name or name in ("none", "off"):
        return None
    if name in _RERANK_CACHE:                            # cache the cross-encoder model (same
        return _RERANK_CACHE[name]                       # per-request ONNX-leak hazard as embedders)
    if name in ("local", "jina", "on"):
        r = LocalReranker()
    elif name.startswith("local:"):                      # local:<hf-reranker-id>
        r = LocalReranker(name.split(":", 1)[1])
    else:
        raise ValueError("unknown reranker: %s" % name)
    _RERANK_CACHE[name] = r
    return r
