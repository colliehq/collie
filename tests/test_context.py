"""Prompt assembly and the cache it has to keep stable: what goes in, in what order,
and what it costs when the prefix moves.

Split out of test_core.py — a pure move; no assertion was changed. Stdlib-only, no Opus, fast.
    python tests/test_context.py     (exit 0 = all pass)
"""
import inspect, io, json, os, re, sys, tempfile, time, types, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ctx, _Skip, _RecordingMemory, _ScriptProvider, run_module  # noqa: E402,F401

import contextlib
import inspect, io, json, os, re, sys, tempfile, time, types, warnings

def test_multimodal_content():
    # a user message can carry images: content is a list of {text}/{image} blocks. Each provider
    # reshapes the image into its own vision format; text-only paths read content_text().
    from harness.providers import (content_text, AnthropicProvider, OpenAICompatProvider,
                                   OllamaProvider, MockProvider)
    B64 = "iVBORw0KGgo="   # tiny stand-in
    msg = {"role": "user", "content": [
        {"type": "text", "text": "what is this?"},
        {"type": "image", "media_type": "image/png", "data": B64}]}
    # text extraction drops the image (no base64 leak into memory/titles/non-vision providers)
    assert content_text(msg["content"]) == "what is this?"
    assert content_text("plain") == "plain"
    # Anthropic: image -> base64 source block
    a = AnthropicProvider.__new__(AnthropicProvider)._to_anthropic([msg])[0]["content"]
    img = [b for b in a if b.get("type") == "image"]
    assert img and img[0]["source"] == {"type": "base64", "media_type": "image/png", "data": B64}, a
    assert any(b.get("type") == "text" and b["text"] == "what is this?" for b in a)
    # OpenAI: image_url data URI
    o = OpenAICompatProvider.__new__(OpenAICompatProvider)._to_openai("sys", [msg])[-1]["content"]
    assert any(p.get("type") == "image_url" and p["image_url"]["url"] == "data:image/png;base64," + B64 for p in o), o
    # Ollama: images[] of bare base64
    ol = OllamaProvider.__new__(OllamaProvider)._to_ollama("sys", [msg])[-1]
    assert ol["images"] == [B64] and ol["content"] == "what is this?", ol
    # mock reads the text task from a multimodal message (doesn't choke on the list)
    assert MockProvider().__class__.__name__ and MockProvider()._first_user_task([msg]) == "what is this?"

def test_multimodal_run_through_composer():
    """Regression: a multimodal user_msg (attached image -> LIST content) must flow through the
    composer's auto-prefetch without crashing. Bug was `'list' object has no attribute 'strip'` at
    context.py `user_msg.strip()`, then an unhashable-list cache key `(project, user_msg)` — the web
    image-upload path hit both. content_text() now flattens to the text before prefetch/recall/cache."""
    from harness.cli import make_harness
    h = make_harness(os.getcwd(), provider="mock", project="mm", embed="hash")
    h.max_turns = 2
    msg = [{"type": "text", "text": "look at this screenshot"},
           {"type": "image", "media_type": "image/png", "data": "iVBORw0KGgo="}]
    res = h.run("mm", msg)                      # must not raise
    assert res.answer is not None, "multimodal run must complete"
    umsg = [m for m in res.messages if m.get("role") == "user"][0]
    assert isinstance(umsg["content"], list), "image message must stay multimodal in the thread"
    # and the composer must also handle the list directly (belt-and-suspenders on the exact crash site)
    system, _msgs, meta = h.composer.build({"messages": []}, msg, os.getcwd(), "mm")
    assert isinstance(system, str)

