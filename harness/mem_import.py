"""Memory migration — distill past Claude Code / Codex sessions into collie's memory.

The gap this closes, observed live (2026-07-14): collie's Recall knew nothing about "where
is the Higgs API key" while the answer sat in a Claude Code transcript on this machine.
Local agent history is the richest source of durable user facts, but raw transcripts are
90% tool dumps — so this pipeline is distill-then-embed, never embed-raw:

    parse session -> chunk ALL turns in order -> RollingDistiller (each call sees the fact
    list so far + the next redacted excerpt; the session's FINAL state wins) ->
    mem.remember(project="global", keys="import src:… sid:…")

Long sessions are the hard case and drove the rolling design: independent per-chunk
extraction breaks cross-chunk references and stores interim conclusions the session
later reverted (a port that changed, an approach that failed). Rolling distillation
revises the list as the narrative unfolds, so only the end-state survives. Giant
sessions beyond --max-chunks are sampled evenly (first/last always kept).

Design rules (agreed 2026-07-14):
  * distill first — raw turns retrieve worse and cost more (the Mem0/LOCOMO lesson,
    see distill.py);
  * redact BEFORE store — transcripts are full of plaintext keys; memories must hold
    {{SECRET:…}} placeholders, never credentials (harness/redact.py patterns);
  * explicit opt-in, incremental afterwards — a state file skips already-imported
    sessions, so a cron `collie mem import` only pays for what's new;
  * provenance — every fact's keys carry `import src:<cc|codex> sid:<id8>`, so a bad
    batch is grep-able and `--purge` can revert every imported fact in one go.

Facts land under project="global": recall's scope filter is (project=? OR 'global'),
so imported knowledge surfaces in every project including the web UI.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import redact as _redact

CC_ROOT = Path(os.path.expanduser("~/.claude/projects"))
CODEX_ROOT = Path(os.path.expanduser("~/.codex/sessions"))
STATE_PATH = Path(os.path.expanduser("~/.collie/mem_import_state.json"))

MAX_USER_CHARS = 600         # user messages are intent-dense — keep more of them
MAX_ASST_CHARS = 400         # assistant prose compresses harder
MAX_CHUNK_CHARS = 6000       # one distiller call sees at most this much conversation
DEFAULT_MAX_CHUNKS = 16      # full coverage for most sessions; giants get even sampling
MAX_FACTS_PER_SESSION = 25   # rolling list cap — the distiller must consolidate, not hoard
MIN_SESSION_BYTES = 2000     # below this a session is an empty/aborted stub

# Templated chore sessions (auto-apply form filling, application-email triage) produce 10+
# near-duplicate facts each and there are thousands of them — they dilute recall without
# carrying durable knowledge. Matched sessions are recorded in state (never re-parsed) and
# skipped without spending a distiller call. Sessions ABOUT building those systems have
# different titles ("Auto-apply system …") and are not matched.
SKIP_TITLE_RE = re.compile(
    r"^(fill|review|submit|complete|classify|qc|quality[- ]?check|verify)\b.*\bapplication"
    r"|at 20 minutes"
    r"|^(analyze\s+)?(dota\s*2\s+)?(team\s*fight|game\s*(battle\s*)?state)\b.*\b(analysis|positioning|strategy|snapshot)",
    re.I)
# 2026-07-17/18: two chore families added beyond the original list —
#   * auto-apply QC ("QC job application form for X"): 74% of a backfill run, ~14
#     near-duplicate facts each;
#   * dota eval-harness per-game analysis ("Dota 2 team fight analysis at 20 minutes"):
#     transient game-state snapshots, hundreds of sessions.
# Sessions ABOUT building those systems have different titles and still import.
# NOTE: this regex is now only the MANUAL override — the primary defense against
# template farms is the automatic family gate below (prompt_sig / FAMILY_MIN).


# ------------------------------------------------------------------- family gate --
# Chore sessions are spawned programmatically, so every member of a family opens with
# a VERBATIM prompt template; hand-typed openings essentially never collide (validated
# 2026-07-18 on this corpus: every first-message signature group >=5 among 150 random
# sessions was a true template family — auto-apply forms, dota eval snapshots, reddit
# persona, cartek QA — while the largest human-session group was 3). Titles are the
# WEAK signal (LLM-paraphrased, short, collision-prone) and are not used here.
# Once a family passes FAMILY_MIN total sightings, only FAMILY_KEEP exemplars are
# distilled — unless progressive sampling finds the exemplars' facts DIVERSE (each
# member carrying fresh content), in which case the family imports in full.
FAMILY_MIN = int(os.environ.get("COLLIE_FAMILY_MIN", "15") or 15)
FAMILY_KEEP = int(os.environ.get("COLLIE_FAMILY_KEEP", "3") or 3)
FAMILY_DIVERSE_SIM = float(os.environ.get("COLLIE_FAMILY_DIVERSE_SIM", "0.78") or 0.78)
# 0.78 calibrated on this corpus 2026-07-18: pure-template farms score 0.89-0.96,
# entity-varying template farms (QC forms w/ company names, dota states w/ coords)
# 0.81-0.83, genuinely diverse families <=0.74. NOTE diversity != durability: a farm
# of SYNTHETIC conversations (fictional QA personas) can score diverse yet still be
# junk — flip its registry entry to "verdict": "template" by hand for those.
FAMILY_REG_PATH = Path(os.path.expanduser("~/.collie/mem_import_families.json"))


def prompt_sig(src: str, path: Path) -> str | None:
    """Family signature: sha1 of the first real user message (300 chars, lowercased,
    whitespace-folded, digits->#). Digit folding groups per-instance variants like
    dota game states ('t=20.2min ... @(4173,-5070)')."""
    import hashlib
    txt = None
    try:
        with open(path, encoding="utf-8") as f:
            for i, ln in enumerate(f):
                if i > 400:
                    break
                if src == "cc":
                    if '"type":"user"' not in ln and '"type": "user"' not in ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    if d.get("type") != "user" or d.get("isSidechain"):
                        continue
                    t = _text_items((d.get("message") or {}).get("content"))
                else:
                    if '"response_item"' not in ln:
                        continue
                    try:
                        d = json.loads(ln)
                    except ValueError:
                        continue
                    p = d.get("payload") or {}
                    if p.get("type") != "message" or p.get("role") != "user":
                        continue
                    t = _text_items(p.get("content"))
                if t and _keep("user", t) and not t.lstrip().startswith("[tool-error]"):
                    txt = t
                    break
    except OSError:
        return None
    if not txt:
        return None
    n = re.sub(r"\s+", " ", txt[:300].lower())
    n = re.sub(r"\d+", "#", n)
    return hashlib.sha1(n.encode()).hexdigest()[:12]


def _load_famreg() -> dict:
    try:
        with open(FAMILY_REG_PATH, encoding="utf-8") as f:
            return json.load(f) or {}
    except (OSError, ValueError):
        return {}


def _save_famreg(reg: dict) -> None:
    FAMILY_REG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(FAMILY_REG_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(reg, f, ensure_ascii=False)
    os.replace(tmp, FAMILY_REG_PATH)


def _family_redundancy(mem, sids: list[str]) -> float | None:
    """Progressive-sampling verdict input: mean over exemplar facts of max cosine to
    the OTHER exemplars' facts. ~0.9 = every member restates the same facts (template
    farm); low = members carry fresh content. None if <2 exemplars have stored facts
    (e.g. the family was always title-skipped) — treated as template by the gate."""
    sess = []
    for sid in sids:
        rows = mem.db.execute(
            "SELECT embedding FROM facts WHERE keys LIKE ? AND embedding IS NOT NULL",
            ("%sid:" + sid + "%",)).fetchall()
        vecs = []
        for r in rows:
            try:
                vecs.append(json.loads(r["embedding"]))
            except (ValueError, TypeError, IndexError):
                pass
        if vecs:
            sess.append(vecs)
    if len(sess) < 2:
        return None
    import math

    def cos(a, b):
        d = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(x * x for x in b))
        return d / (na * nb) if na and nb else 0.0

    tot, n = 0.0, 0
    for i, vs in enumerate(sess):
        others = [v for j, o in enumerate(sess) if j != i for v in o]
        for v in vs:
            tot += max(cos(v, o) for o in others)
            n += 1
    return tot / n if n else None


def _light_title(path: Path) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for i, ln in enumerate(f):
                if i > 50:
                    break
                if "aiTitle" in ln:
                    try:
                        return json.loads(ln).get("aiTitle") or ""
                    except ValueError:
                        return ""
    except OSError:
        pass
    return ""


def family_scan(source: str = "all", seed: bool = False, mem=None, log=print) -> list[dict]:
    """Corpus-wide family census: reads every session's opening prompt (no LLM, no
    imports) and reports signature groups >= FAMILY_MIN with sample titles and — given
    a mem handle — the exemplar-fact redundancy score. seed=True records the families
    in the registry so incremental runs gate them immediately instead of waiting to
    re-observe FAMILY_MIN members."""
    import collections
    state = _load_state()
    groups: dict = collections.defaultdict(list)
    for src, path in discover(source):
        s = prompt_sig(src, path)
        if s:
            groups[s].append((src, path))
    reg = _load_famreg()
    out = []
    for s, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        if len(members) < FAMILY_MIN:
            continue
        titles: list[str] = []
        for _, p in members:
            t = _light_title(p)
            if t and t not in titles:
                titles.append(t)
            if len(titles) >= 3:
                break
        sids = [p.stem[:8] for _, p in members[:8]]
        red = _family_redundancy(mem, sids[:5]) if mem is not None else None
        regex_hit = sum(1 for t in titles if SKIP_TITLE_RE.search(t))
        out.append({"sig": s, "n": len(members), "titles": titles, "redundancy": red})
        if seed:
            # verdict precedence: the manual title regex is a human ruling and wins;
            # then measured redundancy; a big family with NO measurable facts (always
            # skipped, or purged) defaults to template — conservative, exemplars still
            # import and can flip it via the lazy re-check in run_import.
            if regex_hit and regex_hit * 2 >= max(1, len(titles)):
                verdict = "template"
            elif red is not None:
                verdict = "diverse" if red < FAMILY_DIVERSE_SIM else "template"
            else:
                verdict = "template"
            entry = {"seen": len(members),
                     "imported": sum(1 for _, p in members if str(p) in state),
                     "sids": sids, "verdict": verdict}
            if red is not None:
                entry["redundancy"] = red
            reg[s] = entry
        log("family %s n=%-4d red=%s | %s" % (
            s, len(members), ("%.3f" % red) if red is not None else " n/a ",
            " / ".join(titles)[:110]))
    if seed:
        _save_famreg(reg)
        log("seeded %d families -> %s" % (len(out), FAMILY_REG_PATH))
    return out


# --------------------------------------------------------------------------- parsing --
def _text_items(content) -> str:
    """CC/Codex message content -> plain text. Tool dumps stay excluded (90% noise) with
    ONE exception: ERROR tool-results — failures are durable knowledge (root causes,
    gotchas), and dropping them made the distiller store 'what was tried' but never
    'why it failed' (membench fair-v2 autopsy, 2026-07-17)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if not isinstance(p, dict):
                continue
            if p.get("type") in ("text", "input_text", "output_text"):
                parts.append(p.get("text", ""))
            elif p.get("type") == "tool_result" and p.get("is_error"):
                err = p.get("content")
                if isinstance(err, list):
                    err = " ".join(x.get("text", "") for x in err if isinstance(x, dict))
                parts.append("[tool-error] " + str(err or "")[:300])
        return "\n".join(parts)
    return ""


