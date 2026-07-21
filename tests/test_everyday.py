"""Pin the everyday capabilities (harness.everyday): translate, web.summarize,
reminder.set. All ALWAYS deliver — none returns needs_you.

Run: python tests/test_everyday.py   (exit 0 = all green)
"""
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_state = tempfile.mkdtemp(prefix="collie-everyday-")
os.environ["COLLIE_STATE_DIR"] = _state
os.environ["COLLIE_NOTES_DIR"] = os.path.join(_state, "notes")
os.environ["COLLIE_WEBFETCH_ALLOW_LOCAL"] = "1"

from harness import everyday as E  # noqa: E402
from harness.jobs import clear_registry, get_capability  # noqa: E402
from harness import capabilities as caps  # noqa: E402
from harness.verifier import VERIFIED, FAILED  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


class _Prov:
    def __init__(self, text):
        self._t = text

    def complete(self, system, messages, tools, on_text=None):
        c = type("C", (), {})(); c.stop_reason = "end_turn"; c.text = self._t
        return c


def F(t):
    """wrap output in the fence a compliant model emits (see everyday._ask)."""
    return f"<<<OUT>>>{t}<<<END>>>"


def _rec(args):
    return type("R", (), {"args": args})()


def test_translate_delivers():
    print("test_translate_delivers")
    out = E._translate_execute(_rec({"text": "你好世界", "to": "English"}),
                               provider=_Prov(F("Hello world")))
    check(out["translation"] == "Hello world", "translation returned (fence stripped)")
    v = E._delivered("translation", "translated")(_rec({}), out)
    check(v.status == VERIFIED, f"a delivered translation must VERIFY, got {v.status}")


def test_translate_empty_is_failed_not_needsyou():
    print("test_translate_empty_is_failed_not_needsyou")
    out = E._translate_execute(_rec({"text": "x"}), provider=_Prov(""))
    v = E._delivered("translation", "translated")(_rec({}), out)
    check(v.status == FAILED, "empty output is a genuine miss (FAILED), never needs_you")


def test_summarize_delivers():
    print("test_summarize_delivers")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"<html><body><h1>Widgets</h1><p>All about widgets and gizmos.</p></body></html>"
            self.send_response(200); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = E._summarize_execute(_rec({"url": f"http://127.0.0.1:{port}/"}),
                               provider=_Prov(F("- It is about widgets\n- and gizmos")))
    check(out["summary"].startswith("- It is about widgets"), "summary returned")
    v = E._delivered("summary", "summarized")(_rec({}), out)
    check(v.status == VERIFIED, f"a delivered summary must VERIFY, got {v.status}")
    srv.shutdown()


def test_reminder_schedules_and_fires():
    print("test_reminder_schedules_and_fires")
    clear_registry(); caps.register_builtins()
    out = E._reminder_execute(_rec({"text": "call mom", "delay_minutes": 5}))
    check(out.get("reminder_job"), "reminder returns a job id")
    v = E._reminder_verify(_rec({}), out)
    check(v.status == VERIFIED, f"a scheduled reminder must VERIFY, got {v.status}")
    # and colliejobd firing it writes the note
    from harness.actions import ActionStore
    from harness.jobs import JobStore, DONE_VERIFIED
    from harness.scheduler import Scheduler
    a = ActionStore(os.path.join(_state, "actions.db"))
    j = JobStore(os.path.join(_state, "jobs.db"))
    s = Scheduler(a, j, db_path=os.path.join(_state, "jobs.db"))
    fired = s.tick(now=out["scheduled_for"] + 1)
    check(fired >= 1, "the reminder wait fires when due")
    check(j.get(out["reminder_job"]).state == DONE_VERIFIED, "reminder job completes")
    with open(os.path.join(_state, "notes", "reminders.txt"), encoding="utf-8") as f:
        check("call mom" in f.read(), "the reminder text is written on fire")
    s.close(); a.close(); j.close()


def test_translate_refusal_is_failed_not_fabricated():
    print("test_translate_refusal_is_failed_not_fabricated")
    # ANY unfenced decline (bare token, reason, prose policy refusal, non-English)
    # collapses to FAILED — no fence, no success.
    for refusal in ("CANNOT", "CANNOT - the input is empty",
                    "I'm sorry, I can't translate that due to policy", "无法翻译此内容"):
        out = E._translate_execute(_rec({"text": "???"}), provider=_Prov(refusal))
        v = E._delivered("translation", "translated")(_rec({}), out)
        check(v.status == FAILED, f"unfenced refusal {refusal!r} must be FAILED, got {v.status}")
    # a real translation that happens to say sorry IS fenced by a compliant model
    out2 = E._translate_execute(_rec({"text": "对不起"}), provider=_Prov(F("I'm sorry")))
    v2 = E._delivered("translation", "translated")(_rec({}), out2)
    check(v2.status == VERIFIED, "a real (fenced) translation 'I'm sorry' must VERIFY")
    # SHORT fenced refusals of any shape are caught (defense-in-depth)
    for refusal in ("I was unable to find a translation", "I can't translate that content.",
                    "I cannot help with that.", "无法翻译该内容", "抱歉，我无法完成"):
        o = E._translate_execute(_rec({"text": "x"}), provider=_Prov(F(refusal)))
        v = E._delivered("translation", "translated")(_rec({}), o)
        check(v.status == FAILED, f"a short fenced refusal must be FAILED: {refusal!r}")
    # but a LONG genuine translation that mentions 'no results' is NOT failed
    long_ok = "Section 3: when the query returns no results, check the index. " * 3
    out4 = E._translate_execute(_rec({"text": "x"}), provider=_Prov(F(long_ok)))
    v4 = E._delivered("translation", "translated")(_rec({}), out4)
    check(v4.status == VERIFIED, "a long real translation mentioning 'no results' must VERIFY")