def test_response_language_directive():
    """RESPONSE LANGUAGE: reply in the user's INPUT language by default (so clear Chinese like
    "打开collie dashboard" gets a Chinese reply, not the Japanese misfire), with the install
    language (LANG) as the tiebreaker ONLY when the input is ambiguous, plus a per-conversation
    override when the user explicitly asks. The line must ride in the STABLE tier AND survive a
    wholesale identity override — the desktop persona replaces composer.identity outright, so the
    line lives OUTSIDE identity on purpose. LANG=auto has no install language to fall back to."""
    from harness.cli import make_harness
    from harness.context import _response_language_line
    old = os.environ.get("COLLIE_LANG")
    try:
        os.environ["COLLIE_LANG"] = "zh"
        line = _response_language_line()
        # follow input by default; zh is only the AMBIGUITY tiebreaker (not a hard pin)
        assert "same language" in line.lower(), line
        assert "简体中文" in line and "ambiguous" in line.lower(), line
        assert "regardless" not in line.lower() and "always write" not in line.lower(), line
        h = make_harness(os.getcwd(), provider="mock", project="lang", embed="hash")
        h.composer.identity = "You are collie, the user's live desktop assistant."   # wholesale override
        system, _msgs, _meta = h.composer.build({"messages": []}, "打开collie dashboard", os.getcwd(), "lang")
        assert "RESPONSE LANGUAGE" in system and "简体中文" in system, \
            "the directive must survive the identity override"
        # LANG=auto: no concrete install language, so no language name is baked into the tiebreaker
        os.environ["COLLIE_LANG"] = "auto"
        auto = _response_language_line()
        assert "same language" in auto.lower() and "简体中文" not in auto, auto
    finally:
        if old is None:
            os.environ.pop("COLLIE_LANG", None)
        else:
            os.environ["COLLIE_LANG"] = old

def test_grounding_directive():
    """GROUNDING + INITIATIVE: after a miss where collie grepped only the cwd and concluded a
    project "doesn't exist on this machine" while it sat two directories away, the prompt must
    carry three rules: an empty search is not a negative result, auto-recalled memory is a lead
    rather than a fact, and ask only what you cannot determine yourself. Like RESPONSE LANGUAGE
    it lives OUTSIDE identity so
    the desktop persona's wholesale override can't drop it, and the WORKING DIRECTORY line must no
    longer read as "nothing outside cwd exists"."""
    from harness.cli import make_harness
    from harness.context import _grounding_line
    line = _grounding_line()
    low = line.lower()
    assert "your query" in low and "does not exist" in low, "empty search != nonexistent"
    assert "name variants" in low and "former name" in low, "must try renamed/variant spellings"
    assert "lead, not a fact" in low, "recall must not be treated as evidence"
    assert "mis-transcription" in low, "voice input: an odd word may be a misheard proper noun"
    assert "questionnaire" in low and "could" in low, "no question-dumps, no menus of offers"
    h = make_harness(os.getcwd(), provider="mock", project="ground", embed="hash")
    h.composer.identity = "You are collie, the user's live desktop assistant."   # wholesale override
    system, _msgs, _meta = h.composer.build({"messages": []}, "sign the windows build", os.getcwd(), "ground")
    assert "GROUNDING" in system and "INITIATIVE" in system, \
        "the directive must survive the identity override"
    # the working-directory rule must not be readable as "nothing outside cwd exists"
    assert "absolute path" in system and "lives elsewhere on this machine" in system, system[:400]

@contextlib.contextmanager
def _isolated_home():
    """Point HOME at an empty tmp so ~/.claude/skills and ~/.collie/skills resolve to nothing —
    makes the skill tests hermetic regardless of the dev machine's real skill library."""
    hp = tempfile.mkdtemp()
    old = os.environ.get("HOME")
    os.environ["HOME"] = hp
    try:
        yield hp
    finally:
        if old is not None: os.environ["HOME"] = old
        else: os.environ.pop("HOME", None)

def _write_skill(base, name, desc, extra=""):
    d = os.path.join(base, ".collie", "skills", name)
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8").write(
        "---\nname: %s\ndescription: %s\n%s---\n\n# %s body\ndo the thing\n" % (name, desc, extra, name))
    return os.path.join(d, "SKILL.md")

def test_skills_absent_zero_cost():
    """A cwd with no skills must emit NO 'SKILLS' section (docstring-tier reconciliation: the STABLE
    slot promises a skill manifest; without one, zero prompt cost)."""
    from harness.skills import discover_skills, format_skill_index
    with _isolated_home():
        d = tempfile.mkdtemp()
        skills = discover_skills(d)
        assert skills == [] and format_skill_index(skills) == "", "no skills -> empty index"