def _keep(role: str, text: str) -> bool:
    """Drop harness-injected pseudo-messages: reminders, command echoes, permission banners."""
    t = text.lstrip()
    if not t:
        return False
    if role == "user" and t.startswith("<"):     # <system-reminder>, <local-command-…>, <permissions…>
        return False
    return True


def parse_cc_session(path: Path) -> dict | None:
    """One CC session jsonl -> {sid, title, turns:[(role, text)…]}; None if nothing usable."""
    turns, title, sid = [], "", path.stem
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                t = d.get("type")
                if t == "ai-title":
                    title = d.get("aiTitle") or title
                elif t in ("user", "assistant") and not d.get("isSidechain"):
                    txt = _text_items((d.get("message") or {}).get("content"))
                    if _keep(t, txt):
                        turns.append((t, txt.strip()))
                elif t == "attachment" and (d.get("attachment") or {}).get("type") == "max_turns_reached":
                    # terminal outcome marker — without it a failed run reads as merely unfinished
                    turns.append(("user", "[run-outcome] max_turns_reached — turn budget exhausted"))
    except OSError:
        return None
    if not turns:
        return None
    return {"sid": sid, "title": title, "turns": turns}


def parse_codex_session(path: Path) -> dict | None:
    """One Codex rollout jsonl -> same shape as parse_cc_session."""
    turns, title, sid = [], "", path.stem
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                if d.get("type") == "session_meta":
                    sid = (d.get("payload") or {}).get("session_id") or sid
                elif d.get("type") == "response_item":
                    p = d.get("payload") or {}
                    role = p.get("role")
                    if p.get("type") == "message" and role in ("user", "assistant"):
                        txt = _text_items(p.get("content"))
                        if _keep(role, txt):
                            turns.append((role, txt.strip()))
    except OSError:
        return None
    if not turns:
        return None
    return {"sid": sid, "title": title, "turns": turns}


