"""Everything with no larger neighbourhood: sessions, costs, settings, updates,
checkpoints, the dashboard and the index.

Split out of test_core.py — a pure move; no assertion was changed. Stdlib-only, no Opus, fast.
    python tests/test_core.py     (exit 0 = all pass)
"""
import inspect, io, json, os, re, sys, tempfile, time, types, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from _util import _ctx, _Skip, _RecordingMemory, _ScriptProvider, run_module  # noqa: E402,F401

import contextlib
import inspect, io, json, os, re, sys, tempfile, time, types, warnings

# ------------------------------------------------------------------ sessions
def test_sessions_toolcall_roundtrip():
    from harness import sessions as S
    from harness.providers import ToolCall
    sid = S.new_id()
    msgs = [{"role": "user", "content": "hi"},
            {"role": "assistant", "tool_calls": [ToolCall("tc1", "read_file", {"path": "/x"})]},
            {"role": "tool", "tool_call_id": "tc1", "name": "read_file", "content": "data"}]
    S.save(sid, msgs, project="test")
    loaded = S.load(sid)["messages"]
    tc = loaded[1]["tool_calls"][0]
    assert isinstance(tc, ToolCall), "tool_call must reload as ToolCall, got %r" % type(tc)
    assert tc.id == "tc1" and tc.name == "read_file" and tc.args == {"path": "/x"}
    S.delete(sid)

def test_sessions_legacy_str_recovery():
    from harness import sessions as S
    from harness.providers import ToolCall
    # simulate an OLD corrupt session (default=str turned ToolCall into its repr string)
    msgs = [{"role": "assistant", "tool_calls": ["ToolCall(id='old1', name='grep', args={'pattern': 'x'})"]}]
    got = S._msgs_in(msgs)[0]["tool_calls"]
    assert got and isinstance(got[0], ToolCall) and got[0].id == "old1", "legacy str must recover to ToolCall"

def test_sessions_path_traversal():
    from harness import sessions as S
    d = os.path.realpath(S._dir())
    for bad in ["../../etc/passwd", "/etc/passwd", "..", ".", "a\\b", "a/b/c"]:
        p = S._path(bad)
        assert p is None or os.path.dirname(os.path.realpath(p)) == d, "traversal escaped: %r -> %r" % (bad, p)
    assert S._path("good-id_123") is not None

def test_sessions_corrupt_json():
    from harness import sessions as S
    p = os.path.join(S._dir(), "corrupt-test.json")
    open(p, "w").write("{ this is not json")
    assert S.load("corrupt-test") is None, "corrupt JSON must return None, not crash"
    os.remove(p)

# ------------------------------------------------------------------ costs
def test_cost_cache_creation():
    from harness.costs import cost_usd
    base = cost_usd("claude-opus-4-8", 1000, 500, cache_read=2000)
    withc = cost_usd("claude-opus-4-8", 1000, 500, cache_read=2000, cache_creation=1000)
    assert abs((withc - base) - (1000 * 15 * 1.25 / 1e6)) < 1e-9, "cache-creation must bill at 1.25x input"

def test_cost_unknown_model_zero():
    from harness.costs import cost_usd
    assert cost_usd("some-unlisted-model", 1000, 500) == 0.0
    assert cost_usd("claude-opus-4-8", 1_000_000, 0) == 15.0   # opus input $15/M

# ------------------------------------------------------------------ embeddings cache
def test_embedder_singleton():
    from harness.embeddings import make_embedding, _EMB_CACHE
    a = make_embedding("hash"); b = make_embedding("hash")
    assert a is b, "make_embedding must cache (per-request reload was the OOM leak)"