def test_skills_discovery_and_format():
    from harness.skills import discover_skills, format_skill_index
    with _isolated_home():
        d = tempfile.mkdtemp()
        _write_skill(d, "foo", "Use when doing foo things")
        _write_skill(d, "longdesc", "x" * 900)
        _write_skill(d, "nodesc", "")                       # empty desc -> skipped
        _write_skill(d, "off", "should be hidden", extra="disable-model-invocation: true\n")
        # name falls back to dir basename when frontmatter omits name
        dd = os.path.join(d, ".collie", "skills", "bardir")
        os.makedirs(dd, exist_ok=True)
        open(os.path.join(dd, "SKILL.md"), "w").write("---\ndescription: bar skill\n---\nbody\n")
        skills = discover_skills(d)
        names = {s["name"] for s in skills}
        assert "foo" in names and "bardir" in names, names
        assert "nodesc" not in names and "off" not in names, "empty-desc + disabled must be excluded"
        idx = format_skill_index(skills)
        assert "SKILLS (load on demand)" in idx and "foo: Use when doing foo" in idx
        assert os.path.abspath(os.path.join(d, ".collie", "skills", "foo", "SKILL.md")) in idx
        long = next(s for s in skills if s["name"] == "longdesc")
        assert len(long["description"]) == 500, "description capped at 500"

def test_skills_symlinked_dir_discovered():
    """A skill symlinked into a skill dir must be found. os.walk skips symlinked dirs by default
    (followlinks=False), which hid every symlinked skill (e.g. ~/.claude/skills/x -> /project/x)
    so skills never reached the prompt. Also asserts a symlink cycle can't hang discovery."""
    from harness.skills import discover_skills
    with _isolated_home():
        d = tempfile.mkdtemp()
        # real skill lives OUTSIDE the scanned tree, then is symlinked in
        real = tempfile.mkdtemp()
        os.makedirs(os.path.join(real, "linked"), exist_ok=True)
        open(os.path.join(real, "linked", "SKILL.md"), "w").write(
            "---\nname: linked\ndescription: reached via symlink\n---\nbody\n")
        skdir = os.path.join(d, ".collie", "skills")
        os.makedirs(skdir, exist_ok=True)
        try:
            os.symlink(os.path.join(real, "linked"), os.path.join(skdir, "linked"))
            # a self-referential cycle: dir -> itself. followlinks=True must not loop forever.
            os.symlink(skdir, os.path.join(skdir, "loop"))
        except (OSError, NotImplementedError) as e:
            # Windows without Developer Mode / SeCreateSymbolicLink raises WinError 1314. The
            # discovery code (os.walk followlinks=True) is portable; only the fixture needs a symlink.
            raise _Skip("symlink creation not permitted on this OS: %s" % e)
        skills = discover_skills(d)                          # must terminate, must find 'linked'
        assert "linked" in {s["name"] for s in skills}, "symlinked skill must be discovered"

def test_skills_shadowing():
    """A project skill shadows a global one of the same name (first-wins)."""
    from harness.skills import discover_skills
    with _isolated_home() as home:
        # global skill in ~/.collie/skills
        g = os.path.join(home, ".collie", "skills", "dup")
        os.makedirs(g, exist_ok=True)
        open(os.path.join(g, "SKILL.md"), "w").write("---\nname: dup\ndescription: GLOBAL\n---\n")
        d = tempfile.mkdtemp()
        _write_skill(d, "dup", "PROJECT")                   # project dir is scanned first
        skills = discover_skills(d)
        dup = [s for s in skills if s["name"] == "dup"]
        assert len(dup) == 1 and dup[0]["description"] == "PROJECT", "project skill must shadow global"

def test_skills_cache_stable():
    """The STABLE section (with the skill index) is byte-identical across turns -> cache-safe."""
    from harness.cli import make_harness
    with _isolated_home():
        d = tempfile.mkdtemp()
        _write_skill(d, "foo", "do foo")
        h = make_harness(d, provider="mock", project="skilltest", embed="hash")
        s1, _, m1 = h.composer.build({"messages": []}, "hi", d, "skilltest")
        s2, _, m2 = h.composer.build({"messages": []}, "hi", d, "skilltest")
        assert s1 == s2, "skill-index-bearing STABLE must be byte-stable across turns"
        assert "SKILLS (load on demand)" in s1 and "foo: do foo" in s1
        # accounting: skills counted in its OWN section, not double-counted in 'stable'
        assert m1.section_tokens.get("skills", 0) > 0