def discover(source: str) -> list[tuple[str, Path]]:
    """All candidate session files for a source, newest first."""
    out = []
    if source in ("cc", "all") and CC_ROOT.is_dir():
        out += [("cc", p) for p in CC_ROOT.glob("*/*.jsonl")]
    if source in ("codex", "all") and CODEX_ROOT.is_dir():
        out += [("codex", p) for p in CODEX_ROOT.rglob("rollout-*.jsonl")]
    out = [(s, p) for s, p in out if p.stat().st_size >= MIN_SESSION_BYTES]
    out.sort(key=lambda sp: sp[1].stat().st_mtime, reverse=True)
    return out


# ------------------------------------------------------------------------- chunking --
def chunk_turns(turns: list[tuple[str, str]], max_chunks: int = DEFAULT_MAX_CHUNKS) -> list[str]:
    """Turns -> sequential chunks of 'U:/A:' lines, ≤MAX_CHUNK_CHARS each.

    The whole session is chunked in ORDER (rolling distillation needs the narrative);
    only when a giant session exceeds `max_chunks` do we sample evenly across it —
    always keeping the first and last chunk, where the task statement and the final
    state live."""
    lines = ["%s: %s" % ("U" if r == "user" else "A",
                         t[:MAX_USER_CHARS if r == "user" else MAX_ASST_CHARS])
             for r, t in turns]
    chunks, cur, size = [], [], 0
    for ln in lines:
        if size + len(ln) > MAX_CHUNK_CHARS and cur:
            chunks.append("\n".join(cur)); cur, size = [], 0
        cur.append(ln); size += len(ln) + 1
    if cur:
        chunks.append("\n".join(cur))
    n = len(chunks)
    if max_chunks <= 0 or n <= max_chunks:   # 0 = no sampling: full-coverage rolling pass
        return chunks
    idx = sorted({round(i * (n - 1) / (max_chunks - 1)) for i in range(max_chunks)})
    return [chunks[i] for i in idx]


