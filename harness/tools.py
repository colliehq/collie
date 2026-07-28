"""Tool system — two-tier registry (always-on vs deferred).

Every tool declares a `tier`. Only `always` tools ship their JSON schema to the
model each turn; `deferred` tools are advertised by name and their schema is
fetched on demand (Claude Code's ToolSearch pattern). v1 keeps a lean always-on
core; the seam for deferred/MCP tools exists but isn't heavily populated yet.
"""
from __future__ import annotations
import itertools
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass

from . import plat

_SHIM_DIR = None


def _shim_env():
    """Env for the bash tool with a `python` -> `python3` shim on PATH, IF `python` is absent (this
    box, like modern Debian/WSL, only ships `python3`). Without it the model's `python -c` post-edit
    reproductions fail 'python: not found', wasting a turn AND falsely failing the verification gate."""
    global _SHIM_DIR
    if shutil.which("python"):
        return None                                    # already resolves — inherit os.environ
    py3 = shutil.which("python3")
    if not py3:
        return None
    if _SHIM_DIR is None:
        try:
            _SHIM_DIR = tempfile.mkdtemp(prefix="collie_shim_")
            os.symlink(py3, os.path.join(_SHIM_DIR, "python"))
        except OSError:
            _SHIM_DIR = None
            return None
    env = dict(os.environ)
    env["PATH"] = _SHIM_DIR + os.pathsep + env.get("PATH", "")
    return env


# Models trained on Claude Code emit `file_path` where collie's tools want `path`; pi burns a turn
# on this too (edit.ts:175). Only aliases with a real motivating model go here (annotation discipline).
_ARG_ALIASES = {"path": ("file_path",)}


def repair_args(args, schema):
    """Fix known model quirks in tool arguments BEFORE validation, returning (repaired, notes).
    pi's prepareArguments generalized (edit.ts:100-117). Repairs, each noted for the transcript:
      • JSON-string-wrapped array/object args (Opus 4.6 / GLM-5.1 send nested arrays as strings)
      • required-key aliases (file_path -> path)
    A non-dict becomes {} (note 'non_dict'). Never overwrites an existing key; a string that isn't
    valid JSON, or whose parsed type doesn't match the declared type, is left for the tool's own
    error. Well-formed args return unchanged (identity) with no notes — no over-repair churn."""
    if not isinstance(args, dict):
        return {}, ["non_dict"]
    notes = []
    props = (schema or {}).get("properties", {}) or {}
    out = dict(args)
    for key, spec in props.items():
        want = spec.get("type") if isinstance(spec, dict) else None
        if want in ("array", "object") and isinstance(out.get(key), str):
            try:
                parsed = json.loads(out[key])
            except Exception:
                continue                         # leave it — the tool's own error still fires
            if (want == "array" and isinstance(parsed, list)) or \
               (want == "object" and isinstance(parsed, dict)):
                out[key] = parsed
                notes.append("json_str:%s" % key)
    for req in (schema or {}).get("required", []) or []:
        if req in out and out[req] not in (None, ""):
            continue
        for alias in _ARG_ALIASES.get(req, ()):  # fill a missing required key from a known alias
            if alias in out and alias not in props and out.get(alias) not in (None, ""):
                out[req] = out.pop(alias)
                notes.append("alias:%s->%s" % (alias, req))
                break
    return (out, notes) if notes else (args, [])


def _need_str(args, key):
    """Validate a required string arg, returning (value, None) or (None, 'ERROR: …'). Tools should
    return a clean ERROR the model can act on, not KeyError/TypeError (which relies on the caller's
    try/except and surfaces as an opaque 'tool X failed: KeyError')."""
    v = args.get(key) if isinstance(args, dict) else None
    if v is None or v == "":
        return None, "ERROR: missing required arg '%s'" % key
    if not isinstance(v, str):
        return None, "ERROR: arg '%s' must be a string, got %s" % (key, type(v).__name__)
    return v, None


@dataclass
class ToolCtx:
    cwd: str
    project: str
    memory: object          # SqliteMemory
    recorder: object = None


class Tool:
    name = ""
    description = ""
    tier = "always"         # "always" | "deferred"
    schema: dict = {}       # JSON schema of args (Anthropic tool input_schema)

    def run(self, args: dict, ctx: ToolCtx) -> str:
        raise NotImplementedError

    def provider_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema or {"type": "object", "properties": {}}}


