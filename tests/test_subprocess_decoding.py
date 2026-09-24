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
import types

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


def _any_text_captures(tree):
    """Also calls through an alias (`runner=subprocess.run`, a local `_run`): any call that asks
    for text mode and captures output is a subprocess read for this purpose."""
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call):
            continue
        kw = {k.arg: k.value for k in n.keywords if k.arg}
        text = kw.get("text")
        if (((isinstance(text, ast.Constant) and text.value) or "universal_newlines" in kw)
                and any(k in kw for k in ("capture_output", "stdout", "stderr"))):
            yield n, kw


def test_every_text_capture_says_what_to_do_with_bad_bytes():
    missing = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        seen = set()
        for n, kw in list(_text_captures(tree)) + list(_any_text_captures(tree)):
            if "errors" not in kw and n.lineno not in seen:
                seen.add(n.lineno)
                missing.append("%s:%d" % (path.relative_to(ROOT).as_posix(), n.lineno))
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


def _code_page(monkeypatch, name):
    # What subprocess's text mode encodes stdin with and decodes output with, when not told.
    import locale
    monkeypatch.setattr(locale, "getencoding", lambda: name)


def test_agent_cli_stdin_keeps_text_the_code_page_cannot_spell(monkeypatch, tmp_path):
    # errors="replace" also governs what is WRITTEN to stdin: under 1252 the task text reached
    # the agent CLI (a node program, which reads UTF-8) with "?" for every Chinese character.
    from harness import swe
    _code_page(monkeypatch, "cp1252")
    text = "修复解析器 🙂 café"
    r = swe._run_cli([sys.executable, "-c",
                      "import sys; sys.stdout.write(sys.stdin.buffer.read().hex())"],
                     str(tmp_path), timeout=60, stdin_text=text)
    assert bytes.fromhex(r.stdout.strip()).decode("utf-8") == text


def test_worktree_diff_ignores_the_users_diff_config(tmp_path):
    if not shutil.which("git"):
        pytest.skip("git not installed")
    f = tmp_path / "mod.py"
    f.write_bytes(b"x = 1\n")
    _git(tmp_path, "init", "-q")
    for key, value in (("core.autocrlf", "false"), ("color.diff", "always"),
                       ("diff.noprefix", "true"), ("diff.mnemonicPrefix", "true")):
        _git(tmp_path, "config", key, value)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    f.write_bytes(b"x = 1\ny = 2\n")
    diff = loop._tree_diff(str(tmp_path))
    assert "\x1b[" not in diff and "--- a/mod.py" in diff
    _git(tmp_path, "checkout", "--", "mod.py")
    assert loop._apply_diff(str(tmp_path), diff)
    assert f.read_bytes() == b"x = 1\ny = 2\n"


def test_swe_prediction_patch_is_exact_or_refused(tmp_path):
    # The prediction is scored by applying it: a CRLF folded to LF, or a byte turned into U+FFFD,
    # would score a patch the agent never wrote.
    if not shutil.which("git"):
        pytest.skip("git not installed")
    from harness import swe
    f = tmp_path / "mod.py"
    f.write_bytes(b"x = 1\r\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    f.write_bytes(b"x = 1\r\ny = 2\r\n")
    patch = swe.make_patch(str(tmp_path))
    assert "+y = 2\r\n" in patch
    _git(tmp_path, "reset", "-q", "--hard")
    subprocess.run(["git", "-C", str(tmp_path), "apply", "-"], input=patch.encode("utf-8"),
                   check=True, capture_output=True)
    assert f.read_bytes() == b"x = 1\r\ny = 2\r\n"
    # Not UTF-8: kept, with U+FFFD for those bytes and a warning. Refusing it left the
    # instance with no prediction on every retry.
    f.write_bytes(b"x = 1\r\n# caf\xe9\r\n")
    patch = swe.make_patch(str(tmp_path))
    assert "+# caf\ufffd\r\n" in patch


def test_run_in_env_hands_the_container_the_exact_diff(tmp_path, monkeypatch):
    # The patch file was written in text mode: CRLF on Windows, so `git apply` in the container
    # never applied the edits the model asked to test.
    if not shutil.which("git"):
        pytest.skip("git not installed")
    from harness import tools
    f = tmp_path / "mod.py"
    f.write_bytes(b"x = 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "core.autocrlf", "false")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    f.write_bytes(b"x = 1\ny = 2\n")
    expected = subprocess.run(["git", "-C", str(tmp_path), "diff", "--no-color", "--no-ext-diff",
                               "--binary"], capture_output=True, check=True).stdout
    handed = []
    real_run = subprocess.run

    def run(argv, *a, **kw):
        if argv and argv[0] == "docker":
            host = argv[argv.index("-v") + 1].rsplit(":/tmp/e.patch", 1)[0]
            handed.append(open(host, "rb").read())
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        return real_run(argv, *a, **kw)

    monkeypatch.setenv("COLLIE_E2E_IMAGE", "img")
    monkeypatch.setattr(tools.subprocess, "run", run)
    tools.RunInEnvTool().run({"command": "python -c 'print(1)'"},
                             types.SimpleNamespace(cwd=str(tmp_path)))
    assert handed and handed[-1] == expected and b"\r\n" not in handed[-1]


def test_the_critic_reads_source_hunks_not_base85(tmp_path):
    # The critic sees the first 9000 characters; a changed binary file ahead of the source used
    # to fill them with base85. The restore copy still carries it.
    if not shutil.which("git"):
        pytest.skip("git not installed")
    (tmp_path / "a.bin").write_bytes(bytes(range(256)) * 40)
    (tmp_path / "src.py").write_bytes(b"x = 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    (tmp_path / "a.bin").write_bytes(bytes(reversed(range(256))) * 40)
    (tmp_path / "src.py").write_bytes(b"x = 2\n")
    assert "GIT binary patch" in loop._tree_diff(str(tmp_path))
    reading = loop._tree_diff(str(tmp_path), binary=False)
    assert "GIT binary patch" not in reading and "+x = 2" in reading[:9000]
