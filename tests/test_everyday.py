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


def _rec(args):
    return type("R", (), {"args": args})()


def test_translate_delivers():
    print("test_translate_delivers")
    out = E._translate_execute(_rec({"text": "你好世界", "to": "English"}),
                               provider=_Prov("Hello world"))
    check(out["translation"] == "Hello world", "translation returned")
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
                               provider=_Prov("- It is about widgets\n- and gizmos"))
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
    # a model DECLINE (sentinel) must collapse to FAILED, never a fake "translated"
    out = E._translate_execute(_rec({"text": "???"}), provider=_Prov("CANNOT"))
    v = E._delivered("translation", "translated")(_rec({}), out)
    check(v.status == FAILED, "a CANNOT decline must be FAILED, not fabricated success")
    # but a legit translation that happens to say sorry is NOT falsely failed
    out2 = E._translate_execute(_rec({"text": "对不起"}), provider=_Prov("I'm sorry"))
    v2 = E._delivered("translation", "translated")(_rec({}), out2)
    check(v2.status == VERIFIED, "a real translation ('I'm sorry') must still VERIFY")


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


def test_far_future_reminder_still_fires():
    print("test_far_future_reminder_still_fires")
    clear_registry(); caps.register_builtins()
    # a reminder 2 days out must not expire before it fires (the TTL bug)
    out = E._reminder_execute(_rec({"text": "pay rent", "delay_minutes": 2880}))
    from harness.actions import ActionStore
    from harness.jobs import JobStore, DONE_VERIFIED
    from harness.scheduler import Scheduler
    a = ActionStore(os.path.join(_state, "actions.db"))
    j = JobStore(os.path.join(_state, "jobs.db"))
    s = Scheduler(a, j, db_path=os.path.join(_state, "jobs.db"))
    s.tick(now=out["scheduled_for"] + 1)
    check(j.get(out["reminder_job"]).state == DONE_VERIFIED,
          "a 2-day-out reminder must still fire (TTL outlives fire time)")
    s.close(); a.close(); j.close()


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