def test_summarize_error_page_is_failed():
    print("test_summarize_error_page_is_failed")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            body = b"<h1>Access Denied</h1>"
            self.send_response(403); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = E._summarize_execute(_rec({"url": f"http://127.0.0.1:{port}/"}),
                               provider=_Prov("- fake summary of an error page"))
    v = E._delivered("summary", "summarized")(_rec({}), out)
    check(v.status == FAILED, "a 403 error page must NOT be 'summarized' (fabrication)")
    srv.shutdown()


def test_reminder_fires_even_on_a_very_late_wake():
    print("test_reminder_fires_even_on_a_very_late_wake")
    clear_registry(); caps.register_builtins()
    out = E._reminder_execute(_rec({"text": "pay rent", "delay_minutes": 60}))
    from harness.actions import ActionStore
    from harness.jobs import JobStore, DONE_VERIFIED
    from harness.scheduler import Scheduler
    a = ActionStore(os.path.join(_state, "actions.db"))
    j = JobStore(os.path.join(_state, "jobs.db"))
    s = Scheduler(a, j, db_path=os.path.join(_state, "jobs.db"))
    # simulate the laptop waking 40 DAYS after the fire time (>> the old 24h TTL)
    s.tick(now=out["scheduled_for"] + 40 * 86400)
    check(j.get(out["reminder_job"]).state == DONE_VERIFIED,
          "a reminder must still fire on a very late catch-up wake (TTL never expires it)")
    with open(os.path.join(_state, "notes", "reminders.txt"), encoding="utf-8") as f:
        check("pay rent" in f.read(), "the reminder note is actually written on late fire")
    s.close(); a.close(); j.close()


def test_huge_delay_does_not_crash():
    print("test_huge_delay_does_not_crash")
    clear_registry(); caps.register_builtins()
    out = E._reminder_execute(_rec({"text": "x", "delay_minutes": 10**18}))
    check(out.get("reminder_job"), "an absurd delay must be clamped, not crash")
    v = E._reminder_verify(_rec({}), out)
    check(v.status == VERIFIED, "clamped far-future reminder still parks")


def test_note_list_missing_file_is_failed():
    print("test_note_list_missing_file_is_failed")
    clear_registry(); caps.register_builtins()
    out = caps._note_list_execute(_rec({"file": "does-not-exist.txt"}))
    v = caps._note_list_verify(_rec({}), out)
    check(v.status == FAILED, "reading a nonexistent file must FAIL, not fake 'read'")


def test_note_list_unreadable_dir_degrades():
    print("test_note_list_unreadable_dir_degrades")
    import stat as _stat
    clear_registry(); caps.register_builtins()
    d = tempfile.mkdtemp(prefix="collie-nolist-")
    os.environ["COLLIE_NOTES_DIR"] = d
    try:
        os.chmod(d, 0)                              # unreadable dir
        out = caps._note_list_execute(_rec({}))     # must NOT raise
        v = caps._note_list_verify(_rec({}), out)
        check(v.status == FAILED, "an unreadable notes dir must FAIL honestly, not crash")
    finally:
        os.chmod(d, _stat.S_IRWXU)
        os.environ["COLLIE_NOTES_DIR"] = os.path.join(_state, "notes")


def test_none_note_is_failed():
    print("test_none_note_is_failed")
    clear_registry(); caps.register_builtins()
    out = caps._note_execute(_rec({"file": "n.txt", "text": None}))
    check("skipped" in out, "a None-text note writes nothing")
    v = caps._note_verify(_rec({"file": "n.txt", "text": None}), out)
    check(v.status == FAILED, "a None note must FAIL, never fabricate (str(None)='None')")


def test_registered():
    print("test_registered")
    clear_registry(); caps.register_builtins()
    for n in ("translate", "web.summarize", "reminder.set", "note.list"):
        check(get_capability(n) is not None, f"{n} must be registered")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    clear_registry()
    if _fails:
        print(f"\n== EVERYDAY: {len(_fails)} FAILED ==")
        sys.exit(1)
    print(f"\n== EVERYDAY: {len(tests)} test groups passed ==")


if __name__ == "__main__":
    main()