def test_panel_settings_survive_a_fork():
    """A setting the panel saved must reach a child process, not be locked out by inheritance.

    apply() exports every saved setting as COLLIE_<KEY>. The desktop app spawns the web server as
    a child, which inherits those exports; its own _HARD_ENV snapshot then classed them as "the
    user set this in their environment", and apply() skips a hard-set key forever. Measured on a
    live machine: settings.json said LANG=zh and the running server answered en — the panel saved,
    the file was right, and nothing read it. Silent in both directions.
    """
    import subprocess as _sp
    state = tempfile.mkdtemp(prefix="forksettings-")
    # Paths travel by environment, never interpolated into the generated source. The grandchild's
    # source is a string literal INSIDE the parent's string literal, so a %r path lost one level of
    # escaping there: on Windows `sys.path.insert(0, 'C:\Users\…')` made \U a truncated unicode
    # escape, the grandchild died of SyntaxError before printing, and the assert blamed settings.
    # Non-COLLIE_ names on purpose — apply() treats every inherited COLLIE_* as "the user set this".
    env = {**os.environ, "COLLIE_STATE_DIR": state,
           "FORKTEST_REPO": os.getcwd(), "FORKTEST_STATE": state}
    parent = ("import os, sys\n"
              "sys.path.insert(0, os.environ['FORKTEST_REPO'])\n"
              "from harness import settings as st\n"
              "st.save({'LANG': 'en'})\n"          # what the process started with
              "st.apply()\n"
              "st.save({'LANG': 'zh'})\n"          # the user changes it in the panel
              "child = os.path.join(os.environ['FORKTEST_STATE'], 'c.py')\n"
              "open(child, 'w').write(\"import os, sys\\n\"\n"
              "  \"sys.path.insert(0, os.environ['FORKTEST_REPO'])\\n\"\n"
              "  \"from harness import settings as st\\n\"\n"
              "  \"st.apply()\\n\"\n"
              "  \"print(st.get('LANG', 'auto'))\\n\")\n"
              "import subprocess\n"
              "print(subprocess.run([sys.executable, child], capture_output=True, text=True,\n"
              "                     env=dict(os.environ)).stdout.strip())\n")
    pf = os.path.join(state, "p.py")
    with open(pf, "w", encoding="utf-8") as f:
        f.write(parent)
    r = _sp.run([sys.executable, pf], capture_output=True, text=True, env=env, timeout=120)
    got = (r.stdout or "").strip().splitlines()[-1:] or [""]
    assert got[0] == "zh", \
        "the child must see the panel's value, got %r (stderr: %s)" % (got[0], (r.stderr or "")[:200])

def test_launch_failure_carries_a_reason():
    """`could not launch X` names the outcome and hides the cause. launch_detail keeps the reason."""
    from harness import desktop
    ok, why = desktop.launch_detail(os.path.join(os.getcwd(), "definitely-not-here-xyz.app"))
    assert ok is False and "does not exist" in why, "expected the missing path to be named: %r" % why
    ok, why = desktop.launch_detail("")
    assert ok is False and why, "an empty target must still say why"

def test_update_handoff_does_not_detach_the_bootstrap():
    """DETACHED_PROCESS silently does nothing here, which is the worst way for it to be wrong.

    The Windows self-update hands the installer to a PowerShell bootstrap, because the installer
    closes whatever holds the files it is replacing — including the updater itself, which lives in
    that directory. Launched with DETACHED_PROCESS the bootstrap gets no console, powershell exits
    without running a line, and Popen still returns a healthy process object: the handoff reports
    success and nothing whatsoever happens. Measured both ways; CREATE_NO_WINDOW alone works, and a
    child already outlives its parent on Windows.
    """
    import inspect as _i
    from harness import update as up
    src = _i.getsource(up.apply_windows)
    launch = src[src.index("powershell.exe"):]
    assert "0x00000008" not in launch and "DETACHED" not in launch.upper().replace("DETACHED_PROCESS:", ""), \
        "the bootstrap must not be launched detached — it silently never runs"
    # Through plat.no_window_kwargs(), not a bare `creationflags=`: passing that keyword at all
    # raises ValueError off Windows, and the platform-purity check rejects it outside plat.py. The
    # property this test is about — CREATE_NO_WINDOW and nothing else — is what the helper returns
    # on Windows; the assertion follows the expression, not the other way round.
    assert "no_window_kwargs()" in launch, "expected CREATE_NO_WINDOW (via plat) for the bootstrap"