# --------------------------------------------------------------------------- #
class ReadFileTool(Tool):
    name = "read_file"
    description = ("Read a UTF-8 text file with 1-based LINE NUMBERS. Args: path; optional "
                   "offset (1-based first line, for paging large files), limit (max lines, "
                   "default 800), max_bytes. Use offset/limit to page a big file instead of "
                   "dumping it all. The line-number prefix is for reference only — do NOT "
                   "include it in edit_file's old_string.")
    schema = {"type": "object", "properties": {
        "path": {"type": "string"}, "offset": {"type": "integer"},
        "limit": {"type": "integer"}, "max_bytes": {"type": "integer"}},
        "required": ["path"]}

    def run(self, args, ctx):
        path, _perr = _need_str(args, "path")
        if _perr:
            return _perr
        p = os.path.join(ctx.cwd, path) if not os.path.isabs(path) else path
        cap = int(args.get("max_bytes", 100000))
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                data = f.read(cap + 1)
        except Exception as e:
            return "ERROR reading %s: %s" % (args["path"], e)
        byte_capped = len(data) > cap
        if byte_capped:
            data = data[:cap]
        if data == "":
            return "(empty file)"
        lines = data.split("\n")
        total = len(lines)
        # line-based paging (offset/limit) — the truncation note used to PROMISE this but the
        # schema had no offset param, a dead end that forced full-file re-dumps.
        offset = max(1, int(args.get("offset", 1)))
        limit = int(args.get("limit", 800))
        seg = lines[offset - 1: offset - 1 + limit]
        if not seg:
            return "(no lines at offset %d; file has %d lines)" % (offset, total)
        body = "\n".join("%6d\t%s" % (offset + i, ln) for i, ln in enumerate(seg))
        end = offset - 1 + len(seg)
        if end < total:                    # more lines WITHIN the read window — offset paging works
            body += ("\n…[showing lines %d-%d of %d%s; re-read with offset=%d to continue]"
                     % (offset, end, total, " (byte-capped)" if byte_capped else "", end + 1))
        elif byte_capped:                  # shown every line we HAVE, but bytes remain past the cap:
            # offset paging can't reach them (those lines aren't in `data`) — must widen max_bytes.
            body += ("\n…[byte-capped at %d bytes — MORE FILE REMAINS beyond the cap; re-read with a "
                     "larger max_bytes (or a narrower line range) to see the rest]" % cap)
        return body


def _strip_linenums(text):
    """Defensive: if the model pasted read_file's `%6d\\t` line-number prefix into an edit's
    old_string, strip it so the exact match can still land."""
    import re
    out = [re.sub(r"^\s*\d+\t", "", ln) for ln in text.split("\n")]
    return "\n".join(out)


class WriteFileTool(Tool):
    name = "write_file"
    description = "Write text to a file (overwrites). Args: path, content."
    schema = {"type": "object", "properties": {
        "path": {"type": "string"}, "content": {"type": "string"}},
        "required": ["path", "content"]}

    def run(self, args, ctx):
        path, _perr = _need_str(args, "path")
        if _perr:
            return _perr
        p = os.path.join(ctx.cwd, path) if not os.path.isabs(path) else path
        content = args.get("content")
        if not isinstance(content, str):
            return "ERROR: arg 'content' must be a string, got %s" % type(content).__name__
        try:
            _snapshot(ctx.project, p)          # checkpoint prior state so `undo` can restore it
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            _touch_index(ctx.cwd)
            return "wrote %d bytes to %s%s" % (len(content), path, _diag_suffix(p, ctx.cwd))
        except Exception as e:
            return "ERROR writing %s: %s" % (path, e)


def _diag_suffix(path, cwd):
    """Language-agnostic post-write diagnostics appended to an edit/write result (verification
    identity extended to every language). Best-effort: no checker -> empty."""
    try:
        from .lint import diagnose
        d = diagnose(path, cwd)
    except Exception:
        d = ""
    return ("\n⚠ diagnostics (fix if unintended):\n" + d) if d else ""


def _touch_index(cwd):
    """Best-effort: drop the cached code index after a write so the next code_search / coverage
    gate reflects the new content (and newly-created files) instead of pre-edit line numbers."""
    try:
        from .codeindex import invalidate
        invalidate(cwd)
    except Exception:
        pass


def _snapshot(project, abspath):
    """Best-effort pre-mutation checkpoint so the `undo` tool can restore this file."""
    try:
        from .checkpoint import record
        record(project, abspath)
    except Exception:
        pass


def _bridge_live_safe():
    """True iff a browser bridge is live with an extension connected (fast localhost probe). Wrapped
    so a probe/import failure never breaks registry construction."""
    try:
        from .browserbridge import _bridge_live
        return _bridge_live()
    except Exception:
        return False


# Unicode punctuation models silently emit that NFKC does NOT fold: curly quotes, the 7 dash
# variants + minus sign, and stray BOM chars. Folding these (for MATCHING only) rescues the DeepSeek
# old_string-mis-quote class — collie's documented residual empty-patch mode — without ever writing
# the folded bytes back (the span indexes the ORIGINAL string, so untouched lines keep their bytes).
_FOLD_MAP = {0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'",
             0x201C: '"', 0x201D: '"', 0x201E: '"', 0x201F: '"',
             0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2015: "-",
             0x2212: "-", 0xFEFF: None}


