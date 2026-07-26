"""codemap — repo → a compact file map for the Map view (galaxy visualization).

Walks a working tree and, per source file, records the numbers the Map renders spatially:
  loc      lines of code              -> star size
  defs     top-level functions+classes-> planets (a star system's bodies)
  methods  nested methods             -> (reserved: moons)
  names    the def/class names        -> planet labels (click a star → see its functions)
  imports  internal modules it uses   -> gravity links between star systems
Grouped by top-level dir (`g`) so modules become nebulae. Python gets real AST parsing; other
languages fall back to a lightweight def/class scan so the map still populates. stdlib-only.

The web layer calls build_tree(cwd) for GET /api/tree; it is cached per (cwd, mtime-of-dir) at the
call site. Bounded (MAX_FILES) so a huge monorepo can't stall the request.
"""
from __future__ import annotations

import ast
import os
import re

# heavy / vendored / generated dirs never belong on the map
_SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache",
         ".collie", "dist", "build", ".tox", ".next", ".wrangler", "logs", ".cache", "site-packages",
         # benchmark fixtures / vendored / generated trees are not this project's own code
         "testdata", "fixtures", "vendor", "third_party", "target", "coverage", "polyglot", "sandbox"}
# pruned only as a DIRECT child of the user's home (see discover_repos) — OS stores, never projects
_HOME_SKIP = {"Library", "Applications", "Music", "Pictures", "Movies"}
_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".html", ".css", ".md", ".toml")
MAX_FILES = 600            # a request must stay snappy; bigger repos are sampled by size
_DEF_RE = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function|def|class|func|fn)\s+([A-Za-z_$][\w$]*)")


def _group(path: str) -> str:
    """Top-level dir = the nebula. harness/webui/x.html -> 'harness/webui' (one level of nesting
    for the big package), else the first path segment; a root-level file -> 'root'."""
    parts = path.split("/")
    if len(parts) == 1:
        return "root"
    if parts[0] == "harness" and len(parts) > 2:
        return "/".join(parts[:2])
    return parts[0]