def test_skills_aggregate_cap():
    from harness.skills import format_skill_index
    skills = [{"name": "s%02d" % i, "description": "y" * 200,
               "path": "/tmp/s%02d/SKILL.md" % i} for i in range(20)]
    idx = format_skill_index(skills)
    assert len(idx) <= 2500 + 200, "aggregate index must be capped"
    assert "more skills" in idx, "overflow must be disclosed, not silently dropped"

def test_skills_loop_reads_skill():
    """End-to-end: the model reads a skill's absolute path (outside cwd) and gets its body."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    with _isolated_home():
        d = tempfile.mkdtemp()
        sp = _write_skill(d, "foo", "do foo")
        h = make_harness(d, provider="mock", project="skillrun", embed="hash")
        h.max_turns = 3
        h.provider = _ScriptProvider([
            Completion(tool_calls=[ToolCall("t0", "read_file", {"path": sp})], stop_reason="tool_use"),
            Completion(text="read the skill", stop_reason="end_turn")])
        res = h.run("skillrun", "do foo")
        tool_msgs = [m for m in res.messages if m.get("role") == "tool"]
        assert tool_msgs and "foo body" in tool_msgs[0]["content"], "skill body must load via read_file"

# ============================ Batch A: cache-waste ledger (#3) + prefix measurement (#2) =========

class _HonestCacheProvider:
    """Simulates a REAL full-prefix auto-cache (DeepSeek-style):
    cache_read = tokens of the byte-common prefix between this request and the previous one, the
    remainder is fresh input. A stable system+schemas run shows ~0 miss; a schema/system change
    busts the prefix from the divergence point. Priced as deepseek so waste_usd is nonzero."""
    name = "honest-cache"
    model = "deepseek-chat"
    reports_cache = True
    max_tokens = 4096

    def __init__(self, script):
        self._script = list(script)   # list of Completion-producing callables per turn
        self._i = 0
        self._prev = ""

    def complete(self, system, messages, tool_schemas, on_text=None):
        from harness.providers import Usage, est_tokens
        comp = self._script[min(self._i, len(self._script) - 1)](messages)
        self._i += 1
        req = system + json.dumps(tool_schemas, sort_keys=True, default=str) + \
            json.dumps(messages, default=str)
        n = 0
        for a, b in zip(req, self._prev):
            if a != b:
                break
            n += 1
        cache_read = est_tokens(req[:n])
        full = est_tokens(req)
        comp.usage = Usage(input_tokens=max(0, full - cache_read),
                           output_tokens=comp.usage.output_tokens, cache_read=cache_read)
        self._prev = req
        return comp

def test_cache_miss_math():
    from harness.costs import cache_miss, NOISE_FLOOR_TOKENS
    from harness.providers import Usage
    # full hit: almost everything cache_read -> below floor -> (0, 0.0)
    assert cache_miss(10000, Usage(input_tokens=20, cache_read=9980), "deepseek-chat", True) == (0, 0.0)
    # full bust: prev prompt re-billed, nothing cached -> tokens==min(prev,prompt), priced at pin-pcached
    tok, usd = cache_miss(10000, Usage(input_tokens=10050, cache_read=0), "deepseek-chat", True)
    assert tok == 10000, tok
    assert abs(usd - 10000 * (0.27 - 0.07) / 1e6) < 1e-9, usd
    # provider that never reports caching (ollama) -> uncountable, no false positive
    assert cache_miss(10000, Usage(input_tokens=10050, cache_read=0), "deepseek-chat", False) == (0, 0.0)
    # turn 0 (no previous prompt) -> nothing to compare
    assert cache_miss(0, Usage(input_tokens=500, cache_creation=500), "deepseek-chat", True) == (0, 0.0)
    # cache_creation (write premium) counts toward the full prompt, priced at 1.25x pin
    tok2, usd2 = cache_miss(10000, Usage(input_tokens=0, cache_creation=10050, cache_read=0),
                            "deepseek-chat", True)
    assert tok2 == 10000 and usd2 > 0, (tok2, usd2)
    assert NOISE_FLOOR_TOKENS == 1024

def test_cache_ledger_clean_run():
    """Regression LOCK: a healthy full-prefix cache must show ZERO waste and no 'unexplained' miss.
    Runtime complement to test_prefix_cache_stability (which only proves composer byte-stability)."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    h = make_harness(os.getcwd(), provider="mock", project="ledger_clean", embed="hash")
    h.max_turns = 4
    script = [
        lambda m: Completion(tool_calls=[ToolCall("t0", "bash", {"command": "ls"})], stop_reason="tool_use"),
        lambda m: Completion(tool_calls=[ToolCall("t1", "bash", {"command": "pwd"})], stop_reason="tool_use"),
        lambda m: Completion(text="done", stop_reason="end_turn"),
    ]
    h.provider = _HonestCacheProvider(script)
    res = h.run("ledger_clean", "poke around")
    assert res.cache_waste_usd == 0, "clean full-prefix cache run must show $0 waste, got %s" % res.cache_waste_usd
    assert res.cache_miss_tokens == 0, res.cache_miss_tokens