def _fold(line):
    """NFKC (folds the whole NBSP/space + fullwidth class) + quote/dash fold + BOM strip, then
    trim. MATCHING ONLY — never a source of written bytes."""
    return unicodedata.normalize("NFKC", line).translate(_FOLD_MAP).strip()


def _flex_find(s, old, norm=str.strip):
    """Char span (start, end) of a UNIQUE block in `s` whose lines equal `old`'s lines under the
    `norm` per-line normalizer (default: strip whitespace; pass `_fold` for unicode tolerance), else
    None. Requires uniqueness so we never edit the wrong place; the span indexes the ORIGINAL `s`
    (whole lines, end includes the last matched line's newline) so byte preservation is structural."""
    s_lines = s.split("\n")
    sn = [norm(ln) for ln in s_lines]                 # normalize each source line ONCE (not per-window)
    o_lines = [norm(ln) for ln in old.split("\n")]
    while o_lines and o_lines[-1] == "":
        o_lines.pop()
    while o_lines and o_lines[0] == "":
        o_lines.pop(0)
    if not o_lines:
        return None
    hits = []
    for i in range(len(s_lines) - len(o_lines) + 1):
        if all(sn[i + j] == o_lines[j] for j in range(len(o_lines))):
            hits.append(i)
    if len(hits) != 1:
        return None
    i = hits[0]
    start = sum(len(ln) + 1 for ln in s_lines[:i])
    end = sum(len(ln) + 1 for ln in s_lines[:i + len(o_lines)])
    return (start, min(end, len(s)))


class EditFileTool(Tool):
    name = "edit_file"
    description = ("Replace an exact, unique substring in a file (targeted edit — "
                   "safer than rewriting the whole file). Args: path, old_string, new_string.")
    schema = {"type": "object", "properties": {
        "path": {"type": "string"}, "old_string": {"type": "string"},
        "new_string": {"type": "string"}}, "required": ["path", "old_string", "new_string"]}

    def run(self, args, ctx):
        path, _perr = _need_str(args, "path")
        if _perr:
            return _perr
        p = os.path.join(ctx.cwd, path) if not os.path.isabs(path) else path
        try:
            with open(p, "rb") as f:
                raw = f.read()
            s = raw.decode("utf-8")
        except Exception as e:
            return "ERROR reading %s: %s" % (path, e)
        # Strip a leading BOM for matching + the AST gate, restore it on write. Without this, a
        # BOM'd .py file was UNEDITABLE: ast.parse raises "invalid non-printable character U+FEFF",
        # so the syntax gate rejected every edit with a misleading "would break Python syntax".
        bom = s.startswith("﻿")
        if bom:
            s = s[1:]
        # Preserve the file's line-ending style. Reading in text mode would fold CRLF->LF, and
        # writing back would then rewrite EVERY line ending (a huge spurious diff on Windows-origin
        # files). Match on a normalized \n copy, restore the original style on write.
        nl = "\r\n" if b"\r\n" in raw else "\n"
        if nl == "\r\n":
            s = s.replace("\r\n", "\n")
        old, new = args.get("old_string"), args.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return "ERROR: edit_file requires string 'old_string' and 'new_string'"
        if old == new:                    # a no-op "edit" changes nothing but used to
            return ("ERROR: new_string is identical to old_string in %s — no change made; "
                    "make a real edit" % path)   # report success + set did_edit
        cnt = s.count(old)
        new_content, how = None, ""
        if cnt == 1:
            new_content = s.replace(old, new)
        elif cnt > 1:
            return "ERROR: old_string appears %d times in %s; add surrounding context to make it unique" % (cnt, args["path"])
        else:
            # exact match failed — a very common LLM slip is getting leading/trailing whitespace
            # slightly wrong. Retry tolerantly: match whole lines by stripped content, ONLY if
            # unique (else we'd edit the wrong place).
            # ladder: whitespace-tolerant → unicode-tolerant (curly quotes / dashes / NBSP) →
            # line-number-prefix strip. Each rung requires a UNIQUE match and splices into the
            # ORIGINAL s, so untouched lines always keep their exact bytes.
            span = _flex_find(s, old)
            how = " (whitespace-tolerant match)"
            if not span:
                span = _flex_find(s, old, norm=_fold)
                how = " (unicode-tolerant match)"
            if span:
                # match the replaced segment's trailing-newline state, so flex-editing the LAST
                # line of a file with no final newline doesn't silently add one.
                seg_nl = s[span[0]:span[1]].endswith("\n")
                repl = new
                if seg_nl and not repl.endswith("\n"):
                    repl += "\n"
                elif not seg_nl and repl.endswith("\n"):
                    repl = repl[:-1]
                new_content = s[:span[0]] + repl + s[span[1]:]
            else:
                # defensive: the model may have pasted read_file's line-number prefix
                old2 = _strip_linenums(old)
                if old2 != old and s.count(old2) == 1:
                    new_content, how = s.replace(old2, new), " (stripped line-number prefix)"
                else:
                    return "ERROR: old_string not found in %s (read the file first)" % args["path"]
        # A1: lint-in-the-edit-loop — reject an edit that would BREAK Python syntax instead of
        # cementing it (the measured edit-guard lever; collie's diagnosed weakness is edit
        # correctness — a broken edit otherwise passes silently to the grader).
        if p.endswith(".py"):
            import ast
            try:
                ast.parse(new_content)
            except SyntaxError as e:
                return ("ERROR: this edit would break Python syntax in %s — %s (line %s). The "
                        "file was NOT modified; fix new_string and retry." % (args["path"], e.msg, e.lineno))
        _snapshot(ctx.project, p)  # checkpoint prior state so `undo` can restore it
        # utf-8-sig re-emits the BOM the file started with (stdlib); newline=nl restores CRLF.
        with open(p, "w", encoding="utf-8-sig" if bom else "utf-8", newline=nl) as f:
            f.write(new_content)
        _touch_index(ctx.cwd)      # post-edit code_search must see the new line numbers
        return "edited %s%s%s" % (args["path"], how, _diag_suffix(p, ctx.cwd))