# ------------------------------------------------------------------------ distilling --
_ROLL_SYS = (
    "You maintain the running list of durable, memorable facts from one long conversation, "
    "processing it excerpt by excerpt in order. You get the fact list accumulated so far and "
    "the next excerpt. Return the UPDATED COMPLETE list as a JSON array of strings:\n"
    "- keep still-true facts VERBATIM (do not rephrase them);\n"
    "- revise any fact the new excerpt contradicts or refines — the conversation's FINAL "
    "state wins over interim VALUES (a port that changed, a superseded config);\n"
    "- add new durable facts: concise third-person sentences with who/what, dates, numbers, "
    "places, pronouns resolved to names;\n"
    "- ALWAYS keep diagnostic knowledge even when the attempt itself was abandoned: root "
    "causes (X failed because Y — include the exact error message or symptom when short), "
    "decisions with their reason (chose A over B because C), and gotchas (Z breaks unless W). "
    "A failed run whose cause was identified must yield at least one such fact — 'why it "
    "failed' outlives 'what was tried';\n"
    "- drop only true transients: scaffolding commands, chit-chat, and dead-end details "
    "whose lesson is already captured as a fact.\n"
    "Never store text that looks like {{SECRET:…}} placeholders' underlying values. "
    "At most %d facts. Return ONLY the JSON array.")   # %d = MAX_FACTS_PER_SESSION