_BIG = "python3 -c \"print('X'*2600)\""   # deterministic ~2.6KB tool output (> the 1024-tok floor once history accrues)

def test_cache_ledger_schema_cause():
    """A mid-run tool-set change busts the prefix before the messages; the miss must be attributed to
    'schema'. Driven by the real hard_at force-edit restriction (10 schemas -> read/edit/write),
    which the v0.13 restriction and v0.17 load_tools both shipped with zero cache-cost visibility."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    h = make_harness(os.getcwd(), provider="mock", project="ledger_schema", embed="hash")
    h.max_turns = 8
    h.force_edit = True                    # hard_at (turn 6) drops the tool set -> schema change
    causes = []
    h.emit = lambda kind, d: (causes.append(d.get("cause")) if kind == "cache_miss" else None)
    # never edit: keep calling bash with a big output so a real message history accrues to re-bill
    h.provider = _HonestCacheProvider(
        [lambda m: Completion(tool_calls=[ToolCall("b", "bash", {"command": _BIG})], stop_reason="tool_use")])
    h.run("ledger_schema", "poke")
    assert any(c and "schema" in c for c in causes), "tool-set change must carry 'schema' cause: %s" % causes

def test_cache_ledger_elide_cause():
    """History elision newly stubbing a >240-char tool output past turn N busts a full-prefix cache;
    the miss must be attributed to 'elide', not 'unexplained'."""
    from harness.cli import make_harness
    from harness.providers import Completion, ToolCall
    h = make_harness(os.getcwd(), provider="mock", project="ledger_elide", embed="hash")
    h.max_turns = 22
    causes = []
    h.emit = lambda kind, d: (causes.append(d.get("cause")) if kind == "cache_miss" else None)
    # big bash outputs every turn so the recent window is large; once history crosses the 14-msg
    # boundary, old >240-char outputs get stubbed -> prefix bust re-billing the big recent window
    def mk(i):
        return lambda m: (Completion(text="fin", stop_reason="end_turn") if i >= 20
                          else Completion(tool_calls=[ToolCall("b%d" % i, "bash", {"command": _BIG})],
                                          stop_reason="tool_use"))
    h.provider = _HonestCacheProvider([mk(i) for i in range(22)])
    h.run("ledger_elide", "keep poking")
    assert any(c and "elide" in c for c in causes), "elision miss must carry 'elide' cause: %s" % causes

def test_recorder_cache_migration():
    """A runs.db created with the OLD (pre-ledger) schema must migrate in place: Recorder ALTERs the
    missing columns, and log_turn/finish_run with the new cache kwargs succeed against it."""
    import sqlite3
    from harness.recorder import Recorder, RunResult
    d = tempfile.mkdtemp()
    dbp = os.path.join(d, "old.db")
    old = sqlite3.connect(dbp)
    old.execute("""CREATE TABLE runs(run_id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER,
        task_id TEXT, harness TEXT, model TEXT, provider TEXT, prefix_tokens INTEGER,
        input_tokens INTEGER, output_tokens INTEGER, total_tokens INTEGER, cache_read INTEGER,
        turns INTEGER, tool_calls INTEGER, mem_recalls INTEGER, wall_ms INTEGER, success INTEGER,
        quality REAL DEFAULT 0, cost_usd REAL DEFAULT 0, answer TEXT, error TEXT, note TEXT)""")
    old.execute("""CREATE TABLE turns(run_id INTEGER, idx INTEGER, kind TEXT, detail TEXT,
        tokens_in INTEGER, tokens_out INTEGER, prefix_tokens INTEGER, ms INTEGER)""")
    old.commit(); old.close()
    rec = Recorder(dbp)                    # must ALTER, not crash
    rid = rec.start_run("t", "collie", "deepseek-chat", "deepseek")
    rec.log_turn(rid, 0, "tool_use", "d", 10, 5, 700, 12, cache_read=600, cache_miss=1200, miss_cause="schema")
    r = RunResult(run_id=rid, cache_miss_tokens=1200, cache_waste_usd=0.0024, prefix_measured=812,
                  cache_creation=50, verified=True, contract_repairs=1)
    rec.finish_run(r)
    row = rec.db.execute("SELECT cache_miss_tokens, cache_waste_usd, prefix_measured, cache_creation, "
                         "verified, contract_repairs "
                         "FROM runs WHERE run_id=?", (rid,)).fetchone()
    assert row["cache_miss_tokens"] == 1200 and abs(row["cache_waste_usd"] - 0.0024) < 1e-9
    assert row["prefix_measured"] == 812 and row["cache_creation"] == 50
    assert row["verified"] == 1
    assert row["contract_repairs"] == 1
    trow = rec.db.execute("SELECT cache_miss, miss_cause FROM turns WHERE run_id=?", (rid,)).fetchone()
    assert trow["cache_miss"] == 1200 and trow["miss_cause"] == "schema"
    rec.close()

def test_usage_no_double_count():
    """#13 regression lock: OpenAI prompt_tokens INCLUDES cached; input_tokens must be UNCACHED so
    input+cache_read == the full input (no double count). This test is the lock backlog #13 lacked."""
    from harness.providers import _openai_usage
    u = _openai_usage({"prompt_tokens": 1000, "completion_tokens": 50,
                       "prompt_tokens_details": {"cached_tokens": 800}})
    assert u.input_tokens == 200 and u.cache_read == 800, (u.input_tokens, u.cache_read)
    assert u.input_tokens + u.cache_read + u.cache_creation == 1000, "full input must not double-count"
    assert u.output_tokens == 50