# Per-user spill dir: fold the uid into the name so two users on a shared host can't be steered
# onto the same predictable path (which would let one pre-create/symlink the other's spill files).
_SPILL_UID = getattr(os, "geteuid", lambda: 0)()
_SPILL_DIR = os.path.join(tempfile.gettempdir(), "collie-spill-%d" % _SPILL_UID)
_spill_seq = itertools.count(1)
_spill_swept = False


def _spill_full_output(out):
    """Persist FULL bash output to a file when the model-facing result is truncated to the tail, so
    the model can grep/read_file it instead of re-running an expensive command. Returns the path or
    None. Best-effort — a disk error must never fail the tool call."""
    global _spill_swept
    try:
        os.makedirs(_SPILL_DIR, mode=0o700, exist_ok=True)
        # Security: /tmp is world-writable and shared. Before writing, confirm the spill dir is
        # really ours — owned by the current uid and mode 0700 — using lstat (no symlink follow).
        # If an attacker pre-created it (e.g. as a symlink or a dir they own) to redirect our
        # writes, ownership/mode won't match, so we refuse rather than write into their trap.
        if hasattr(os, "geteuid"):
            st = os.lstat(_SPILL_DIR)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() \
                    or (st.st_mode & 0o777) != 0o700:
                return None
        if not _spill_swept:                          # once per process: 3-day age sweep (the real
            _spill_swept = True                        # upper bound — /tmp is ext4 on WSL2, not tmpfs)
            cutoff = time.time() - 3 * 86400
            for fn in os.listdir(_SPILL_DIR):
                fp = os.path.join(_SPILL_DIR, fn)
                try:
                    if os.path.getmtime(fp) < cutoff:
                        os.unlink(fp)
                except OSError:
                    pass
        path = os.path.join(_SPILL_DIR, "bash-%d-%d.log" % (os.getpid(), next(_spill_seq)))
        # O_EXCL|O_NOFOLLOW: fail if the target already exists or is a symlink, so a planted
        # symlink can't make us follow it and overwrite an arbitrary file the user can write.
        # (O_NOFOLLOW is added only where the platform has it — see plat.open_excl.)
        fd = plat.open_excl(path)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(out)
        return path
    except Exception:
        return None