def test_windows_update_refuses_a_guard_owned_handoff():
    from harness import update as up
    src = inspect.getsource(up.apply_windows)
    assert "COLLIE_PROCESS_OWNER" in src and "cannot be handed off" in src, \
        "a Slack-owned bootstrap would be killed with its guard and must not report success"

def test_update_bootstrap_waits_installs_and_refuses_to_restart_after_a_failure():
    from harness import update as up
    s = up._BOOTSTRAP.format(pid=4242, exe="C:\\x\\setup.exe", root="C:\\r",
                             log="C:\\l.log", restarts='"noop"')
    assert "Get-Process -Id 4242" in s, "it must wait for the caller to exit before installing"
    assert "-Wait" in s, "it must wait for the installer, or it restarts Collie mid-install"
    assert "installer exit code" in s, "the installer's exit code has to be recorded somewhere"
    assert "not restarting anything" in s, \
        "a failed install must not be followed by a restart that hides it"

def test_update_tells_wallpaper_and_window_apart():
    """Both are the same exe; only `--window` separates them, and the server port cannot."""
    import inspect as _i
    from harness import update as up
    src = _i.getsource(up.running_parts)
    assert "--window" in src, "the window must be identified by its command line"
    # strip comments: the comment that explains why 8787 is wrong must not read as using it
    code = "\n".join(l.split("#", 1)[0] for l in src.splitlines())
    assert "8787" not in code, \
        "8787 is the server and the wallpaper holds it too — using it opens a window that was never there"

def test_update_inventory_and_restart_include_live_slack_listener():
    """An installer kills bundled pythonw, so a live dog must be explicit restart inventory."""
    from harness import update as up
    home = tempfile.mkdtemp(prefix="collie-update-slack-")
    kennel = os.path.join(home, ".collie")
    os.makedirs(kennel)
    launcher = os.path.join(kennel, "slack-cornetto.pyw")
    open(launcher, "w", encoding="utf-8").close()
    root = os.path.join(home, "Programs", "Collie")
    runtime = os.path.join(root, "python", "pythonw.exe")

    class Result:
        returncode = 0
        stdout = '"%s" "%s"\n' % (runtime, launcher)
        stderr = ""

    real_run, real_expand = up.subprocess.run, up.os.path.expanduser
    up.subprocess.run = lambda *a, **k: Result()
    up.os.path.expanduser = lambda path: home if path == "~" else real_expand(path)
    try:
        parts = up.running_parts(root)
    finally:
        up.subprocess.run, up.os.path.expanduser = real_run, real_expand
    assert "slack:slack-cornetto.pyw" in parts, \
        "the active bundled Slack launcher must survive installer process teardown"
    restart = up._restart_script("slack:slack-cornetto.pyw", root)
    assert ("slack-cornetto.pyw" in restart and "Start-Process" in restart
            and restart.count("[char]34") == 2), \
        "post-install restart must use the newly installed pythonw with that exact launcher"
    assert up._restart_script("slack:../bad.pyw", root) == "", \
        "restart inventory cannot inject an arbitrary path into PowerShell"

def test_new_windows_installer_migrates_pre_slack_updaters():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    iss = open(os.path.join(root, "installer", "collie.iss"), encoding="utf-8").read()
    prepare = iss.split("function PrepareToInstall", 1)[1].split("procedure InitializeWizard", 1)[0]
    run = iss.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
    assert "taskkill.exe /PID $_.Id /T /F" in prepare, \
        "the first upgrade must quiesce each legacy listener's external child tree"
    assert "slack-*.pyw" in run and "subprocess.Popen([sys.executable,p]" in run, \
        "the new installer itself must restart Slack when the old updater did not inventory it"

def test_macos_update_kickstarts_loaded_slack_agents():
    from harness import update as up
    install = inspect.getsource(up.apply_macos)
    restart = inspect.getsource(up._restart_slack_agents)
    assert "_loaded_slack_agents" in install and "_restart_slack_agents" in install, \
        "the app swap must inventory listeners before replacement and restart them after"
    assert '"kickstart", "-k"' in restart and '"bootstrap"' in restart, \
        "a loaded old runtime is replaced, with re-bootstrap if it vanished during the swap"