def test_prefix_measured_anchor_rules():
    """Copy pi's anchor skip-rules: an errored/empty turn-0 leaves prefix_measured None (never a
    plausible-but-wrong number); a real cache-carrying anthropic turn-0 records the measured prefix."""
    from harness.cli import make_harness
    from harness.providers import Completion, Usage
    # error turn-0 -> stays None
    h = make_harness(os.getcwd(), provider="mock", project="pm_err", embed="hash")
    h.max_turns = 1
    h.provider.name = "anthropic"
    h.provider.complete = lambda s, m, t, on_text=None: Completion(text="x", stop_reason="error",
                                                                   usage=Usage())
    res = h.run("pm_err", "hi")
    assert res.prefix_measured is None, "errored turn-0 must leave prefix_measured unmeasured"
    # clean cache-carrying anthropic turn-0 -> measured == cache_creation + cache_read
    h2 = make_harness(os.getcwd(), provider="mock", project="pm_ok", embed="hash")
    h2.max_turns = 1
    h2.provider.name = "anthropic"
    h2.provider.complete = lambda s, m, t, on_text=None: Completion(
        text="done", stop_reason="end_turn", usage=Usage(input_tokens=5, cache_creation=900))
    res2 = h2.run("pm_ok", "hi")
    assert res2.prefix_measured == 900, res2.prefix_measured

def test_measure_prefix_differential():
    """measure_prefix returns A-B (full prefix minus bare request); the bare side sends NO tools
    param (guards the #16 empty-tools 400)."""
    from harness.providers import measure_prefix, Completion, Usage
    seen_schemas = []
    class P:
        name = "fake"; model = "deepseek-chat"; max_tokens = 4096
        def complete(self, system, messages, tool_schemas, on_text=None):
            seen_schemas.append(len(tool_schemas))
            return Completion(usage=Usage(input_tokens=len(system) + 10 * len(tool_schemas)))
    m = measure_prefix(P(), "SYSTEM-PROMPT", [{"name": "a"}, {"name": "b"}])
    # A = len("SYSTEM-PROMPT")=13 + 20 = 33 ; B = len(".")=1 + 0 = 1 ; A-B = 32
    assert m == 32, m
    assert seen_schemas == [2, 0], "bare side must send zero tool schemas: %s" % seen_schemas