def _py_facts(src: str, path: str, mod2path: dict):
    """AST-accurate defs / methods / names / internal-import edges for a Python file."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return _scan_facts(src)
    defs = methods = 0
    names, imps = [], set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs += 1
            names.append(node.name + "()")
        elif isinstance(node, ast.ClassDef):
            defs += 1
            names.append(node.name)
            methods += sum(1 for n in node.body
                           if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            m = node.module.lstrip(".")
            if m in mod2path and mod2path[m] != path:
                imps.add(mod2path[m])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in mod2path and mod2path[a.name] != path:
                    imps.add(mod2path[a.name])
    return defs, methods, names[:14], sorted(imps)


def _scan_facts(src: str):
    """Language-agnostic fallback: regex the def/class/function headers (no import graph)."""
    names = []
    for line in src.splitlines():
        m = _DEF_RE.match(line)
        if m:
            names.append(m.group(1) + ("()" if not m.group(1)[0].isupper() else ""))
    return len(names), 0, names[:14], []


def build_tree(cwd: str) -> list[dict]:
    """The map data for `cwd`: one dict per source file with loc/defs/methods/names/imports and a
    dir group. Sorted by group then size. Bounded to MAX_FILES (largest first)."""
    cwd = os.path.abspath(cwd)
    files = []
    for dp, dn, fn in os.walk(cwd):
        dn[:] = [d for d in dn if d not in _SKIP and not d.startswith(".")]
        for f in fn:
            if not f.endswith(_EXT):
                continue
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, cwd).replace(os.sep, "/")
            try:
                loc = sum(1 for _ in open(full, encoding="utf-8", errors="ignore"))
            except OSError:
                continue
            if loc < 3:
                continue
            files.append({"p": rel, "g": _group(rel), "loc": loc, "full": full})
    files.sort(key=lambda x: -x["loc"])
    files = files[:MAX_FILES]
    # internal module index (for python import edges): dotted path + short name -> file
    mod2path = {}
    for x in files:
        if x["p"].endswith(".py"):
            m = x["p"][:-3].replace("/", ".")
            mod2path[m] = x["p"]
            mod2path[m.split(".")[-1]] = x["p"]
    for x in files:
        src = ""
        try:
            src = open(x["full"], encoding="utf-8", errors="ignore").read()
        except OSError:
            pass
        if x["p"].endswith(".py"):
            d, me, nm, im = _py_facts(src, x["p"], mod2path)
        else:
            d, me, nm, im = _scan_facts(src)
        x["defs"], x["methods"], x["names"], x["imports"] = d, me, nm, im
        x["abs"] = x.pop("full")          # absolute path so the code sidebar can open/edit it directly
    files.sort(key=lambda x: (x["g"], -x["loc"]))
    return files


# --------------------------------------------------------------------------- #
#  Per-repo galaxies + per-session constellations
#
#  The Map is two axes: WHICH repo (a full galaxy you can explore) and WHICH run
#  (a session's own file-touches replayed as a probe). A run that touches one repo
#  lights up that repo; a run that spans several repos becomes a constellation of
#  per-repo nebulae with the probe hopping between them.
# --------------------------------------------------------------------------- #

# tokens inside a bash command that look like a source file (so `cat x.py`, `sed -i … y.js`,
# `python pkg/run.py`, `> out.ts` all register as touches, not just the structured file tools)
_SRC_TOKEN = re.compile(
    r"~?[\w./+-]*\.(?:py|js|ts|tsx|jsx|mjs|cjs|go|rs|java|rb|php|html|css|scss|md|toml|c|cpp|cc|h|hpp|sh|sql|vue|svelte)\b")
_WRITE_HINT = re.compile(r"(^|\s)(sed\s+-i\b|tee\b|>>?)")


def git_root(path: str) -> str | None:
    """Nearest ancestor dir containing a .git (the repo the file belongs to), else None."""
    d = os.path.abspath(path if os.path.isdir(path) else os.path.dirname(path))
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def discover_repos(home: str, max_depth: int = 4, limit: int = 60) -> list[dict]:
    """Git repos under `home` (bounded depth/count) — one galaxy per project. Returns
    [{root, name}], repos not descended into (no nested-submodule noise)."""
    home = os.path.abspath(home)
    base = home.count(os.sep)
    out, names = [], set()
    for dp, dn, _fn in os.walk(home):
        dn[:] = [d for d in dn if d not in _SKIP and not d.startswith(".")]
        if dp == home:
            # ~/Library is macOS's per-user system store: ~86% of the directories under a
            # typical home, and never a place a user keeps a project. Walking it cost this
            # scan several seconds on a cold cache — past /api/repos' own timeout. Pruned at
            # the TOP LEVEL only, so a repo's own Library/ (Unity, Arduino) still resolves
            # by the normal rules.
            dn[:] = [d for d in dn if d not in _HOME_SKIP]
        if os.path.exists(os.path.join(dp, ".git")):
            nm = os.path.basename(dp)
            if nm not in names:
                out.append({"root": dp, "name": nm})
                names.add(nm)
            dn[:] = []                       # a repo is a leaf; don't walk into it
            continue
        if dp.count(os.sep) - base >= max_depth:
            dn[:] = []
        if len(out) >= limit:
            break
    out.sort(key=lambda r: r["name"].lower())
    return out


def _file_facts(abs_path: str, disp: str):
    """loc/defs/methods/names for a single file (no cross-file import graph — used for the
    session constellation, whose files may span repos)."""
    try:
        src = open(abs_path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return 1, 0, 0, []
    loc = src.count("\n") + 1
    if abs_path.endswith(".py"):
        d, me, nm, _ = _py_facts(src, disp, {})
    else:
        d, me, nm, _ = _scan_facts(src)
    return loc, d, me, nm


def session_touches(session: dict, fallback_cwd: str = "") -> list[tuple]:
    """Every source file a run touched, in order, as (order, action, abs_path). Reads the
    structured file tools AND file-looking tokens inside bash commands. action ∈ {edit,read,seen}."""
    cwd = session.get("cwd") or fallback_cwd or os.getcwd()
    out, order = [], 0
    for m in session.get("messages", []):
        for tc in (m.get("tool_calls") or []):
            name = (getattr(tc, "name", None) or (tc.get("name") if isinstance(tc, dict) else "") or "").lower()
            args = getattr(tc, "args", None) or (tc.get("args") if isinstance(tc, dict) else {}) or {}
            cands, act = [], "seen"
            f = args.get("path") or args.get("file_path") or args.get("file")
            if f:
                cands = [str(f)]
                act = ("edit" if any(k in name for k in ("edit", "write", "apply", "create"))
                       else "read" if "read" in name else "seen")
            elif args.get("command") and any(k in name for k in ("bash", "shell", "execute", "run")):
                cmd = str(args.get("command"))
                cands = _SRC_TOKEN.findall(cmd)
                act = "edit" if _WRITE_HINT.search(cmd) else "seen"
            for p in cands:
                ap = os.path.abspath(os.path.join(cwd, os.path.expanduser(p)))
                if ap.endswith(_EXT) and os.path.isfile(ap):
                    out.append((order, act, ap))
                    order += 1
    return out


_EDIT_CAP = 6000            # per hunk string; keep the /api/session_map payload sane


def session_edits(session: dict, fallback_cwd: str = "") -> dict:
    """The actual changes a run made, keyed by abs path: [{kind, old, new}] where kind is 'edit'
    (edit_file old→new) or 'write' (write_file, whole content). Lets the code sidebar show a real
    diff of what changed, not just the current source. Shell edits (sed -i) carry no hunk."""
    cwd = session.get("cwd") or fallback_cwd or os.getcwd()
    out = {}
    for m in session.get("messages", []):
        for tc in (m.get("tool_calls") or []):
            name = (getattr(tc, "name", None) or (tc.get("name") if isinstance(tc, dict) else "") or "").lower()
            args = getattr(tc, "args", None) or (tc.get("args") if isinstance(tc, dict) else {}) or {}
            f = args.get("path") or args.get("file_path") or args.get("file")
            if not f:
                continue
            ap = os.path.abspath(os.path.join(cwd, os.path.expanduser(str(f))))
            if "edit" in name and args.get("new_string") is not None:
                out.setdefault(ap, []).append({"kind": "edit",
                    "old": str(args.get("old_string", ""))[:_EDIT_CAP],
                    "new": str(args.get("new_string", ""))[:_EDIT_CAP]})
            elif ("write" in name or "create" in name) and args.get("content") is not None:
                out.setdefault(ap, []).append({"kind": "write", "old": "",
                    "new": str(args.get("content", ""))[:_EDIT_CAP]})
    return out


def session_map(session: dict, fallback_cwd: str = "", max_files: int = 160) -> dict:
    """A run's own constellation: the files it touched, keyed `<repo>/<subpath>` and grouped by
    repo (nebula), plus a single probe replaying the touches on a time axis. Files spanning many
    repos naturally yield many nebulae — that IS the cross-repo case. Each edited file carries the
    run's actual `edits` (old→new hunks) so the sidebar can diff what changed."""
    touches = session_touches(session, fallback_cwd)
    edmap = session_edits(session, fallback_cwd)
    files, events, repos = {}, [], {}
    for _order, act, ap in touches:
        root = git_root(ap) or os.path.dirname(ap)
        rname = os.path.basename(root) or "root"
        disp = rname + "/" + os.path.relpath(ap, root).replace(os.sep, "/")
        repos.setdefault(rname, root)
        events.append({"t": 0.5 + len(events) * 1.2, "act": act, "f": disp})
        if disp not in files and len(files) < max_files:
            loc, d, me, nm = _file_facts(ap, disp)
            files[disp] = {"p": disp, "g": rname, "loc": loc, "defs": d, "methods": me,
                           "names": nm, "imports": [], "abs": ap,
                           "edits": edmap.get(ap, []), "edited": bool(edmap.get(ap))}
    dur = max((e["t"] for e in events), default=1) + 1
    agents = [{"id": session.get("id", "run"), "hue": "#8FA6F2", "events": events}] if events else []
    return {"files": list(files.values()), "agents": agents, "dur": dur,
            "repos": [{"name": k, "root": v} for k, v in sorted(repos.items())]}