def test_pack_selection():
    from harness.pack import select
    # a check filters to passing attempts only
    a = [{"idx": 0, "check_pass": False, "verified": True, "answer": "x", "turns": 1},
         {"idx": 1, "check_pass": True, "verified": False, "answer": "y", "turns": 5}]
    assert select(a, True)[0] == 1, "only check-passing attempts eligible"
    # no check: verified beats fewer turns
    b = [{"idx": 0, "verified": False, "answer": "x", "turns": 1},
         {"idx": 1, "verified": True, "answer": "y", "turns": 9}]
    assert select(b, False)[0] == 1, "verified wins"
    # check given, none pass -> refuse (no winner, don't ship a wrong edit)
    assert select([{"idx": 0, "check_pass": False, "verified": True, "turns": 1}], True)[0] is None

def test_settings_layering():
    from harness import settings as S
    import tempfile, json
    p = os.path.join(tempfile.gettempdir(), "collie_settings_unit.json")
    old = S._PATH
    env_had = os.environ.pop("COLLIE_MODEL", None)  # isolate from ambient env
    try:
        S._PATH = p; S._cache["mtime"] = -1.0
        S.save({"MODEL": "m-from-json", "JUNK": "x"})
        assert "JUNK" not in json.load(open(p)), "unknown keys must be dropped"
        assert S.get("MODEL") == "m-from-json"
        os.environ["COLLIE_MODEL"] = "m-from-env"
        assert S.get("MODEL") == "m-from-env", "real env var must win over settings.json"
    finally:
        os.environ.pop("COLLIE_MODEL", None)
        if env_had is not None:
            os.environ["COLLIE_MODEL"] = env_had
        S._PATH = old; S._cache["mtime"] = -1.0
        try: os.remove(p)
        except OSError: pass

# ------------------------------------------------------------------ web_search resilience (no network)
def test_websearch_graceful():
    from harness.websearch import WebSearchTool
    ws = WebSearchTool()
    assert isinstance(ws.run({"query": ""}, _ctx(tempfile.gettempdir())), str), "empty query must return a str, not crash"
    assert isinstance(ws.run({}, _ctx(tempfile.gettempdir())), str), "MISSING query key must not crash the tool"

# ------------------------------------------------------------------ compare grading word-boundary
def test_compare_num_in_boundary():
    from harness.compare import _num_in
    assert _num_in("there are 7 files", 7)
    assert not _num_in("17 files here", 7), "'7' must not false-match inside '17'"
    assert not _num_in("13 tests, 0 fail", 3), "'3' must not false-match inside '13'"
    assert not _num_in("test_3.py", 3)

# ------------------------------------------------------------------ sessions.set_title preserves ToolCall
def test_sessions_set_title_roundtrip():
    from harness import sessions as S
    from harness.providers import ToolCall
    sid = S.new_id()
    S.save(sid, [{"role": "assistant", "tool_calls": [ToolCall("tc9", "grep", {"p": "x"})]}], project="t")
    assert S.set_title(sid, "My Thread")
    reloaded = S.load(sid)
    assert reloaded["title"] == "My Thread"
    tc = reloaded["messages"][0]["tool_calls"][0]
    assert isinstance(tc, ToolCall) and tc.id == "tc9", "set_title must NOT re-stringify tool_calls"
    S.delete(sid)

# ------------------------------------------------------------------ memory recall (global-union + dedup)
def test_memory_global_union_and_dedup():
    from harness.memory import SqliteMemory
    m = SqliteMemory(tempfile.mktemp(), embedder=None)   # default hash embedder
    m.remember("collie prefers dark mode", keys="ui theme", project="global")
    hits = list(m.recall("dark mode theme", project="acme", k=5))
    assert any("dark mode" in h["text"] for h in hits), "a global fact must be reachable from a project-scoped recall"
    m.remember("deploy prod on Friday", keys="deploy", project="p1")
    m.remember("deploy prod on Monday", keys="deploy", project="p1")
    fri = list(m.recall("deploy Friday", project="p1", k=5))
    mon = list(m.recall("deploy Monday", project="p1", k=5))
    assert any("Friday" in h["text"] for h in fri) and any("Monday" in h["text"] for h in mon), \
        "distinct facts must NOT be false-merged by dedup under the weak hash embedder"