class BashTool(Tool):
    name = "bash"
    description = ("Run a shell command in the working dir. Args: command, optional timeout_s "
                  "(seconds; default 120, max 600 — RAISE it for slow test suites / builds). For a "
                  "command that never returns (a server, tail -f) background it with & instead.")
    # accept BOTH `timeout_s` and `timeout` (execute_code uses `timeout`) so an override never
    # silently falls back to the default just because the model picked the other name.
    schema = {"type": "object", "properties": {
        "command": {"type": "string"},
        "timeout_s": {"type": "integer", "description": "seconds, default 120, max 600"},
        "timeout": {"type": "integer", "description": "alias for timeout_s"}},
        "required": ["command"]}

    def run(self, args, ctx):
        import os as _os
        import signal as _sig
        # default 120s (the verify gate runs whole test suites — 30s killed them mid-run); clamp to
        # [1, 600] so a typo'd huge value can't wedge the loop. Either arg name works.
        _t = args.get("timeout_s", args.get("timeout"))
        timeout = 120 if _t in (None, "") else max(1, min(600, int(_t)))
        # Popen + start_new_session (NOT subprocess.run) so a timeout kills the WHOLE process group.
        # subprocess.run kills only the direct `sh`; a backgrounded grandchild that inherited the
        # stdout pipe keeps its write end open, so the follow-up drain would block forever and wedge
        # the whole agent loop — the same hazard GrepTool already guards against.
        try:
            # Route through plat.shell_argv so `;`, `&&`, pipes and heredocs mean the same on every
            # OS: POSIX uses /bin/sh; Windows uses Git Bash/MSYS2 if present (else cmd.exe, degraded).
            # Inside the try so a missing `command` key returns a graceful ERROR, never raises.
            _cmdargs, _use_shell = plat.shell_argv(args["command"])
            p = subprocess.Popen(_cmdargs, shell=_use_shell, cwd=ctx.cwd,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=_shim_env(), **plat.new_group_kwargs())
        except Exception as e:
            return "ERROR: %s" % e
        timed_out = False
        try:
            stdout, stderr = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            plat.kill_tree(p)                                  # sh + every grandchild (cross-platform)
            try:
                stdout, stderr = p.communicate(timeout=5)      # drain what was buffered
            except Exception:
                stdout, stderr = "", ""
        out = (stdout or "") + (("\n[stderr] " + stderr) if stderr else "")
        out = out.strip() or "(no output)"
        if timed_out:
            # highest-value spill: re-running a timed-out command costs another full timeout.
            if len(out) > 4000:
                sp = _spill_full_output(out)
                if sp:
                    return ("ERROR: command timed out after %ds (killed) — full pre-kill output "
                            "saved to %s\n%s" % (timeout, sp, out[-4000:]))
            return "ERROR: command timed out after %ds (killed)\n%s" % (timeout, out[-4000:])
        head = "" if p.returncode == 0 else "[exit %d]\n" % p.returncode
        # keep the TAIL on overflow: errors/tracebacks print last, and head-truncation
        # dropped exactly the part that says what went wrong. Spill the FULL output to a file so
        # the model can grep/read_file it instead of paying to re-run the command. The pointer is
        # kept in the FIRST line so it survives context.py's 240-char elision stub of old outputs.
        if len(out) > 8000:
            sp = _spill_full_output(out)
            if sp:
                marker = ("…[truncated %d chars — full output (%d lines) saved to %s; grep it or "
                          "read_file offset=1, do NOT re-run to see more]\n"
                          % (len(out) - 8000, out.count("\n") + 1, sp))
            else:
                marker = "…[truncated %d chars]\n" % (len(out) - 8000)
            out = marker + out[-8000:]
        return head + out