def read_source(cwd: str, rel: str, max_lines: int = 1200) -> str | None:
    """Source of `rel` (relative to cwd) for the code sidebar. Path-traversal guarded: the resolved
    path MUST stay inside cwd. Returns None if outside / missing / binary-ish."""
    # Path-traversal guard: resolve symlinks on BOTH the base and the joined path with realpath (not
    # abspath) so a symlink planted under cwd that points outside (e.g. -> /etc/passwd) resolves out
    # and is rejected before opening — mirroring read_abs. abspath alone would not follow the link.
    cwd = os.path.realpath(cwd)
    full = os.path.realpath(os.path.join(cwd, rel))
    if not (full == cwd or full.startswith(cwd + os.sep)):
        return None
    try:
        txt = open(full, encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    return "\n".join(txt.split("\n")[:max_lines])


def read_abs(abs_path: str, max_lines: int = 1200) -> str | None:
    """Source of an absolute path for the code sidebar when the Map spans many repos. Guarded: the
    real path must stay under the user's home and be a known source ext (this is a local, 127.0.0.1
    tool reading the user's own files; the guard just blocks /etc, symlink escapes and binaries)."""
    home = os.path.realpath(os.path.expanduser("~"))
    full = os.path.realpath(os.path.expanduser(abs_path))
    if not full.startswith(home + os.sep) or not full.endswith(_EXT):
        return None
    try:
        txt = open(full, encoding="utf-8", errors="ignore").read()
    except OSError:
        return None
    return "\n".join(txt.split("\n")[:max_lines])