# A chunk-scaled floating cap (25→60 for long sessions) was A/B'd 2026-07-17: no strict@10
# gain (66%=66%), slightly worse early ranks, and with the diagnosis-preserving prompt the
# fixed cap rarely binds anyway (long sessions distilled to 22-24 facts under a 55 cap).


class RollingDistiller:
    """Session-level distillation through ANY collie provider (the user's flat-rate Claude
    subscription does it at $0). Rolling, not per-chunk-independent: each call sees the fact
    list so far, so cross-chunk references resolve and superseded interim conclusions (an
    approach later reverted, a port later changed) are corrected instead of stored as truth."""

    def __init__(self, provider):
        self.provider = provider
        self.name = "roll:" + provider.model

    def update(self, notes: list[str], chunk_text: str) -> list[str]:
        cap = MAX_FACTS_PER_SESSION
        user = "Facts so far:\n%s\n\nNext excerpt:\n%s" % (
            json.dumps(notes, ensure_ascii=False) if notes else "[]", chunk_text)
        # bulk imports run for hours — a rate-limit blip must not silently drop a session's
        # facts, so transient provider errors retry with backoff before we give up.
        for attempt in range(3):
            comp = self.provider.complete(_ROLL_SYS % cap, [{"role": "user", "content": user}], [])
            if getattr(comp, "stop_reason", "") != "error":
                break
            time.sleep(5 * 2 ** attempt)
        out = (getattr(comp, "text", "") or "")
        try:
            arr = json.loads(out[out.find("["): out.rfind("]") + 1])
            got = [str(x).strip() for x in arr if str(x).strip()][:cap]
            return got or notes            # an empty/failed round keeps prior state
        except Exception:
            return notes                   # parse failure must never lose accumulated facts

    def session_facts(self, chunks: list[str]) -> list[str]:
        notes: list[str] = []
        for ch in chunks:
            # privacy: redact the TRANSCRIPT before it goes to the distillation provider —
            # with a third-party distiller (e.g. gemini) raw history must not leak keys.
            notes = self.update(notes, _redact.redact(ch, {}))
        return notes


