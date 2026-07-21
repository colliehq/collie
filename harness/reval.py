"""Retrieval quality eval — measures pain #1 directly (precision@k / MRR).

A small labeled set (multilingual, paraphrase + cross-lingual) with one gold fact
per query. Runs the harness's real hybrid recall and scores it. Also runs the Hash
baseline so the dashboard can show what the real embedding buys.
"""
import json
import os
import tempfile

from .memory import SqliteMemory
from .embeddings import make_embedding

# Synthetic multilingual fixtures (generic software / ML / devops topics). Kept deliberately
# impersonal so the open-source eval reveals nothing about any real project or person.
CORPUS = [
    ("Kubernetes 集群自动扩缩容与负载均衡配置指南", "k8s autoscale"),
    ("图像分类 CNN 模型的迁移学习训练流程", "cnn transfer"),
    ("服务用 Redis 加 PostgreSQL 加全文索引做混合查询", "hybrid query"),
    ("电商订单分析 SaaS 带用户分群数据", "ecommerce saas"),
    ("使用本地向量库避免云端 API 调用成本", "local vector"),
    ("微服务每次请求固定开销高达数百毫秒", "request overhead"),
    ("开源大语言模型是便宜且兼容 OpenAI 的选择", "oss llm"),
    ("CI 流水线需要缓存依赖以加速构建", "ci cache"),
    ("文本生成模型在单张 GPU 上跑小参数版本", "text gen gpu"),
    ("缓存层每次请求把热点数据预取到内存", "cache prefetch"),
]
# (query, index of the one relevant fact) — paraphrase + zh<->en cross-lingual
QUERIES = [
    ("kubernetes cluster autoscaling", 0),
    ("transfer learning for image classification", 1),
    ("how does the service do hybrid search", 2),
    ("ecommerce customer segmentation tool", 3),
    ("avoid paying for cloud vector api", 4),
    ("为什么微服务每次请求这么慢", 5),
    ("cheap openai compatible llm", 6),
    ("cache dependencies to speed up ci", 7),
    ("local text generation model on gpu", 8),
    ("每次请求自动预取热点数据", 9),
]


def run_eval(embedder, reranker=None) -> dict:
    db = tempfile.mktemp(suffix=".db")
    m = SqliteMemory(db, embedder=embedder, reranker=reranker)
    ids = [m.remember(t, keys=k, project="eval") for t, k in CORPUS]
    n = len(QUERIES)
    p1 = p5 = mrr = 0.0
    for q, gold in QUERIES:
        hits = m.recall(q, project="eval", k=5)
        hit_ids = [h["id"] for h in hits]
        goldid = ids[gold]
        rank = hit_ids.index(goldid) + 1 if goldid in hit_ids else 0
        p1 += 1 if rank == 1 else 0
        p5 += 1 if 0 < rank <= 5 else 0
        mrr += 1.0 / rank if rank else 0
    m.close()
    try:
        os.remove(db)
    except OSError:
        pass
    return {"embedder": embedder.name, "n": n,
            "p_at_1": round(p1 / n, 3), "p_at_5": round(p5 / n, 3),
            "mrr": round(mrr / n, 3)}


def run_and_save(out_path: str, embed_name: str = "local") -> dict:
    """Eval the real embedder + the Hash baseline; write JSON for the dashboard."""
    from .embeddings import make_reranker
    result = {"real": None, "hash": run_eval(make_embedding("hash"))}
    try:
        emb = make_embedding(embed_name)
        result["real"] = run_eval(emb)
        try:                                              # + cross-encoder reranker
            rr = make_reranker("local")
            r = run_eval(emb, reranker=rr)
            r["embedder"] += " + reranker"
            result["real_rerank"] = r
        except Exception:
            pass
    except Exception as e:
        result["real"] = {"embedder": "unavailable (%s)" % type(e).__name__,
                          "n": 0, "p_at_1": 0, "p_at_5": 0, "mrr": 0}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result