class RunInEnvTool(Tool):
    """Execute a command in the REAL project environment — the instance's container (deps installed,
    code at /testbed) with the model's CURRENT working-tree edits replayed on top. This closes the
    hole that makes collie's SWE verify theater: the bare checkout has no deps, so `import <pkg>`
    fails locally and the model can't actually run its fix. Here it can. Gated by COLLIE_E2E_IMAGE."""
    name = "run_in_env"
    description = ("Run a shell command in the REAL project environment — dependencies installed, "
                  "code at /testbed, with YOUR current edits applied. Your LOCAL working dir has NO "
                  "deps (so `import <thepackage>` fails there); use THIS to actually REPRODUCE the "
                  "issue and VERIFY your fix against real code — e.g. "
                  "`python -c \"from pkg.mod import fn; assert fn(x)==expected\"`, or run the repo's "
                  "OWN existing tests for the file you changed. Test the EDGE cases (None, empty, "
                  "boundaries), not just the happy path. Non-zero exit shows as [exit N]. "
                  "Args: command, optional timeout_s (default 180, max 600).")
    schema = {"type": "object", "properties": {
        "command": {"type": "string"},
        "timeout_s": {"type": "integer", "description": "seconds, default 180, max 600"},
        "timeout": {"type": "integer", "description": "alias for timeout_s"}},
        "required": ["command"]}

    def run(self, args, ctx):
        import tempfile as _tf
        image = os.environ.get("COLLIE_E2E_IMAGE", "")
        if not image:
            return "ERROR: run_in_env not configured (COLLIE_E2E_IMAGE unset)"
        _t = args.get("timeout_s", args.get("timeout"))
        timeout = 180 if _t in (None, "") else max(1, min(600, int(_t)))
        cmd = args.get("command", "")
        try:
            diff = subprocess.run(["git", "-C", ctx.cwd, "diff", "--no-color"],
                                  capture_output=True, text=True, timeout=30).stdout
        except Exception:
            diff = ""

        def _exec(apply_edits):
            pf = _tf.NamedTemporaryFile("w", suffix=".patch", delete=False)
            pf.write(diff if apply_edits else ""); pf.close()
            apply = ("(git apply /tmp/e.patch 2>/dev/null || git apply --3way /tmp/e.patch 2>/dev/null || true) && "
                     if apply_edits else "")
            inner = ("cd /testbed && " + apply +
                     "CONDA=$(ls -d /opt/conda /opt/miniconda3 2>/dev/null | head -1) && "
                     "{ source \"$CONDA/etc/profile.d/conda.sh\" 2>/dev/null && conda activate testbed 2>/dev/null; }; "
                     + cmd)
            docker = ["docker", "run", "--rm", "-v", pf.name + ":/tmp/e.patch:ro",
                      image, "bash", "-lc", inner]
            try:
                p = subprocess.run(docker, capture_output=True, text=True, timeout=timeout + 40)
                o = (p.stdout or "") + (("\n[stderr] " + p.stderr) if p.stderr else "")
                return p.returncode, (o.strip() or "(no output)")
            except subprocess.TimeoutExpired:
                return 124, "(timed out after %ds)" % timeout
            except Exception as e:
                return 1, "ERROR: %s" % e
            finally:
                try:
                    os.remove(pf.name)
                except OSError:
                    pass

        def _tail(s, n=3000):
            return s if len(s) <= n else "…\n" + s[-n:]

        # REPRODUCE-FIRST red->green: a verification command (assert / a test run) is executed BOTH on
        # the ORIGINAL code and with the edits. A check that already passes on the original does NOT
        # reproduce the reported bug and cannot validate the fix — the exact false-green that let wrong
        # fixes finish. Only run the dual-check once the model has edits to compare against.
        low = cmd.lower()
        is_verify = any(k in low for k in ("assert", "pytest", "unittest", "-m unittest"))
        if is_verify and diff.strip():
            base_rc, base_out = _exec(False)
            edit_rc, edit_out = _exec(True)
            if base_rc != 0 and edit_rc == 0:
                verdict = ("✓ RED→GREEN: FAILS on the original code (reproduces the bug) and PASSES with "
                           "your fix. This is a genuine verification.")
            elif base_rc == 0 and edit_rc == 0:
                verdict = ("⚠ PASSES WITHOUT YOUR FIX — this check also succeeds on the ORIGINAL code, so it "
                           "does NOT reproduce the reported bug and does NOT validate your fix. Rewrite the "
                           "check so it FAILS on the original code (encode the exact wrong behavior the ISSUE "
                           "describes), then re-run. Do not finish until you see RED→GREEN.")
            elif base_rc != 0 and edit_rc != 0:
                verdict = "✗ STILL FAILING with your fix — the bug is not resolved. Read the failure and iterate."
            else:
                verdict = "✗ REGRESSION — passed on the original code but FAILS with your fix; your edit broke it."
            return ("%s\n--- ORIGINAL code [exit %d] ---\n%s\n--- WITH YOUR EDITS [exit %d] ---\n%s"
                    % (verdict, base_rc, _tail(base_out), edit_rc, _tail(edit_out)))
        # exploration (no assertion) or no edits yet: single run with whatever edits exist
        rc, out = _exec(bool(diff.strip()))
        if len(out) > 8000:
            out = "…[truncated]\n" + out[-8000:]
        head = "" if rc == 0 else "[exit %d]\n" % rc
        return head + out


class GrepTool(Tool):
    name = "grep"
    description = "Search files for a pattern (ripgrep if available). Args: pattern, path."
    schema = {"type": "object", "properties": {
        "pattern": {"type": "string"}, "path": {"type": "string"}},
        "required": ["pattern"]}

    # heavy dirs/files a code search should never waste time on (matters on non-git trees like ~,
    # where ripgrep won't have a .gitignore to prune) — this is what made a bare `.` grep time out.
    _EXC = (".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", "target",
            ".next", "vendor", ".mypy_cache", ".cache", "site-packages")

    def run(self, args, ctx):
        pat, _e = _need_str(args, "pattern")
        if _e:
            return _e
        path = args.get("path") or "."
        if not isinstance(path, str):
            path = str(path)
        # `-e <pat>` so a pattern starting with '-' isn't parsed as a flag. --max-filesize skips
        # the giant blobs (logs/datasets) that stall a scan; -g '!dir' prunes vendored trees.
        excl_rg = " ".join("-g '!%s'" % d for d in self._EXC) + \
            " -g '!*.jsonl' -g '!*.min.js' -g '!*.map' -g '!*.lock'"
        rg = "rg -n --no-heading --max-filesize 2M %s -e %s %s" % (excl_rg, _sh(pat), _sh(path))
        # grep fallback runs in ERE (-E) so `a|b` alternation means OR like it does in rg — default
        # BRE grep treats `|` as a literal, silently returning nothing for every rg-style pattern.
        # The -F pass catches literal patterns that are invalid ERE (e.g. an unclosed `emit(`).
        excl_gr = " ".join("--exclude-dir=%s" % d for d in self._EXC)
        gr = "grep -rnIE %s -e %s %s" % (excl_gr, _sh(pat), _sh(path))
        grf = "grep -rnIF %s -e %s %s" % (excl_gr, _sh(pat), _sh(path))
        cmd = rg + " || " + gr + " || " + grf
        # Popen (not run) so a timeout still returns the matches found SO FAR — a huge tree should
        # yield partial results, not nothing (the user's ask: even very large grep output must still
        # be capturable). start_new_session so
        # we can kill the WHOLE process group on timeout: p.kill() alone leaves the rg/grep children
        # holding the stdout pipe and communicate() hangs forever.
        import os as _os
        import signal as _sig
        _cmdargs, _use_shell = plat.shell_argv(cmd)          # POSIX shell on every OS (Git Bash on Win)
        p = subprocess.Popen(_cmdargs, shell=_use_shell, cwd=ctx.cwd, stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, text=True, **plat.new_group_kwargs())
        try:
            out, _ = p.communicate(timeout=25)
            return ((out or "").strip() or "(no matches)")[:6000]
        except subprocess.TimeoutExpired:
            plat.kill_tree(p)                                  # kill sh + rg + grep together
            out = ""
            try:
                out, _ = p.communicate(timeout=5)
            except Exception:
                pass
            out = (out or "").strip()
            if out:
                return out[:6000] + "\n… (hit 25s; PARTIAL results — pass a narrower `path` for the rest)"
            return ("(no match within 25s — this tree is very large; narrow `path` (e.g. a "
                    "subdir) or use a more specific pattern)")
        except Exception as e:
            return "ERROR: %s" % e