def heuristic_facts(sess: dict) -> list[str]:
    """--no-llm fallback: title + opening request + closing answer. Coarse but free."""
    first_user = next((t for r, t in sess["turns"] if r == "user"), "")
    last_asst = next((t for r, t in reversed(sess["turns"]) if r == "assistant"), "")
    facts = []
    if sess["title"]:
        facts.append("Past session: %s — asked: %s" % (sess["title"], first_user[:200]))
    elif first_user:
        facts.append("Past session asked: %s" % first_user[:240])
    if last_asst:
        facts.append("Session%s outcome: %s" % (
            " '%s'" % sess["title"] if sess["title"] else "", last_asst[:240]))
    return facts


# --------------------------------------------------------------------------- driver --
def _load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(state: dict):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(STATE_PATH) + ".tmp"
    Path(tmp).write_text(json.dumps(state))
    os.replace(tmp, STATE_PATH)


def purge(mem) -> int:
    """Delete every imported fact (provenance keys prefix 'import ')."""
    ids = [r["id"] for r in mem.db.execute(
        "SELECT id FROM facts WHERE keys LIKE 'import %'").fetchall()]
    if ids:
        q = ",".join("?" * len(ids))
        mem.db.execute("DELETE FROM facts WHERE id IN (%s)" % q, ids)
        if mem.has_fts:
            mem.db.execute("DELETE FROM facts_fts WHERE rowid IN (%s)" % q, ids)
        mem.db.commit()
    return len(ids)


