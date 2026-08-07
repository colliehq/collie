"""Post-edit diagnostics — extend collie's executed-verification identity to every language.

collie's edit_file already blocks Python syntax breakage (ast.parse). This adds a language-
agnostic, DEPENDENCY-FREE syntax check on the edited file and feeds any diagnostics straight
back into the edit result, so a broken non-Python edit self-corrects the same way — the
assert-verify philosophy one level down, keyless.

Deliberately SYNTAX-only (checks that don't need the whole project to resolve): node --check,
gofmt -e, ruby -c, php -l, bash -n, luac -p, plus pure-python JSON/YAML parsing and pyflakes
for Python name errors. Cross-file/type checks (tsc --noEmit, cargo check, gcc) are noisy on a
single file and belong to user-configured HOOKS, not this cheap always-on pass. Any checker
whose binary is absent simply no-ops — never a false alarm from a missing tool.
"""
import json
import os
import shutil
import subprocess

# ext -> argv template ({p} = file path); only run if the binary (argv[0]) is on PATH.
_CHECKERS = {
    ".js": ["node", "--check", "{p}"], ".jsx": ["node", "--check", "{p}"],
    ".mjs": ["node", "--check", "{p}"], ".cjs": ["node", "--check", "{p}"],
    ".go": ["gofmt", "-e", "{p}"],
    ".rb": ["ruby", "-c", "{p}"],
    ".php": ["php", "-l", "{p}"],
    ".sh": ["bash", "-n", "{p}"], ".bash": ["bash", "-n", "{p}"],
    ".lua": ["luac", "-p", "{p}"],
}


def _run(argv, cwd):
    try:
        from . import plat
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=20,
                           **plat.no_window_kwargs())
    except Exception:
        return ""
    if p.returncode == 0:
        return ""
    msg = ((p.stderr or "") + (p.stdout or "")).strip()
    return msg[:800]


def diagnose(path, cwd):
    """Return a capped diagnostics string for the edited file, or '' if clean / unsupported."""
    ext = os.path.splitext(path)[1].lower()

    if ext in (".json",):
        try:
            with open(path, encoding="utf-8") as f:
                json.load(f)
            return ""
        except Exception as e:
            return "invalid JSON: %s" % e

    if ext in (".yaml", ".yml"):
        try:
            import yaml
        except Exception:
            return ""
        try:
            with open(path, encoding="utf-8") as f:
                yaml.safe_load(f)
            return ""
        except Exception as e:
            return "invalid YAML: %s" % str(e)[:400]

    if ext == ".py":                       # ast already gates syntax; add name-level checks
        if shutil.which("pyflakes"):
            return _run(["pyflakes", path], cwd)
        return ""

    tmpl = _CHECKERS.get(ext)
    if not tmpl or not shutil.which(tmpl[0]):
        return ""
    return _run([a.replace("{p}", path) for a in tmpl], cwd)
