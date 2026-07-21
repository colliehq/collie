# Memory benchmark — LOCOMO (the standard agent-memory benchmark)

collie's memory measured on snap-research/locomo (10 conversations, ~1500 QA, `evidence`
+ `category` annotations). Category 5 (disputed adversarial/unanswerable) excluded.

## Retrieval recall@k — what collie's memory subsystem owns (n=383, 3 conv)

| config | recall@10 | hit@10 |
|---|:--:|:--:|
| hybrid (BM25 + jina-v3 dense + RRF) | 0.621 | 0.676 |
| **+ cross-encoder reranker** | **0.749** | **0.809** |

The reranker (jina-reranker-v2, local) lifts evidence recall@10 by **+12.8 pts (+20.6%)** on
real LOCOMO questions — confirming the research finding (a reranker is the highest-ROI
retrieval upgrade) on the standard benchmark, not just a toy corpus.

## End-to-end QA (LLM-as-judge — the protocol Mem0/Zep report) (n=231, 2 conv)

| system | accuracy | note |
|---|:--:|---|
| **collie** (retrieve top-k + DeepSeek answer, reranker on) | **42.4%** | DeepSeek judge |
| Mem0 (reported) | ~66.9% | GPT-4o-mini judge |
| Zep (reported) | ~75% | disputed |

**Honest read:** collie's end-to-end 42% is below Mem0's 67% — and the *reason* is clear.
Retrieval recall@10 is a healthy 0.75, but end-to-end drops to 0.42 because collie stores
**raw conversation turns** while Mem0 does **LLM extraction + consolidation** (distils clean
facts). Reasoning over 10 raw dialogue snippets (multi-hop / temporal questions, a weaker
DeepSeek answerer+judge) caps accuracy below the retrieval ceiling. This benchmark-confirms
the research's #3 lesson.

Comparability caveats (per the research, LOCOMO figures are directional): different judge
model (DeepSeek vs GPT-4o-mini), different memory representation (top-k snippets vs Mem0's
extracted facts), and LOCOMO itself is contested (Zep 84 → Mem0 58 → Zep 75 → Mem0 67).
Also: in collie's *real* use, `remember()` stores agent-distilled facts, not raw turns — the
raw-turn ingestion here is the benchmark's format, which is exactly what extraction fixes.

## Next: extraction + consolidation (the benchmark-motivated build)

Add an optional distillation step so noisy/raw content is condensed into clean, atomic facts
at write time (Mem0/A-MEM style), and near-duplicate consolidation on top of the existing
supersession path. Expected to close much of the 42→? gap on raw-turn ingestion. Deliberately
NOT adding a knowledge graph / temporal graph (research: marginal for this use).

## Extraction measured — an honest NEGATIVE (naive per-turn distillation HURT)

Hypothesis: distil raw turns into clean facts (Mem0-style) closes the end-to-end gap.
**Measured (LOCOMO sample 0, same protocol, only ingestion differs):**

| ingestion | end-to-end acc |
|---|:--:|
| raw turns | **43.3%** (65/150) |
| per-turn LLM distillation | 34.7% (52/150) |

Naive per-turn distillation **hurt by −8.6 pts (−20%)**. Why: it drops turns (some
"chit-chat" carried answer context), rewords turns into single facts (losing detail /
exact wording / nuance the questions need), occasionally over-extracts, and destroys the
cross-turn connections LOCOMO's temporal & multi-hop questions rely on.

**Conclusion:** for conversational memory, collie storing raw turns beats naive per-turn
extraction. Mem0's edge is NOT simple extraction — it's a more sophisticated pipeline
(chunk-level, detail-preserving extraction + cross-turn consolidation). So the distiller
ships **opt-in and OFF by default**; enabling it for conversation ingestion is a
pessimization. (The reranker, by contrast, is a clear, benchmark-backed win — keep it.)
The consolidation (LLM-free near-dup supersession) is orthogonal and safe — it removes
duplicates without losing information.

Next, if pursued: chunk-level extraction (extract a *set* of atomic facts from a window of
turns, preserving detail) rather than per-turn — the actual Mem0 design. Bigger build;
only worth it if conversation memory becomes a target use case for collie.

## Extraction, done right (chunk-level) — still doesn't beat raw on LOCOMO

Follow-up to the per-turn negative: chunk-level extraction (see a whole session, emit a set
of atomic facts — the actual Mem0 design). Same sample, same protocol:

| ingestion | end-to-end acc |
|---|:--:|
| raw turns | **43.3%** |
| chunk-level extraction | 40.7% |
| per-turn distillation | 34.7% |

Chunk-level (40.7%) recovers most of the per-turn loss (34.7%) — so **chunk > per-turn**,
confirming the design direction. But it **still doesn't beat raw turns (43.3%)**. Honest
conclusion: on LOCOMO, extraction is *not* collie's lever — raw turns + reranker is the best
config, and the gap to Mem0 (~67%) comes from their full pipeline + answering model + config,
not extraction per se. The reranker remains the one clear, benchmark-backed retrieval win.

## Embedding swap — Qwen3-0.6B beats jina, but the reranker erases the gap

LOCOMO retrieval recall@10 (sample 0), jina-embeddings-v3 vs Qwen3-Embedding-0.6B:

| embedder | hybrid | + reranker |
|---|:--:|:--:|
| jina-v3 | 0.552 | **0.738** |
| Qwen3-0.6B | **0.614** | 0.736 |

Qwen3-0.6B is a better base embedder (+11% recall@10 hybrid, confirming its MTEB lead) —
**but with the reranker the two converge (0.738 ≈ 0.736)**: the cross-encoder re-scores the
candidate pool, so the base-embedder choice barely matters once a reranker is in play. This
is the research verdict, measured: *the reranker matters more than the base embedder.*
Decision: keep **jina-v3 + reranker** (lighter — fastembed/ONNX, no torch); `make_embedding
("qwen3")` is wired for reranker-off configs. We *predicted* Qwen3 would clearly pay off on
**code_search** (its big edge is code retrieval, and code_search has no reranker) — measured
below, that prediction was **wrong on both counts.**

## code_search retrieval — both predicted upgrades TESTED, both FAIL

code_search is collie's SWE lever (locate-before-edit) and the one retrieval spot with no
reranker, so it was the prime candidate for the two upgrades above. Measured on SWE-bench
Verified instances (`bench/codesearch_eval.py`): clone @ base_commit, build the code index,
`search(problem_statement)`, check if the gold-patch file lands in top-k.

**Baseline (current design — bge-small bi-encoder + code-density demotion + keyword overlap):**

| config | file_hit@k | gold_recall@k | n |
|---|:--:|:--:|:--:|
| **bge-small (shipped)** | **0.833** | 0.833 | 6 |

**Upgrade 1 — swap bge-small → Qwen3-0.6B (predicted win): INFEASIBLE.** Qwen3-0.6B (600M
transformer) has to embed the *whole repo* (~1000+ code chunks) at index-build time. On CPU
that didn't finish 2 instances in 8+ min; on GPU it OOM'd a 32GB card (and the box's GPU runs
other jobs). Operationally unusable here — and even if usable, localization is already 0.83
and is **not the SWE bottleneck** (edit/patch correctness is), so the ROI ceiling is low.
Keep bge-small (fast ONNX, ~15ms/chunk).

**Upgrade 2 — add the cross-encoder reranker (predicted win): HURTS.** Same 6 instances,
same built index, reranker off vs on:

| config | file_hit@k | gold_recall@k |
|---|:--:|:--:|
| bge-small (bi-encoder) | **0.833** | 0.833 |
| bge-small + jina-reranker-v2 | 0.667 | 0.667 |

The reranker — the clear WIN on conversational memory — **lowered** code localization. Why:
jina-reranker-v2 is a **prose-trained** cross-encoder; "NL problem statement vs 50-line code
chunk" is OOD for it, so it favors chunks that *lexically resemble the problem text* (comments,
docstrings, error strings) over the actual code to edit, overriding the bi-encoder's
code-tuned signal (density demotion + keyword overlap). n=6 makes the exact delta noisy (one
instance), but the direction is clear and there is **no upside** to justify +280MB model +
per-call rerank latency on the SWE hot path. Not wired into production — the reranker is
reachable only via the eval's `--rerank` flag; `CodeSearchTool`/`get_index` stay bi-encoder.

**Conclusion:** the reranker is domain-specific — a WIN on prose/conversation memory, a LOSS
on code retrieval. code_search's bi-encoder (bge-small + density + keyword) is already
well-tuned at 0.83 file-hit@10; both "obvious" upgrades were confirmed dead ends. The SWE
lever is downstream of localization (reading enough context + generating a correct edit),
not in the retrieval embedder.