# ------------------------------------------------------------------ dashboard HTML-escapes run data
def test_dashboard_escapes_adversarial():
    import harness.dashboard as D
    from harness.recorder import Recorder, RunResult
    d = tempfile.mkdtemp(); runs_db = os.path.join(d, "runs.db"); out = os.path.join(d, "x.html")
    r = Recorder(runs_db)
    rid = r.start_run("<script>alert(1)</script>", "collie", "<img src=x onerror=alert(1)>", "mock")
    r.finish_run(RunResult(run_id=rid, task_id="<script>alert(1)</script>", harness="collie", model="m", success=True))
    r.close()
    import json as _j
    _j.dump({"n": 5, "resolve": [{"harness": "<script>evil</script>", "resolved": 1, "total": 5, "note": "x"}]},
            open(os.path.join(d, "swebench.json"), "w"))
    html = open(D.build(runs_db, out)).read()
    # every angle bracket from run/config data must be escaped — no LIVE <script>/<img> tag survives
    assert "<script>alert" not in html and "<script>evil" not in html, "dashboard must escape run/config HTML"
    assert "<img src=x onerror" not in html, "model field must be escaped (no live img tag)"

# ------------------------------------------------------------------ codeindex invalidate
def test_codeindex_ripgrep_fresh():
    """code_search is ripgrep-backed (no vector index): results always reflect CURRENT file
    contents, so it never serves stale line numbers and invalidate() is a compatibility no-op."""
    from harness import codeindex as C
    d = tempfile.mkdtemp()
    p = os.path.join(d, "mod.py")
    open(p, "w", encoding="utf-8").write("def find_widget_by_name(x):\n    return x\n")
    idx = C.get_index(d)
    hits = idx.search("find_widget_by_name", k=3)
    assert any("mod.py" in h and "find_widget_by_name" in h for h in hits), hits
    # change the file; WITHOUT invalidate the next search must reflect the new symbol (freshness)
    open(p, "w", encoding="utf-8").write("def resolve_gadget_ref(x):\n    return x\n")
    C.invalidate(d)                                   # no-op, but must stay callable/safe
    hits2 = idx.search("resolve_gadget_ref", k=3)
    assert any("resolve_gadget_ref" in h for h in hits2), hits2
    assert idx.search("find_widget_by_name", k=3) == [] or \
        all("find_widget_by_name" not in h for h in idx.search("find_widget_by_name", k=3))

