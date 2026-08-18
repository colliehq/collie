"""collie — a minimal, evolvable coding-agent harness.

Design goal: every subsystem is behind an abstract seam so v1 can be swapped
part-by-part into v2+ without rewrites. See README.md for the architecture map.

Seams (abstract base -> v1 impl -> future):
  ModelProvider      -> MockProvider / AnthropicProvider   -> local/ollama
  EmbeddingProvider  -> HashEmbedding                       -> bge-m3 / fastembed
  MemoryStore        -> SqliteMemory (FTS5 + brute cosine)  -> sqlite-vec + rerank
  Tool / ToolRegistry-> core tools, two-tier               -> MCP, deferred load
  ContextComposer    -> STABLE/CONTEXT/VOLATILE tiers       -> learned policies
"""
# Silence the noisy RequestsDependencyWarning (urllib3/chardet version skew in a transitive
# dep) BEFORE `requests` gets imported anywhere downstream — it printed on every collie run.
import warnings as _warnings
_warnings.filterwarnings("ignore", message=r".*doesn't match a supported version.*")

__version__ = "0.20.36"   # iteration log in CHANGELOG.md; runs are tagged with this
