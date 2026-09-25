"""Post-edit syntax diagnostics, at their own boundary.

edit_file appends whatever diagnose() says to the edit result, so a false alarm tells the model a
correct file is broken. On Windows every .sh file got one: which() found Git Bash, but the bare
name "bash" was run, and CreateProcess searches System32 first -- WSL's bash.exe, which cannot see
the file ("/bin/bash: C:Users...ok.sh: No such file or directory").
"""
import shutil

import pytest

from harness import lint


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_json_is_checked_without_any_tool(tmp_path):
    assert lint.diagnose(_write(tmp_path, "ok.json", '{"a": 1}'), str(tmp_path)) == ""
    assert "invalid JSON" in lint.diagnose(_write(tmp_path, "bad.json", '{"a": }'), str(tmp_path))


def test_an_unsupported_or_missing_checker_says_nothing(tmp_path, monkeypatch):
    assert lint.diagnose(_write(tmp_path, "notes.txt", "(("), str(tmp_path)) == ""
    monkeypatch.setattr(lint.shutil, "which", lambda name: None)
    assert lint.diagnose(_write(tmp_path, "bad.js", "function ("), str(tmp_path)) == ""


@pytest.mark.parametrize("ext, good, bad, tool", [
    (".sh", "echo hi\n", "if then fi (\n", "bash"),
    (".js", "const a = 1;\n", "function (\n", "node"),
])
def test_a_correct_file_is_clean_and_a_broken_one_is_not(tmp_path, ext, good, bad, tool):
    if not shutil.which(tool):
        pytest.skip("%s is not installed" % tool)
    assert lint.diagnose(_write(tmp_path, "ok" + ext, good), str(tmp_path)) == ""
    out = lint.diagnose(_write(tmp_path, "bad" + ext, bad), str(tmp_path))
    assert out and "No such file" not in out, out