def test_every_agent_cli_is_resolved_on_path_before_exec():
    """A competitor that cannot start must be an error, not a loss.

    On Windows CreateProcess ignores PATHEXT, so `subprocess.run(["claude", ...])` raises
    FileNotFoundError in ~0.2s and never runs anything. Twice now a comparison run recorded that
    as "the other harness produced no patch" (a bogus 10:0, then a bogus 2:0). Fixing the call
    site in adapters.py did not fix the identical call in swe.py, so lock the CLASS: every place
    that execs an external agent CLI resolves argv[0] through shutil.which first.
    """
    import ast
    from harness import swe, adapters
    for mod in (swe, adapters):
        src = inspect.getsource(mod)
        assert "shutil.which" in src, "%s execs a CLI without resolving it on PATH" % mod.__name__
        tree = ast.parse(src)
        # no subprocess.run/Popen whose argv[0] is a bare string literal command name
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fn = node.func
            name = getattr(fn, "attr", "") or getattr(fn, "id", "")
            if name not in ("run", "Popen"):
                continue
            argv = node.args[0]
            if not isinstance(argv, ast.List) or not argv.elts:
                continue
            first = argv.elts[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                # git/where/powershell are OS built-ins with real .exe files; the agent CLIs
                # (claude, hermes, codex, ...) are npm shims that only exist as .cmd/.ps1
                assert first.value in ("git", "where", "powershell", "docker", "sg", "cmd",
                                       "systemd-run"), (
                    "%s: exec of bare %r — resolve it with shutil.which first"
                    % (mod.__name__, first.value))

def test_multiline_prompt_is_never_passed_as_a_windows_argv():
    """cmd.exe ends a command at a newline, so a multi-line prompt in argv arrives truncated.

    Empirically: a 1007-char problem statement reached `claude` as its FIRST LINE ONLY via argv,
    and complete via stdin. The agent then said it had no issue body, edited nothing, exited 0 —
    and the paired benchmark scored that as a loss for the other harness. Silent truncation of the
    task itself is the most expensive lie a comparison harness can tell, so _run_cli must refuse.
    """
    from harness import swe
    with tempfile.TemporaryDirectory() as wd:
        try:
            swe._run_cli(["git", "status", "line one\nline two"], wd)
        except ValueError as e:
            assert "newline" in str(e) and "stdin_text" in str(e)
        else:
            if os.name == "nt":
                raise AssertionError("_run_cli accepted a multi-line argv on Windows")
    # and the real caller must use the stdin path
    src = inspect.getsource(swe.predict_claude_code)
    assert "stdin_text=" in src, "predict_claude_code still puts the prompt in argv"

def test_a_provider_outage_is_not_scored_as_a_failed_attempt():
    """Collie reports provider failures in RunResult.error and returns NORMALLY — it does not
    raise. A comparison runner that only catches exceptions therefore books a quota outage as
    "produced no patch". That happened: two 16-second, one-turn, zero-byte runs were scored as
    losses while the Claude arm's identical outage was reported correctly (it exits non-zero).
    Same outage, opposite bookkeeping, and the bookkeeping decided the result.
    """
    import inspect as _i
    from bench import paired_eval
    src = _i.getsource(paired_eval.run_collie)
    assert 'getattr(rr, "error"' in src, "run_collie ignores RunResult.error again"
    # and the loop must actually populate it on a provider error
    from harness import loop
    lsrc = _i.getsource(loop)
    assert 'if comp.stop_reason == "error":' in lsrc and "res.error = " in lsrc

def test_both_arms_record_cache_tokens_and_cost():
    """A cost figure without cache reads is several times too high, and unrecorded is unrecoverable.

    The first graded run measured Collie's tokens and NOTHING for Claude Code (plain -p returns
    only the answer), so efficiency could not be compared at all. Worse, Collie's own figure
    omitted cache reads: a live check shows a run with 6 fresh input tokens against 117,696 cached
    ones, so pricing all input at the uncached rate overstates spend by orders of magnitude. Cold
    runs delete their store, so a field not captured at the call site is gone for good.
    """
    import inspect as _i
    from bench import paired_eval
    from harness import swe
    collie_src = _i.getsource(paired_eval.run_collie)
    for field in ("cache_read", "cache_creation", "cost_usd"):
        assert field in collie_src, "run_collie stopped recording %s" % field
    claude_src = _i.getsource(paired_eval.run_claude)
    assert "cache_read_input_tokens" in claude_src and "total_cost_usd" in claude_src
    # the CLI only reports usage in json mode
    assert '"--output-format", "json"' in _i.getsource(swe.predict_claude_code)

def _mkrepo(d):
    import subprocess as sp
    sp.run(["git", "init", "-q", d], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t"), ("commit.gpgsign", "false")):
        sp.run(["git", "-C", d, "config", k, v], check=True)
    with open(os.path.join(d, "tracked.txt"), "w") as f:
        f.write("original\n")
    sp.run(["git", "-C", d, "add", "-A"], check=True)
    sp.run(["git", "-C", d, "commit", "-qm", "init"], check=True)

def test_checkpoint_rewinds_edits_new_files_and_deletions():
    """A checkpoint the user relies on must restore all three kinds of damage an agent can do:
    modify a tracked file, create a new one, and delete one. Untracked files are the case a
    plain `git stash` loses, which is why the snapshot carries them as a third parent."""
    from harness import checkpoints as cp
    with tempfile.TemporaryDirectory() as d:
        _mkrepo(d)
        with open(os.path.join(d, "untracked.txt"), "w") as f:
            f.write("keep me\n")
        ok, why = cp.available(d)
        assert ok, why
        c = cp.capture(d, "s1", 1, "before edits")

        with open(os.path.join(d, "tracked.txt"), "w") as f:
            f.write("AGENT BROKE THIS\n")
        with open(os.path.join(d, "untracked.txt"), "w") as f:
            f.write("AGENT BROKE THIS TOO\n")
        with open(os.path.join(d, "new.txt"), "w") as f:
            f.write("agent made this\n")

        info = cp.restore(d, c)
        assert info["untracked_rewound"] is True, info
        assert open(os.path.join(d, "tracked.txt")).read() == "original\n"
        assert open(os.path.join(d, "untracked.txt")).read() == "keep me\n"
        assert not os.path.exists(os.path.join(d, "new.txt")), "a file created after the checkpoint survived"

def test_checkpoint_never_touches_the_users_stash_list_or_index():
    """Taking a snapshot must be invisible: `git stash create` does not move the worktree, the
    private ref namespace keeps it out of `git stash list`, and untracked files are staged into a
    THROWAWAY index so anything the user had staged survives."""
    import subprocess as sp
    from harness import checkpoints as cp
    with tempfile.TemporaryDirectory() as d:
        _mkrepo(d)
        with open(os.path.join(d, "staged.txt"), "w") as f:
            f.write("i was staged\n")
        sp.run(["git", "-C", d, "add", "staged.txt"], check=True)
        with open(os.path.join(d, "untracked.txt"), "w") as f:
            f.write("u\n")
        with open(os.path.join(d, "tracked.txt"), "w") as f:
            f.write("dirty\n")

        cp.capture(d, "s1", 1)

        assert sp.run(["git", "-C", d, "stash", "list"], capture_output=True,
                      text=True).stdout.strip() == "", "checkpoint leaked into git stash list"
        staged = sp.run(["git", "-C", d, "diff", "--cached", "--name-only"],
                        capture_output=True, text=True).stdout.split()
        assert "staged.txt" in staged, "capture clobbered the user's index"
        assert open(os.path.join(d, "tracked.txt")).read() == "dirty\n", "capture moved the worktree"

def test_checkpoint_says_when_it_cannot_protect_you():
    """Silently not saving is worse than not offering: the user lets the agent run BECAUSE they
    believe a checkpoint exists."""
    from harness import checkpoints as cp
    with tempfile.TemporaryDirectory() as d:
        ok, why = cp.available(d)
        assert not ok and "not inside a git repository" in why
        try:
            cp.capture(d, "s1", 1)
        except cp.CheckpointError as e:
            assert "git repository" in str(e)
        else:
            raise AssertionError("capture returned a handle outside a git repo")

def test_checkpoint_refuses_to_stash_apply_an_ordinary_merge():
    """A snapshot is recognised by merge shape AND our marker. Shape alone would let a real merge
    commit reach `git stash apply`, which corrupts the tree."""
    import subprocess as sp
    from harness import checkpoints as cp
    with tempfile.TemporaryDirectory() as d:
        _mkrepo(d)
        sp.run(["git", "-C", d, "checkout", "-qb", "side"], check=True)
        with open(os.path.join(d, "side.txt"), "w") as f:
            f.write("s\n")
        sp.run(["git", "-C", d, "add", "-A"], check=True)
        sp.run(["git", "-C", d, "commit", "-qm", "side"], check=True)
        sp.run(["git", "-C", d, "checkout", "-q", "-"], capture_output=True)
        sp.run(["git", "-C", d, "merge", "-q", "--no-ff", "side", "-m", "a real merge"],
               capture_output=True)
        head = sp.run(["git", "-C", d, "rev-parse", "HEAD"], capture_output=True,
                      text=True).stdout.strip()
        assert cp._kind_of(d, head) == "commit", "an ordinary merge was mistaken for a snapshot"

def test_checkpoint_taken_on_a_clean_tree_still_removes_what_the_agent_created():
    """The commonest case: you check out clean, then ask the agent to do something.

    `git stash create` returns nothing on a clean tree, so the obvious implementation (Cline's)
    falls back to recording HEAD — and then restore dare not delete untracked files, leaving every
    file the agent created on disk. Found by restoring for real and watching new.txt survive.
    An EMPTY untracked set is complete knowledge, not missing knowledge: anything untracked at
    restore time must have appeared afterwards, so it is safe to remove.
    """
    from harness import checkpoints as cp
    with tempfile.TemporaryDirectory() as d:
        _mkrepo(d)                                   # clean tree, nothing untracked
        c = cp.capture(d, "s1", 1, "clean tree")
        assert c.kind == "stash", "clean tree fell back to a checkpoint that cannot rewind"
        with open(os.path.join(d, "tracked.txt"), "w") as f:
            f.write("BROKEN\n")
        with open(os.path.join(d, "new.txt"), "w") as f:
            f.write("agent made this\n")
        info = cp.restore(d, c)
        assert info["untracked_rewound"] is True, info
        assert open(os.path.join(d, "tracked.txt")).read() == "original\n"
        assert not os.path.exists(os.path.join(d, "new.txt"))

def test_rewind_button_is_hidden_when_nothing_can_be_rewound():
    """An undo control that cannot undo is worse than none — the user lets the agent run BECAUSE
    they believe it exists. The button starts hidden and only appears once a snapshot is listed."""
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "harness", "webui", "index.html"), encoding="utf-8").read()
    assert 'id="rewindBtn"' in html and 'id="rewindBtn" title=' in html
    btn = html[html.index('id="rewindBtn"'):]
    assert "hidden" in btn[:400], "rewind button is not hidden by default"
    assert "/api/checkpoints" in html and "/api/checkpoint/restore" in html
    # destructive: it must ask, and it must say what gets thrown away
    assert "window.confirm" in html and "cannot be undone" in html
    # and it must not imply untracked files came back when they could not
    assert "untracked_rewound" in html

