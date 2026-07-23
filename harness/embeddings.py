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


# The default real embedder: IBM granite-embedding-107m-multilingual. Apache-2.0, ~55MB int8,
# 384-d, CLS-pooled, strong cross-lingual (measured LOCOMO recall@10 0.600 with correct CLS pooling
# — near jina-v3's 0.621 at 1/40th the size; see docs/MEMORY_BENCH.md). Loaded via raw onnxruntime
# (NOT fastembed, whose Python model menu lacks it), so collie can run ANY ONNX model on the Hub.
GRANITE = "ibm-granite/granite-embedding-107m-multilingual"


class OnnxEmbedding(EmbeddingProvider):
    """Any ONNX sentence-embedding model, run through onnxruntime + tokenizers directly (no torch,
    no fastembed). This is what frees collie from fastembed's narrow Python model list: point it at
    a HuggingFace repo's .onnx + tokenizer and it works. Handles CLS vs mean pooling and the e5
    `query:`/`passage:` prefix. Cross-platform: onnxruntime ships CPU wheels for win/mac/linux."""

    def __init__(self, repo: str = GRANITE, onnx_file: str = "model.onnx", pooling: str = "cls",
                 e5_prefix: bool = False, tok_file: str = "tokenizer.json",
                 data_file: str | None = None, name: str | None = None):
        self.model = repo
        self.name = name or repo.split("/")[-1]
        self.pooling = pooling                             # "cls" (granite/bge/arctic) | "mean" (e5/gte)
        self.e5_prefix = e5_prefix
        _t = os.environ.get("COLLIE_EMBED_THREADS")
        threads = int(_t) if _t else min(8, os.cpu_count() or 8)

        def mk():
            import numpy as np
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
            import onnxruntime as ort
            if data_file:                                  # external-weights models (e.g. bge-m3)
                hf_hub_download(repo, data_file)
            mp = hf_hub_download(repo, onnx_file)
            tp = hf_hub_download(repo, tok_file)
            self._np = np
            self._tok = Tokenizer.from_file(tp)
            self._tok.enable_truncation(max_length=512)
            so = ort.SessionOptions()
            so.intra_op_num_threads = threads
            self._sess = ort.InferenceSession(mp, sess_options=so, providers=["CPUExecutionProvider"])
            self._inputs = {i.name for i in self._sess.get_inputs()}
            return True
        _hf_build(mk, repo)                                # first use downloads — mirror retry inside
        self.dim = len(self.embed("dimension probe"))

    def embed(self, text: str, kind: str = "passage") -> list[float]:
        return self.embed_batch([text], kind)[0]

    def embed_batch(self, texts: list[str], kind: str = "passage") -> list[list[float]]:
        np = self._np
        out = []
        for t in texts:
            if self.e5_prefix:
                t = ("query: " if kind == "query" else "passage: ") + t
            enc = self._tok.encode(t)
            ids = np.array([enc.ids], dtype=np.int64)
            mask = np.array([enc.attention_mask], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            last = self._sess.run(None, feed)[0]           # (1, seq, dim)
            if self.pooling == "cls":
                vec = last[0][0]
            else:
                m = mask[0][:, None].astype(np.float32)
                vec = (last[0] * m).sum(0) / max(float(m.sum()), 1e-9)
            n = float(np.linalg.norm(vec)) or 1.0
            out.append((vec / n).astype(np.float32).tolist())
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
    if name == "hash":
        return HashEmbedding()
    if name in ("granite", "local", "prod", "default"):   # the permissive default real embedder
        return OnnxEmbedding(GRANITE, pooling="cls")
    if name in ("bge-m3", "m3"):                          # quality opt-in: MIT, dense+sparse+colbert
        return OnnxEmbedding("BAAI/bge-m3", onnx_file="onnx/model.onnx",
                             data_file="onnx/model.onnx_data", pooling="cls", name="bge-m3")
    if name in ("gte", "gte-multilingual"):
        return OnnxEmbedding("onnx-community/gte-multilingual-base",
                             onnx_file="onnx/model_int8.onnx", pooling="cls", name="gte-multilingual")
    if name in ("e5", "e5-small"):                        # multilingual-e5-small int8 (MIT, mean-pooled)
        return OnnxEmbedding("intfloat/multilingual-e5-small",
                             onnx_file="onnx/model_qint8_avx512_vnni.onnx", tok_file="onnx/tokenizer.json",
                             pooling="mean", e5_prefix=True, name="e5-small")
    if name in ("jina", "jina-v3"):                      # fastembed jina-v3 (CC-BY-NC) — explicit opt-in
        return LocalEmbedding("jinaai/jina-embeddings-v3")
    if name in ("qwen3", "qwen3-embed"):
        return STEmbedding("Qwen/Qwen3-Embedding-0.6B")
    if name.startswith("onnx:"):                         # onnx:<repo>[:cls|mean] via raw onnxruntime
        rest = name.split(":", 1)[1]
        pool = "cls"
        if rest.endswith(":mean") or rest.endswith(":cls"):
            rest, pool = rest.rsplit(":", 1)
        return OnnxEmbedding(rest, pooling=pool)
    if name.startswith("st:"):                           # st:<hf-model-id> via sentence-transformers
        return STEmbedding(name.split(":", 1)[1])
    if name.startswith("local:"):                        # local:<hf-model-id> via fastembed
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


# Default reranker: bge-reranker-v2-m3 int8 (Apache-2.0, multilingual incl. strong Chinese). Chosen
# over jina-reranker-v2 (CC-BY-NC — can't be a permissive default) after a design-space sweep; run
# via onnxruntime int8 and capped to the top candidates, it's the low-spec-viable cross-encoder
# (LOCOMO: granite+this-reranker top-20 = 0.644 vs granite-only 0.556, at ~6x less CPU than jina).
RERANKER = "onnx-community/bge-reranker-v2-m3-ONNX"


class OnnxReranker(Reranker):
    """Cross-encoder reranker via onnxruntime int8 (no torch, no fastembed). Scores (query, doc)
    jointly. Capped to the top `cap` candidates so the per-query cost stays weak-CPU-viable."""

    def __init__(self, repo: str = RERANKER, onnx_file: str = "onnx/model_int8.onnx",
                 tok_file: str = "tokenizer.json", cap: int = 20):
        self.cap = cap
        self.name = "rerank:" + repo.split("/")[-1]

        def mk():
            import numpy as np
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
            import onnxruntime as ort
            self._np = np
            self._tok = Tokenizer.from_file(hf_hub_download(repo, tok_file))
            self._tok.enable_truncation(max_length=512)
            so = ort.SessionOptions()
            _t = os.environ.get("COLLIE_EMBED_THREADS")
            so.intra_op_num_threads = int(_t) if _t else min(8, os.cpu_count() or 8)
            self._sess = ort.InferenceSession(hf_hub_download(repo, onnx_file), sess_options=so,
                                              providers=["CPUExecutionProvider"])
            self._inputs = {i.name for i in self._sess.get_inputs()}
            return True
        _hf_build(mk, repo)

    def rerank(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        np = self._np
        scores = [-1e9] * len(docs)
        for i in range(min(self.cap, len(docs))):          # only the top `cap` candidates
            enc = self._tok.encode(query, docs[i][:1200])   # RoBERTa pair encoding
            ids = np.array([enc.ids], dtype=np.int64)
            mask = np.array([enc.attention_mask], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            scores[i] = float(self._sess.run(None, feed)[0].reshape(-1)[0])
        return scores


class LocalReranker(Reranker):
    """fastembed cross-encoder (jina-reranker-v2, CC-BY-NC). Kept as an explicit opt-in only —
    NOT the default, because its non-commercial license can't ship as a permissive default."""
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
    if name in ("local", "on", "bge", "default"):        # the permissive default (Apache, onnx int8)
        r = OnnxReranker()
    elif name in ("jina",):                              # explicit opt-in to the CC-BY-NC fastembed one
        r = LocalReranker()
    elif name.startswith("onnx:"):                       # onnx:<hf-reranker-id>
        r = OnnxReranker(name.split(":", 1)[1])
    elif name.startswith("local:"):                      # local:<hf-reranker-id> via fastembed
        r = LocalReranker(name.split(":", 1)[1])
    else:
        raise ValueError("unknown reranker: %s" % name)
    _RERANK_CACHE[name] = r
    return r