def run_import(mem, source="all", limit=100, dry_run=False, no_llm=False,
               force=False, provider_name=None, model=None,
               max_chunks=DEFAULT_MAX_CHUNKS, workers=1, log=print) -> dict:
    """Distill + migrate up to `limit` not-yet-imported sessions (newest first).

    With workers>1, parsing+distillation (network-bound) fan out on a thread pool while
    every db write and state save stays on THIS thread — sqlite connections are not
    shared across threads, and per-session state persists as each future completes."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    pname = None
    if not no_llm:
        from . import settings
        from .providers import make_provider
        pname = provider_name or settings.get("PROVIDER", "anthropic")
        # rolling distillation carries state across calls — worth a stronger model than haiku
        if model is None and pname.startswith(("anthropic", "claude")):
            model = "claude-sonnet-5"
        log("[distill] roll:%s via provider %s, %d worker(s)" % (model or pname, pname, workers))
    _tl = threading.local()

    def _distiller():
        if getattr(_tl, "ex", None) is None:      # one provider per worker thread
            from .providers import make_provider
            _tl.ex = RollingDistiller(make_provider(pname, model))
        return _tl.ex

    # force re-processes sessions but must NOT wipe state entries of files this run never
    # touches — the per-file skip below already honors the flag.
    state = _load_state()
    parsers = {"cc": parse_cc_session, "codex": parse_codex_session}
    stats = {"scanned": 0, "skipped": 0, "sessions": 0, "facts": 0, "redacted": 0, "lowvalue": 0}
    t0 = time.time()

    todo = []
    now = time.time()
    for src, path in discover(source):
        stats["scanned"] += 1
        key = str(path)
        mt = path.stat().st_mtime
        if not force and state.get(key) == mt:
            stats["skipped"] += 1
            continue
        if now - mt < 600:                     # session still being written (live agent):
            continue                           # importing it now churns state + dup facts
        if len(todo) < limit:
            todo.append((src, path, mt))

    # ---- family gate: a template farm gets FAMILY_KEEP exemplars, not the whole farm.
    # Two-phase so a family arriving in bulk is caught inside this run: count incoming
    # signatures first, then partition. The registry persists counts across runs so a
    # family trickling in (a few chore sessions a day) is gated once its cumulative
    # sightings pass FAMILY_MIN. Verdict is lazy progressive sampling: when the gate
    # first bites, exemplar facts already in the DB decide template vs diverse.
    import collections as _c
    famreg = _load_famreg()
    sig_of = {str(path): prompt_sig(src, path) for src, path, mt in todo}
    incoming = _c.Counter(s for s in sig_of.values() if s)
    planned: _c.Counter = _c.Counter()
    gated, kept = [], []
    for item in todo:
        src, path, mt = item
        s = sig_of.get(str(path))
        if s:
            reg = famreg.setdefault(s, {"seen": 0, "imported": 0, "sids": []})
            if reg["seen"] + incoming[s] >= FAMILY_MIN and \
               reg["imported"] + planned[s] >= FAMILY_KEEP:
                if "verdict" not in reg:
                    red = _family_redundancy(mem, (reg.get("sids") or [])[:5])
                    reg["redundancy"] = red
                    reg["verdict"] = "diverse" if (red is not None and red < FAMILY_DIVERSE_SIM) \
                        else "template"
                if reg["verdict"] != "diverse":
                    gated.append(item)
                    continue
            planned[s] += 1
        kept.append(item)
    todo = kept
    for src, path, mt in gated:
        state[str(path)] = mt                 # like low-value: never re-parsed
        famreg[sig_of[str(path)]]["seen"] += 1
    stats["family"] = len(gated)
    if gated and not dry_run:
        _save_state(state)
        _save_famreg(famreg)
    if gated:
        log("[family] gated %d template-family session(s)" % len(gated))

    def _one(item):
        """worker: parse + distill only — no db, no state, no shared mutability."""
        src, path, mt = item
        sess = parsers[src](path)
        if not sess:
            return (path, mt, None, [])
        if SKIP_TITLE_RE.search(sess.get("title") or ""):
            return (path, mt, "low-value", [])     # templated chore — no distiller call
        facts = heuristic_facts(sess) if no_llm else \
            _distiller().session_facts(chunk_turns(sess["turns"], max_chunks))
        return (path, mt, sess, facts)

    def _consume(path, mt, sess, facts):
        if sess is None or sess == "low-value":
            if sess == "low-value":
                stats["lowvalue"] += 1
            state[str(path)] = mt              # stub / chore — don't re-parse it every run
            if not dry_run:
                _save_state(state)
            return
        stored = 0
        for fact in facts:
            red = _redact.redact(fact, {})     # vault discarded — memories keep placeholders only
            if red != fact:
                stats["redacted"] += 1
            keys = "import src:%s sid:%s %s" % (
                "cc" if "/.claude/" in str(path) else "codex", sess["sid"][:8], sess["title"][:60])
            if dry_run:
                log("  would store: %s" % red[:140])
            # created_at = the session file's mtime: recency weighting must see when the
            # fact was TRUE, not when it was migrated.
            elif mem.remember(red, keys=keys, project="global", importance=0.55,
                              created_at=int(mt)) != -1:
                stored += 1
        stats["sessions"] += 1
        stats["facts"] += stored if not dry_run else len(facts)
        s = sig_of.get(str(path))
        if s:                                  # family bookkeeping (exemplar counting)
            reg = famreg.setdefault(s, {"seen": 0, "imported": 0, "sids": []})
            reg["seen"] += 1
            reg["imported"] += 1
            if len(reg["sids"]) < 8:
                reg["sids"].append(sess["sid"][:8])
        if not dry_run:
            state[str(path)] = mt
            _save_state(state)                 # crash-safe: persist state after every session
            _save_famreg(famreg)
        log("[%d/%d] %s → %d facts  (%s)" % (
            stats["sessions"], len(todo), sess["sid"][:8], len(facts),
            (sess["title"] or "untitled")[:50]))

    if workers <= 1:
        for item in todo:
            r = _one(item)
            _consume(*r)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for fut in as_completed([ex.submit(_one, it) for it in todo]):
                _consume(*fut.result())

    stats["secs"] = round(time.time() - t0, 1)
    log("done: %(sessions)d sessions -> %(facts)d facts (%(redacted)d redacted, "
        "%(lowvalue)d low-value skipped, %(skipped)d already imported, %(secs)ss)" % stats)
    return stats
