"""Web code-editor write-back: verify, then write — or reject and leave the file untouched.

The Map's code sidebar can edit a file and hit Commit. That never writes blindly: for Python we
first compile-check the new text, then run the file's *relevant tests* (the test files under the
repo that reference this module). Only if both pass do we keep the write; a failing test restores
the original bytes, so a bad edit can't land. Guarded to source files under the run's project root
(cwd) only (this is a local, 127.0.0.1, CSRF-gated tool editing the user's own code)."""
from __future__ import annotations

import os
import re
import subprocess
import sys

_EXTS = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".rb", ".html", ".css",
         ".md", ".toml", ".c", ".cpp", ".h", ".sh", ".json", ".yaml", ".yml", ".sql")
_TEST_TIMEOUT = 90


def _guard(cwd: str, path: str) -> str | None:
    """Resolve `path` to an absolute source file that stays under the run's project root (cwd).
    Constrained to cwd ONLY — the earlier home-wide allowance is dropped: a web-driven Commit must
    not be able to write arbitrary source anywhere under $HOME, which (together with the test runner)
    could plant and run attacker-chosen code. realpath resolves symlinks so an in-cwd symlink can't
    escape either."""
    cwd = os.path.realpath(cwd)
    full = os.path.realpath(os.path.join(cwd, os.path.expanduser(path)))
    inside = full == cwd or full.startswith(cwd + os.sep)
    if not inside or not full.endswith(_EXTS):
        return None
    return full


def _atomic_write(full: str, text: str):
    tmp = full + ".webedit.%d.tmp" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, full)


def relevant_tests(cwd: str, full: str) -> list[str]:
    """Test files (under cwd) that exercise this file — its own file if it's a test, tests/test_<name>.py,
    and any test file that names the module. Capped so a Commit stays quick."""
    cwd = os.path.realpath(cwd)
    base = os.path.splitext(os.path.basename(full))[0]
    rel = os.path.relpath(full, cwd).replace(os.sep, "/")
    # SECURITY: never execute the just-written file as its own test. Otherwise a web-driven Commit
    # could write a fresh test_*.py full of arbitrary code and have the harness run it (RCE). For an
    # edited test file we skip self-execution — the compile gate still rejects unparseable text.
    if base.startswith("test_") or base.endswith("_test"):
        return []
    hits, seen = [], set()
    for droot in ("tests", "test"):
        d = os.path.join(cwd, droot)
        if not os.path.isdir(d):
            continue
        # direct match first (tests/test_<name>.py), then any test file mentioning the module
        cand = os.path.join(d, "test_%s.py" % base)
        if os.path.isfile(cand):
            hits.append(cand); seen.add(cand)
        pat = re.compile(r"\b%s\b" % re.escape(base))
        modpath = re.compile(re.escape(rel.rsplit(".", 1)[0].replace("/", ".")))  # dotted module ref
        for f in sorted(os.listdir(d)):
            if not (f.startswith("test_") or f.endswith("_test.py")) or not f.endswith(".py"):
                continue
            fp = os.path.join(d, f)
            if fp in seen:
                continue
            try:
                src = open(fp, encoding="utf-8", errors="ignore").read()
            except OSError:
                continue
            if pat.search(src) or modpath.search(src):
                hits.append(fp); seen.add(fp)
            if len(hits) >= 3:
                break
    return hits[:3]


def _run_tests(cwd: str, tests: list[str]) -> tuple[int, str]:
    """Run the given test files. Prefer pytest (collects the def test_* fns); fall back to executing
    each file directly (collie's suites have __main__ runners). Returns (returncode, combined output)."""
    env = dict(os.environ, COLLIE_PROVIDER="mock", COLLIE_EMBED="hash", PYTHONUNBUFFERED="1")
    have_pytest = False
    try:
        import pytest  # noqa: F401
        have_pytest = True
    except Exception:
        have_pytest = False
    try:
        if have_pytest:
            from . import plat as _plat
            p = subprocess.run([sys.executable, "-m", "pytest", "-q", *tests], cwd=cwd, env=env,
                               capture_output=True, text=True, timeout=_TEST_TIMEOUT,
                               **_plat.no_window_kwargs())
            return p.returncode, (p.stdout + p.stderr)
        out, rc = "", 0
        for t in tests:
            p = subprocess.run([sys.executable, t], cwd=cwd, env=env, **_plat.no_window_kwargs(),
                               capture_output=True, text=True, timeout=_TEST_TIMEOUT)
            out += "$ %s\n%s%s\n" % (os.path.relpath(t, cwd), p.stdout, p.stderr)
            rc = rc or p.returncode
        return rc, out
    except subprocess.TimeoutExpired:
        return 1, "tests timed out after %ds" % _TEST_TIMEOUT


def write_checked(cwd: str, path: str, content: str) -> dict:
    """Verify then write. Stages: guard -> compile (py) -> write -> relevant tests -> keep or revert.
    Returns {ok, stage?, error?, tests, wrote}. On a test failure the original bytes are restored."""
    full = _guard(cwd, path)
    if full is None:
        return {"ok": False, "stage": "guard", "error": "path not allowed"}
    # tests live in the file's OWN project, which may differ from the server's cwd (repo picker /
    # cross-repo runs) — root at the file's git repo so relevant_tests/relpath are correct there.
    from . import codemap
    root = codemap.git_root(full) or os.path.realpath(cwd)
    rel = os.path.relpath(full, root).replace(os.sep, "/")

    # 1) syntax gate (python) — never write text that won't even parse
    if full.endswith(".py"):
        try:
            compile(content, full, "exec")
        except SyntaxError as e:
            return {"ok": False, "stage": "compile",
                    "error": "SyntaxError: %s (line %s)" % (e.msg, e.lineno), "tests": []}

    # 2) write (remembering the original so a failing test can roll back)
    orig = None
    if os.path.exists(full):
        try:
            orig = open(full, encoding="utf-8", errors="ignore").read()
        except OSError:
            orig = None
    if orig is not None and orig == content:
        return {"ok": True, "tests": [], "wrote": rel, "unchanged": True}
    _atomic_write(full, content)

    # 3) relevant tests — green keeps the write, red restores the original
    tests = relevant_tests(root, full)
    tnames = [os.path.relpath(t, root).replace(os.sep, "/") for t in tests]
    if tests:
        rc, out = _run_tests(root, tests)
        if rc != 0:
            if orig is not None:
                _atomic_write(full, orig)
            else:
                try:
                    os.remove(full)
                except OSError:
                    pass
            return {"ok": False, "stage": "test", "error": out[-4000:].strip(),
                    "tests": tnames, "reverted": True}
    return {"ok": True, "tests": tnames, "wrote": rel}
