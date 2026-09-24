"""One byte a child writes that is not valid text must not cost the whole output.

With text=True and no errors=, a single undecodable byte is fatal to the read: on Windows the
reader thread dies and run() returns stdout=None with returncode 0, on POSIX run() raises. So one
latin-1 source file among the matches made code search report no matches at all, and a non-UTF-8
edit made the per-turn worktree diff None, which _tree_empty then crashed on.
"""
import ast
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import codeindex, loop, plat  # noqa: E402

ROOT = Path(__file__).resolve().parents[1] / "harness"
_SPAWN = {"run", "Popen", "check_output", "check_call", "call"}


def _text_captures(tree):
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in _SPAWN and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "subprocess"):
            continue
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        text = kw.get("text")
        texty = ((isinstance(text, ast.Constant) and text.value)
                 or "universal_newlines" in kw or "encoding" in kw)
        captures = (any(k in kw for k in ("capture_output", "stdout", "stderr"))
                    or n.func.attr == "check_output")
        if texty and captures:
            yield n, kw


def test_every_text_capture_says_what_to_do_with_bad_bytes():
    missing = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        missing += ["%s:%d" % (path.relative_to(ROOT).as_posix(), n.lineno)
                    for n, kw in _text_captures(tree) if "errors" not in kw]
    assert not missing, ("pass errors= (usually \"replace\") to these text-mode subprocess "
                         "calls: " + ", ".join(missing))


def test_mcp_stdio_is_utf8_whatever_the_code_page():
    # JSON-RPC over stdio is UTF-8 by the MCP spec; the locale default on Windows is the ANSI
    # code page, which garbles non-ASCII both ways (or cannot encode it at all).
    tree = ast.parse((ROOT / "mcpclient.py").read_text(encoding="utf-8"))
    popens = [kw for n, kw in _text_captures(tree) if n.func.attr == "Popen"]
    assert popens and all(isinstance(kw.get("encoding"), ast.Constant)
                          and kw["encoding"].value == "utf-8" for kw in popens)


def _search_tool_available():
    if plat.is_windows() and not plat.posix_shell():
        return False
    return bool(shutil.which("rg") or shutil.which("grep"))


def test_code_search_survives_a_file_that_is_not_utf8(tmp_path):
    if not _search_tool_available():
        pytest.skip("no rg/grep to search with")
    (tmp_path / "good.py").write_text("def parse_config():\n    return 1\n", encoding="utf-8")
    (tmp_path / "legacy.py").write_bytes(b"# caf\xe9 parse_config helper (latin-1)\nx = 1\n")
    found = codeindex._grep_matches(str(tmp_path), ["parse_config"])
    assert "good.py" in found, found               # was {}: the UTF-8 match was lost too
    assert found["good.py"][1] == 1
    assert "legacy.py" in found


def _git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), "-c", "user.name=t", "-c", "user.email=t@t",
                           "-c", "core.autocrlf=false"] + list(args),
                          capture_output=True, check=True)


@pytest.mark.parametrize("base, edited", [
    (b"x = 1\n", b"x = 1\n# na\xefve (latin-1)\n"),     # was None on Windows, then a crash
    (b"x = 1\n", b"x = 1\ny = 2\n"),                     # plain LF: never re-applied on Windows
    (b"x = 1\r\n", b"x = 1\r\ny = 2\r\n"),               # CRLF: folded to LF on read (POSIX)
], ids=["latin-1", "lf", "crlf"])
def test_worktree_diff_round_trips_exactly(tmp_path, base, edited):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    f = tmp_path / "mod.py"
    f.write_bytes(base)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    f.write_bytes(edited)

    diff = loop._tree_diff(str(tmp_path))
    assert diff and "mod.py" in diff
    assert loop._tree_empty(str(tmp_path)) is False
    # The white-flag guard re-applies this diff after a revert: the bytes must come back exactly.
    _git(tmp_path, "checkout", "--", "mod.py")
    assert f.read_bytes() == base
    assert loop._tree_empty(str(tmp_path)) is True
    assert loop._apply_diff(str(tmp_path), diff)
    assert f.read_bytes() == edited
