"""Code navigation over a repo's source files — ripgrep-backed (no embedding index).

`code_search(query)` pulls the code-like identifiers out of a natural-language query and greps the
repo for them, ranking files by how many DISTINCT query terms they contain and returning the top
`path:line` snippets. This replaced a semantic embedding index: measured head-to-head on SWE-bench
(`bench/grep_vs_codesearch.py`), grep localization matched-or-beat the embedding index (file-hit@10
0.83 vs 0.67 on the same instances) at a fraction of the cost — no model download, no index build,
and it never goes stale because ripgrep always reads current file contents (so `invalidate()` is a
no-op). The multi-file-coverage helpers (`related*`) keep the one signal that actually drove them —
same-package (same-directory) siblings, ranked by shared identifiers — which the embedding version's
own comments noted was structural, not topical.

Cross-platform: the search runs through `plat.shell_argv`, so `rg`/`grep` execute under the POSIX
shell collie guarantees on every OS (Git Bash on Windows); ripgrep is used when present, else grep.
"""
import os
import re
import subprocess

from . import plat
from .tools import Tool

SRC_EXT = {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte",
           ".go", ".java", ".rb", ".rs", ".c", ".cc", ".cpp", ".h", ".hpp", ".php", ".cs",
           ".kt", ".swift", ".scala", ".lua", ".cfg", ".ini", ".toml"}
SKIP_DIR = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
            ".tox", ".mypy_cache", ".pytest_cache", "site-packages", ".idea", ".vscode",
            "tests", "test", "testing"}

# prose words that localize nothing — dropped from a query before grepping
_STOP = set("""the a an and or of to in is be it that this for with on as at by from not no if
    then else when while return def class self none true false import error issue bug should would
    could where which what how why you your we our they them than into out over under add remove
    fix make use using need want get set new old value values type types file files line lines""".split())


def _is_test_file(fn):
    return fn.startswith("test_") or fn.endswith(("_test.py", "_tests.py", ".test.js"))


def _iter_files(root, max_bytes=200_000):
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIR and not d.startswith(".")]
        for fn in fns:
            if os.path.splitext(fn)[1] in SRC_EXT and not _is_test_file(fn):
                p = os.path.join(dp, fn)
                if os.path.islink(p):                 # never index a symlink (host-file exfil guard)
                    continue
                try:
                    if os.path.getsize(p) <= max_bytes:
                        yield p
                except OSError:
                    pass


