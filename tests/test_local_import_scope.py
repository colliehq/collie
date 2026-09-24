"""A function-local import must run before every use of the name it binds.

Python makes a name local to the whole function once the function imports it anywhere, so a use
on a path that skipped the import raises UnboundLocalError, and a nested def that reads it raises
NameError. This has shipped three times: a do_GET that 500'd every request on `urllib`, the 0.29.1
/api/browser/status that 500'd on a closure reading `plat`, and the editor's test gate, which read
`_plat` on the no-pytest path and so neither verified nor reverted a write. None of them is caught
by a test that only takes the common path, so this reads the source instead.

A use counts as safe when an import (or assignment) of the name comes before it in the same
statement list or an enclosing one; when it sits in the true branch of an `if` (or a conditional
expression) with the same test as the `if` whose true branch holds the import, and that `if` is,
or sits inside, a later sibling of the import's; or when it is in an except clause, one that cannot catch the
import's own failure, of a `try` whose first statement is the import. It does not notice the
guard's variable being reassigned in between.
"""
import ast
import functools
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "harness"
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

# (file, function, name): why it is safe although the rules above cannot see it.
ALLOWED = {
    ("subscription_guard.py", "_check_claude_direct_credentials", "claude_oauth_expired"):
        "the import's except clause calls _deny, which always raises",
    ("primitives.py", "_live_code_slice", "runner_slice"):
        "imported and read under two separate `try: if external_worker:` blocks; external_worker "
        "is not reassigned after the import",
}


def _own_nodes(scope):
    stack = [scope.body] if isinstance(scope, ast.Lambda) else list(scope.body)
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, SCOPES):
            stack.extend(ast.iter_child_nodes(n))
            continue
        # A nested def stands in for what its body reads, but its decorators, default values and
        # (for a class) bases are evaluated here, in this scope, when the def runs.
        stack.extend(getattr(n, "decorator_list", []))
        if isinstance(n, ast.ClassDef):
            stack.extend(n.bases + [k.value for k in n.keywords])
        else:
            stack.extend(n.args.defaults + [d for d in n.args.kw_defaults if d is not None])


def _imported(stmt):
    if isinstance(stmt, ast.Import):
        return {(a.asname or a.name).split(".")[0] for a in stmt.names}
    if isinstance(stmt, ast.ImportFrom):
        return {a.asname or a.name for a in stmt.names}
    return set()


def _binds(stmt, name):
    if name in _imported(stmt):
        return True
    if isinstance(stmt, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in stmt.targets)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return stmt.name == name
    if isinstance(stmt, ast.Try):
        exits = (ast.Raise, ast.Return, ast.Continue, ast.Break)
        return (any(_binds(s, name) for s in stmt.body)
                and all(any(_binds(s, name) or isinstance(s, exits) for s in h.body)
                        for h in stmt.handlers))
    return False


def _free_reads(scope):
    """Names that nested `scope` (or a scope inside it) reads from the enclosing function."""
    loads, bound = set(), set()
    if not isinstance(scope, (ast.Lambda, ast.ClassDef)):
        a = scope.args
        bound |= {p.arg for p in a.posonlyargs + a.args + a.kwonlyargs + [a.vararg, a.kwarg] if p}
    for n in _own_nodes(scope):
        if isinstance(n, ast.Name):
            (loads if isinstance(n.ctx, ast.Load) else bound).add(n.id)
        elif isinstance(n, SCOPES):
            loads |= _free_reads(n)
        bound |= _imported(n)
    return loads - bound


def unsafe_uses(source, filename="<src>"):
    tree = ast.parse(source)
    parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    found = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        nodes = list(_own_nodes(fn))
        imports = [n for n in nodes if _imported(n)]
        names = set().union(*(_imported(n) for n in imports))
        if not names:
            continue
        reads = {}
        for n in nodes:
            if isinstance(n, ast.Name) and n.id in names and isinstance(n.ctx, ast.Load):
                reads.setdefault(n.id, []).append(n)
            elif isinstance(n, SCOPES):
                for name in _free_reads(n) & names:
                    reads.setdefault(name, []).append(n)
        for name in sorted(reads):
            guards = [g for n in imports if name in _imported(n)
                      for g in [_guard_of(n, fn, parent)] if g]
            for node in reads[name]:
                if not _dominated(node, fn, name, parent, guards):
                    found.append((filename, fn.name, name, node.lineno))
    return found


def _slot(stmt, parent):
    """(the statement list holding stmt, its index there), or None for a non-statement."""
    par = parent.get(stmt)
    for field in ("body", "orelse", "finalbody"):
        stmts = getattr(par, field, None)
        if isinstance(stmts, list) and stmt in stmts:
            return stmts, stmts.index(stmt)
    return None


def _guard_of(imp, fn, parent):
    """The `if` an import runs under: (its test, its statement list, its index), from the
    nearest enclosing `if` only, and only when the import is in that `if`'s true branch."""
    child = imp
    for par in _ancestors(imp, fn, parent):
        if isinstance(par, ast.If):
            if child not in par.body:
                return None
            slot = _slot(par, parent)
            return (ast.dump(par.test),) + slot if slot else None
        child = par
    return None