class GlobTool(Tool):
    name = "glob"
    description = "List files matching a glob. Args: pattern (e.g. **/*.py)."
    schema = {"type": "object", "properties": {"pattern": {"type": "string"}},
              "required": ["pattern"]}

    def run(self, args, ctx):
        import glob
        pattern, _e = _need_str(args, "pattern")
        if _e:
            return _e
        hits = glob.glob(os.path.join(ctx.cwd, pattern), recursive=True)
        rel = sorted(os.path.relpath(h, ctx.cwd) for h in hits)   # deterministic order
        out = rel[:200]
        tail = "\n…(+%d more; narrow the pattern)" % (len(rel) - 200) if len(rel) > 200 else ""
        return ("\n".join(out) + tail) or "(no matches)"


class MemorySearchTool(Tool):
    name = "memory_search"
    description = ("Recall facts from long-term memory via hybrid semantic+keyword "
                   "search. Args: query, optional k.")
    schema = {"type": "object", "properties": {
        "query": {"type": "string"}, "k": {"type": "integer"}},
        "required": ["query"]}

    def run(self, args, ctx):
        query, _e = _need_str(args, "query")
        if _e:
            return _e
        try:
            k = max(1, min(50, int(args.get("k", 5))))
        except (TypeError, ValueError):
            k = 5
        hits = ctx.memory.recall(query, project=ctx.project, k=k)
        if not hits:
            return "(no memories found)"
        return "\n".join("[%.3f] %s" % (h["score"], h["text"]) for h in hits)


class RememberTool(Tool):
    name = "remember"
    description = "Store a durable fact in long-term memory. Args: text, optional keys."
    schema = {"type": "object", "properties": {
        "text": {"type": "string"}, "keys": {"type": "string"}},
        "required": ["text"]}

    def run(self, args, ctx):
        text, _e = _need_str(args, "text")
        if _e:
            return _e
        keys = args.get("keys", "")
        rid = ctx.memory.remember(text, keys=keys if isinstance(keys, str) else "", project=ctx.project)
        return "remembered #%d" % rid


def _sh(s: str) -> str:
    return "'" + str(s).replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------- #
class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._activated: set[str] = set()   # deferred tools promoted to active this session

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def always_on(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.tier == "always"]

    def deferred_names(self) -> list[str]:
        # ALL deferred tool names, SORTED — byte-stable across the whole session. Point-12 stage A:
        # previously this dropped activated tools from the advert (to avoid a dup mention), but that
        # shrank the STABLE prompt section on every load_tools call and busted the cached prefix —
        # the exact lever collie's context ordering exists to protect. A re-advertised active tool is
        # a cheap no-op (a load_tools re-call just re-resolves it), so stability wins over de-duping.
        return sorted(t.name for t in self._tools.values() if t.tier == "deferred")

    def activate(self, names) -> list[str]:
        """Promote named deferred tools to the active set (their schema then rides the next turn).
        Returns the names that actually resolved to a known deferred tool."""
        ok = []
        for n in (names or []):
            t = self._tools.get(n)
            if t is not None and t.tier == "deferred":
                self._activated.add(n); ok.append(n)
        return ok

    def active_schemas(self) -> list[dict]:
        out = [t.provider_schema() for t in self.always_on()]
        for n in self._activated:               # + any deferred tool the model has loaded
            t = self._tools.get(n)
            if t is not None:
                out.append(t.provider_schema())
        return out

    def names(self) -> list[str]:
        return list(self._tools.keys())