def test_checkpoint_commits_carry_no_user_text():
    """These refs live under refs/, so `git log --all` and `git for-each-ref` list them.

    Putting the prompt in the commit subject wrote what the user asked Collie straight into their
    own repository. Real runs had already committed subjects like "What did we decide about the
    embedding memory design?" and a base64 image payload — visible to anyone who runs git log.
    """
    import subprocess as sp
    from harness import checkpoints as cp
    secret = "MY PRIVATE PROMPT about acquiring Foo Corp"
    with tempfile.TemporaryDirectory() as d:
        _mkrepo(d)
        cp.capture(d, "s1", 1, secret)
        log = sp.run(["git", "-C", d, "log", "--all", "--format=%s%n%b"],
                     capture_output=True, text=True).stdout
        assert secret not in log, "the prompt was written into the repository"
        refs = sp.run(["git", "-C", d, "for-each-ref", "--format=%(subject)"],
                      capture_output=True, text=True).stdout
        assert secret not in refs
        # and it must not come back out through the listing either
        assert all(secret not in (c.label or "") for c in cp.history(d))

def test_checkpoints_do_not_accumulate_forever():
    """One ref per run, never expiring, is thousands of pinned trees for an everyday user — the
    live repo was already at run 3940. Old snapshots are also the least useful ones."""
    from harness import checkpoints as cp
    with tempfile.TemporaryDirectory() as d:
        _mkrepo(d)
        for i in range(1, 9):
            with open(os.path.join(d, "tracked.txt"), "w") as f:
                f.write("v%d\n" % i)
            cp.capture(d, "s1", i)
        assert len(cp.history(d)) == 8
        removed = cp.prune(d, keep=3)
        assert removed == 5
        left = cp.history(d)
        assert [c.n for c in left] == [8, 7, 6], [c.n for c in left]

if __name__ == "__main__":                 # LAST, always: a guard with definitions after it
    sys.exit(run_module(globals(), "CORE"))  # silently skips every one of them.