def _same_guard(stmt, test, guards, parent):
    """Does stmt sit in (or inside) a later sibling of an `if` with this same test that imported
    the name? Inside, because `for x in xs: if a: plat.x(x)` after `if a: import plat` is safe."""
    for g in guards:
        if g[0] != test:
            continue
        node = stmt
        while node in parent:
            slot = _slot(node, parent)
            if slot and slot[0] is g[1]:
                if slot[1] > g[2]:
                    return True
                break
            node = parent[node]
    return False


_CATCHES_IMPORT = {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}


def _may_catch_import_error(handler):
    if handler.type is None:
        return True
    kinds = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any((k.id if isinstance(k, ast.Name) else getattr(k, "attr", "")) in _CATCHES_IMPORT
               for k in kinds)


def _ancestors(node, fn, parent):
    while node is not fn:
        node = parent[node]
        yield node


def _dominated(node, fn, name, parent, guards):
    child = node
    for par in _ancestors(node, fn, parent):
        if (isinstance(par, ast.If) and child in par.body
                and _same_guard(par, ast.dump(par.test), guards, parent)):
            return True
        if isinstance(par, ast.IfExp) and child is par.body:
            stmt = par
            while stmt is not fn and _slot(stmt, parent) is None:
                stmt = parent[stmt]
            if stmt is not fn and _same_guard(stmt, ast.dump(par.test), guards, parent):
                return True
        if isinstance(par, ast.ExceptHandler) and not _may_catch_import_error(par):
            # The import ran first in the try; this clause cannot be what its failure lands in.
            tr = parent[par]
            if tr.body and _binds(tr.body[0], name):
                return True
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(par, field, None)
            if isinstance(stmts, list) and child in stmts:
                if any(_binds(s, name) for s in stmts[:stmts.index(child)]):
                    return True
        child = par
    return False


def test_checker_catches_the_shapes_that_shipped():
    branch = ("def f(a):\n"
              "    if a:\n"
              "        from . import plat as _plat\n"
              "        return _plat.x()\n"
              "    return _plat.y()\n")
    closure = ("def f(a):\n"
               "    if a:\n"
               "        from . import plat\n"
               "    def g():\n"
               "        return plat.z()\n"
               "    return g()\n")
    before = ("import urllib.parse\n"
              "def f(self):\n"
              "    p = urllib.parse.urlparse(self.path)\n"
              "    if p:\n"
              "        import urllib.request\n")
    assert [u[3] for u in unsafe_uses(branch)] == [5]
    assert [u[3] for u in unsafe_uses(closure)] == [4]
    assert [u[3] for u in unsafe_uses(before)] == [3]


def test_checker_is_not_fooled_by_a_look_alike_guard():
    shapes = {
        "import in else, use under if": (
            "def f(a):\n"
            "    if a:\n"
            "        pass\n"
            "    else:\n"
            "        import plat\n"
            "    if a:\n"
            "        plat.x()\n", [7]),
        "same test, but nested one level down": (
            "def f(a, b):\n"
            "    if a:\n"
            "        if b:\n"
            "            import plat\n"
            "    if b:\n"
            "        plat.x()\n", [6]),
        "use before the guarded import": (
            "def f(a):\n"
            "    if a:\n"
            "        plat.x()\n"
            "    if a:\n"
            "        import plat\n", [3]),
        "except that catches the failed import": (
            "def f():\n"
            "    try:\n"
            "        import yaml\n"
            "    except ImportError:\n"
            "        yaml.x()\n", [5]),
        "decorator and default value": (
            "def f(a):\n"
            "    if a:\n"
            "        import plat\n"
            "    @plat.deco\n"
            "    def g(p=plat.x()):\n"
            "        return p\n", [4, 5]),
    }
    for what, (src, lines) in shapes.items():
        assert sorted(u[3] for u in unsafe_uses(src)) == lines, what


def test_checker_accepts_the_safe_shapes():
    ok = ("def f(a, s):\n"
          "    from . import plat\n"
          "    if a:\n"
          "        return plat.x()\n"
          "    if a.b != 'x':\n"
          "        from .m import t\n"
          "    try:\n"
          "        import datetime as _dt\n"
          "        n = int(s)\n"
          "    except ValueError:\n"
          "        n = _dt.now()\n"
          "    if a.b != 'x':\n"
          "        return t(n)\n"
          "    for x in s:\n"
          "        if a.b != 'x':\n"
          "            t(x)\n"
          "    def g(plat):\n"
          "        return plat\n")
    assert unsafe_uses(ok) == []


@functools.lru_cache(maxsize=1)
def _harness_findings():
    found = []
    for path in sorted(ROOT.rglob("*.py")):
        found += unsafe_uses(path.read_text(encoding="utf-8"), path.relative_to(ROOT).as_posix())
    return tuple(found)


def test_no_local_import_is_read_before_it_runs():
    found = [u for u in _harness_findings() if u[:3] not in ALLOWED]
    assert not found, "\n".join(
        "%s:%d %s() reads %r on a path that may not have imported it" % (f, line, fn, name)
        for f, fn, name, line in found)


def test_allowlist_has_no_stale_entries():
    stale = set(ALLOWED) - {u[:3] for u in _harness_findings()}
    assert not stale, stale