def _query_terms(text, limit=16):
    """Terms a human would grep for, weighted by signal: quoted/backtick code spans (3) > code-shaped
    identifiers — snake_case / CamelCase / dotted.paths (2) > plain words (1). Stopwords and 1-2 char
    noise are dropped. Plain words are KEPT (weighted lower) so a natural-language query like 'where
    the posix shell is chosen' still greps `posix`/`shell`; distinct-term ranking floats the files
    that match the most query terms to the top, so common words don't dominate."""
    cand = {}
    for m in re.findall(r'[`\'"]([A-Za-z_][A-Za-z0-9_.]{2,})[`\'"]', text):
        cand[m] = max(cand.get(m, 0), 3)
    for m in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]{2,}(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\b', text):
        if m.lower() in _STOP:
            continue
        w = 2 if (("_" in m) or ("." in m) or any(c.isupper() for c in m)) else 1
        cand[m] = max(cand.get(m, 0), w)
    return sorted(cand, key=lambda t: (-cand[t], -len(t)))[:limit]


def _sh(s):                                            # POSIX single-quote
    return "'" + s.replace("'", "'\\''") + "'"


def _grep_matches(root, terms, timeout=25):
    """Run one ripgrep (fallback grep) over `root` for any of `terms` (fixed strings). Returns
    {relpath: (set(distinct terms present), first_line_no, first_line_text)}. Empty on no match."""
    if not terms:
        return {}
    excl_rg = " ".join("-g '!%s'" % d for d in SKIP_DIR) + " -g '!*.min.js' -g '!*.map' -g '!*.lock'"
    excl_gr = " ".join("--exclude-dir=%s" % d for d in SKIP_DIR)
    pats = " ".join("-e %s" % _sh(t) for t in terms)
    rg = "rg -n --no-heading -F --max-filesize 500K %s %s -- ." % (excl_rg, pats)
    gr = "grep -rnI -F %s %s -- ." % (excl_gr, pats)
    argv, use_shell = plat.shell_argv(rg + " || " + gr)
    try:
        p = subprocess.run(argv, shell=use_shell, cwd=root, stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=timeout,
                           **plat.no_window_kwargs())
    except Exception:
        return {}
    low_terms = [(t, t.lower()) for t in terms]
    out = {}
    for line in (p.stdout or "").splitlines():
        # rg/grep line format: path:lineno:text
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        # Native Windows rg emits ``.\\path`` while POSIX rg emits ``./path``.
        # Normalize separators first, then remove only actual ``./`` path
        # components.  ``lstrip("./")`` treated its argument as a character set:
        # it turned ``.\\mod.py`` into ``/mod.py`` and ``./.config/x.py`` into
        # ``config/x.py``.
        rel = parts[0].replace("\\", "/")
        while rel.startswith("./"):
            rel = rel[2:]
        no, text = parts[1], parts[2]
        if os.path.splitext(rel)[1] not in SRC_EXT or _is_test_file(os.path.basename(rel)):
            continue
        tl = text.lower()
        present = {t for (t, tlow) in low_terms if tlow in tl}
        if not present:
            continue
        if rel not in out:
            try:
                lineno = int(no)
            except ValueError:
                lineno = 1
            out[rel] = [set(), lineno, text.strip()[:200]]
        out[rel][0] |= present
    return {k: (v[0], v[1], v[2]) for k, v in out.items()}


class CodeIndex:
    """Ripgrep-backed searcher. Kept the class name/shape for API compatibility (cli init, benches);
    there is no vector index to build, so build() is a cheap no-op and results are always fresh."""

    def __init__(self, root, embedder=None, **_kw):        # embedder ignored (kept for call-site compat)
        self.root = root

    def build(self):
        return 0                                           # ripgrep is index-free

    def search(self, query, k=6):
        terms = _query_terms(query)
        matches = _grep_matches(self.root, terms)
        if not matches:
            return []
        # rank by how many DISTINCT query terms a file contains, then by earliest match
        ranked = sorted(matches.items(), key=lambda kv: (-len(kv[1][0]), kv[1][1]))
        out = []
        for rel, (present, lineno, snippet) in ranked[:k]:
            out.append("%s:%d  %s" % (rel, lineno, snippet))
        return out

    def _siblings(self, query_text, edited_path, exclude_paths, k):
        """Same-package (same-directory) source files sharing identifiers with the edit, ranked.
        Structure dominates: a same-directory sibling gets a 3x boost (the embedding version's own
        finding — collaborators are structural neighbours, not the most topically-similar files)."""
        terms = _query_terms(query_text)
        matches = _grep_matches(self.root, terms)
        excl = set(exclude_paths or [])
        edir = os.path.dirname((edited_path or "").replace("\\", "/"))
        nterms = len(terms) or 1
        scored = []
        for rel, (present, lineno, _snip) in matches.items():
            if rel in excl or rel == (edited_path or "").replace("\\", "/"):
                continue
            score = len(present) / nterms
            if os.path.dirname(rel) == edir:               # same package -> collaborator
                score *= 3.0
            scored.append((min(1.0, score), rel, lineno))
        scored.sort(reverse=True)
        return scored[:k]

    def related(self, query_text, edited_path, exclude_paths, k=4):
        return ["%s:%d" % (rel, ln) for _s, rel, ln in
                self._siblings(query_text, edited_path, exclude_paths, k)]

    def related_scored(self, query_text, edited_path, exclude_paths, k=8, min_score=0.0):
        return [("%s:%d" % (rel, ln), round(s, 3)) for s, rel, ln in
                self._siblings(query_text, edited_path, exclude_paths, k) if s >= min_score]


_INDEX = {}


def get_index(root, embedder=None):
    if root not in _INDEX:
        _INDEX[root] = CodeIndex(root, embedder)
    return _INDEX[root]


def invalidate(root=None):
    """No-op: ripgrep always reads current file contents, so there is no stale index to drop.
    Kept as a call-site-compatible seam (the edit/write/checkpoint tools call it)."""
    return None


def related_locations(root, edited_text, edited_path, exclude_paths, k=4):
    """Multi-file coverage: same-package sibling spots that may need the same change."""
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
    description = ("Locate WHERE in the repo to look or edit. Extracts the identifiers from your "
                   "query and greps the repo, returning the top path:line matches ranked by how "
                   "many of your terms each file contains. Use this FIRST to find the right file "
                   "before reading/editing. Args: query, optional k.")
    schema = {"type": "object", "properties": {
        "query": {"type": "string"}, "k": {"type": "integer"}}, "required": ["query"]}

    def run(self, args, ctx):
        try:
            idx = get_index(ctx.cwd)
            hits = idx.search(args["query"], k=int(args.get("k", 6)))
            return "\n".join(hits) or "(no code matches)"
        except Exception as e:
            return "ERROR(code_search): %s" % e


def register_code_search(registry):
    registry.register(CodeSearchTool())
    return True