def test_prefix_ceiling_warns():
    """#14: the prefix_ceiling was never enforced. It must at least WARN (emit) when est > ceiling."""
    from harness.cli import make_harness
    h = make_harness(os.getcwd(), provider="mock", project="ceil", embed="hash", prefix_ceiling=10)
    events = []
    h.emit = lambda kind, d: events.append((kind, d))
    h.max_turns = 1
    h.run("ceil", "hello")
    warns = [d for k, d in events if k == "prefix_ceiling"]
    assert warns and warns[0]["est"] > warns[0]["ceiling"], "must emit prefix_ceiling when est exceeds it"

# ------------------------------------------------------------------ context history trimming
def test_context_trimming_preserves_pairing():
    from harness.cli import make_harness
    from harness.providers import ToolCall, AnthropicProvider
    h = make_harness(os.getcwd(), provider="mock", project="ctxtest", embed="hash")
    msgs = []
    for i in range(20):   # long interleaved history: assistant(tool_use) -> tool(result), big outputs
        msgs.append({"role": "user", "content": "q%d" % i})
        msgs.append({"role": "assistant", "tool_calls": [ToolCall("tc%d" % i, "read_file", {"path": "/x%d" % i})]})
        msgs.append({"role": "tool", "tool_call_id": "tc%d" % i, "name": "read_file", "content": "X" * 500})
    system, pmsgs, meta = h.composer.build({"messages": msgs}, "next", os.getcwd(), "ctxtest")
    assert len(pmsgs) == len(msgs), "trimming must NOT drop messages (would orphan tool_use/result)"
    old_tool = [m for m in pmsgs[:len(pmsgs) - 14] if m.get("role") == "tool"]
    assert old_tool and all("elided" in m["content"] for m in old_tool), "old tool outputs must be stubbed"
    recent_tool = [m for m in pmsgs[len(pmsgs) - 14:] if m.get("role") == "tool"]
    assert all("elided" not in m["content"] for m in recent_tool), "recent tool outputs must stay full"
    # the API-validity invariant: every tool_result must be preceded by its matching tool_use
    an = AnthropicProvider.__new__(AnthropicProvider)._to_anthropic(pmsgs)
    seen = set()
    for m in an:
        c = m["content"]
        if isinstance(c, list):
            for b in c:
                if b.get("type") == "tool_use": seen.add(b["id"])
                if b.get("type") == "tool_result":
                    assert b["tool_use_id"] in seen, "orphaned tool_result -> provider 400: %s" % b["tool_use_id"]

# ------------------------------------------------------------------ context: unknown mode keeps tools
def test_context_unknown_mode_keeps_act():
    from harness.cli import make_harness
    h = make_harness(os.getcwd(), provider="mock", project="modetest", embed="hash")
    sysb, _, _ = h.composer.build({"messages": []}, "hi", os.getcwd(), "modetest", mode="bogusmode")
    assert "MODE: Act" in sysb, "an unknown/typo mode must fall back to Act, not silently drop the tool contract"

# ------------------------------------------------------------------ PERF: prefix-cache stability
def test_prefix_cache_stability():
    from harness.cli import make_harness
    h = make_harness(os.getcwd(), provider="mock", project="perftest", embed="hash")
    sess = {"messages": [{"role": "user", "content": "fix the bug"}]}
    sys1, _, _ = h.composer.build(sess, "fix the bug", os.getcwd(), "perftest")
    sys2, _, _ = h.composer.build(sess, "fix the bug", os.getcwd(), "perftest")
    assert sys1 == sys2, "system prefix must be byte-STABLE across turns — else prompt caching (collie's core efficiency lever) is defeated every turn"
    now = [l for l in sys1.split("\n") if l.startswith("NOW:")]
    assert now and ":" not in now[0].split("NOW:", 1)[1], "NOW must be date-only (a per-minute timestamp busts the whole cached prefix): %r" % now

if __name__ == "__main__":                 # LAST, always: a guard with definitions after it
    sys.exit(run_module(globals(), "CONTEXT"))  # silently skips every one of them.