class LoadToolsTool(Tool):
    """The deferred-tier seam: extra tools (MCP servers, opt-in extras) are advertised by NAME only
    so the cached prefix stays lean; the model calls load_tools to pull the schema of the ones it
    actually needs, and they ride the active schema list from the next turn on. Mirrors Claude
    Code's ToolSearch."""
    name, tier = "load_tools", "always"
    description = ("Load one or more DEFERRED tools (listed under 'TOOLS (deferred, load on "
                   "demand)') so you can call them. Pass the exact names. Returns their input "
                   "schemas; call the tool on a later turn. Args: names (array of strings).")
    schema = {"type": "object", "properties": {
        "names": {"type": "array", "items": {"type": "string"}}}, "required": ["names"]}

    def __init__(self, registry):
        self._reg = registry

    def run(self, args, ctx):
        names = args.get("names") if isinstance(args, dict) else None
        if isinstance(names, str):
            names = [names]
        if not names or not isinstance(names, list):
            return "ERROR: 'names' must be a non-empty array of deferred tool names"
        ok = self._reg.activate(names)
        miss = [n for n in names if n not in ok]
        if not ok:
            avail = ", ".join(self._reg.deferred_names()) or "(none)"
            return "ERROR: none of %s are loadable deferred tools. Available: %s" % (names, avail)
        lines = ["loaded %d tool(s): %s" % (len(ok), ", ".join(ok))]
        if miss:
            lines.append("(not found, skipped: %s)" % ", ".join(miss))
        lines.append("")
        for n in ok:
            t = self._reg.get(n)
            s = t.provider_schema()
            import json as _j
            lines.append("### %s\n%s\ninput_schema: %s" % (
                s["name"], s.get("description", ""), _j.dumps(s.get("input_schema", {}))))
        lines.append("\nCall them on your next turn.")
        return "\n".join(lines)


def default_registry(code_search: bool = False,
                     web_search: bool = False, exec_code: bool = False,
                     delegate: bool = False) -> ToolRegistry:
    r = ToolRegistry()
    from .plantool import PlanTool          # multi-step task tracking (CC TodoWrite / Hermes todo)
    from .checkpoint import UndoTool         # roll back file edits made this session
    for t in (ReadFileTool(), WriteFileTool(), EditFileTool(), BashTool(), GrepTool(),
              GlobTool(), MemorySearchTool(), RememberTool(), PlanTool(), UndoTool()):
        r.register(t)
    if code_search:                              # semantic repo navigation (embedding)
        from .codeindex import register_code_search
        register_code_search(r)
    if os.environ.get("COLLIE_E2E_IMAGE"):       # real-env verification (SWE): run repro vs installed deps
        r.register(RunInEnvTool())
    # LLM-driven real browser via the extension. COLLIE_BROWSER_BRIDGE=1 forces it on; =0 forces off;
    # otherwise AUTO-ENABLE whenever a bridge is live with a browser connected — "if the local browser
    # is available, default to using it" (authenticated pages, real logged-in results, no bot-block).
    _bb = os.environ.get("COLLIE_BROWSER_BRIDGE")
    bridge_on = _bb == "1" or (_bb != "0" and _bridge_live_safe())
    if web_search and not bridge_on:             # keyless web_search + web_fetch ONLY when no real
        from .websearch import register_web_search   # browser. When the bridge is live, browser_* is
        register_web_search(r)                       # the SOLE web path so collie defaults to the
        from .webfetch import register_web_fetch      # user's logged-in browser (auth, real results).
        register_web_fetch(r)
    if exec_code:                                # programmatic tool calling (per-token lever)
        from .progtool import register_execute_code
        register_execute_code(r)
    if delegate and os.environ.get("COLLIE_SUBAGENT") != "1":  # single-depth: no nesting
        from .delegate import register_delegate
        register_delegate(r)
    if bridge_on:                                # the ONE browser path: the user's real logged-in
        from .browserbridge import register_browser_bridge   # Chrome via the extension (or a managed
        register_browser_bridge(r)                           # Chromium launched with it — same tools)
    # native desktop app control (Windows UI Automation): the desktop_* tools that let collie drive
    # any native app. Opt-in — powerful, so only when COLLIE_DESKTOP_CONTROL=1 (the "Control desktop
    # apps" setting) AND the platform can actually drive apps.
    if os.environ.get("COLLIE_DESKTOP_CONTROL") == "1":
        try:
            from .native import register_native, available as _native_available
            if _native_available()[0]:
                register_native(r)
        except Exception:
            pass
    # external MCP servers -> deferred tier (advertised by name, schema loaded on demand). Kept
    # last so a broken server can't stop the core tools from registering.
    try:
        from .mcpclient import register_mcp_servers
        register_mcp_servers(r)
    except Exception:
        pass
    # the load-on-demand seam only earns its always-on slot when there's something deferred to load
    if r.deferred_names():
        r.register(LoadToolsTool(r))
    return r
