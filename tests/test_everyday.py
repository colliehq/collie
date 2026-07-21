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
    """wrap output in the OK fence a compliant model emits (see everyday._ask)."""
    return f"<<<OK>>>{t}<<<END>>>"


FAIL = "<<<FAIL>>>"   # what a compliant model emits when it declines


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
    page = "<html><body><h1>Widgets</h1><p>All about widgets and gizmos.</p></body></html>"
    out = E._summarize_execute(_rec({"url": "http://example.com/"}),
                               provider=_Prov(F("- It is about widgets\n- and gizmos")),
                               fetch=lambda u: (200, page))
    check(out["summary"].startswith("- It is about widgets"), "summary returned")
    v = E._delivered("summary", "summarized")(_rec({}), out)
    check(v.status == VERIFIED, f"a delivered summary must VERIFY, got {v.status}")


def test_summarize_ssrf_blocked_even_with_allow_local():
    print("test_summarize_ssrf_blocked_even_with_allow_local")
    # even with the operator opt-out set, an autonomous summarize must NOT reach a
    # loopback/metadata target: the scrub keeps the SSRF guard on -> "" -> FAILED.
    os.environ["COLLIE_WEBFETCH_ALLOW_LOCAL"] = "1"
    try:
        out = E._summarize_execute(_rec({"url": "http://127.0.0.1:9/secret"}),
                                   provider=_Prov(F("- leaked internal data")))
        check(out["summary"] == "", "a loopback URL must be blocked (empty summary)")
        v = E._delivered("summary", "summarized")(_rec({}), out)
        check(v.status == FAILED, "an SSRF-blocked summarize must be FAILED, not fabricated")
    finally:
        os.environ["COLLIE_WEBFETCH_ALLOW_LOCAL"] = "1"   # test module default


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


def test_declared_refusal_fails_but_content_verbatim():
    print("test_declared_refusal_fails_but_content_verbatim")
    # STRUCTURAL contract: a model that declines emits <<<FAIL>>> (or nothing) ->
    # "" -> FAILED. No content guessing, so refusal-idiom CONTENT is not false-failed.
    for decline in (FAIL, "", "I'm sorry, I can't translate that due to policy"):
        out = E._translate_execute(_rec({"text": "???"}), provider=_Prov(decline))
        v = E._delivered("translation", "translated")(_rec({}), out)
        check(v.status == FAILED, f"a declined translate {decline[:20]!r} must be FAILED")
    # a CORRECT translation whose content IS a refusal idiom VERIFIES — the model
    # DECLARED success (<<<OK>>>…), we don't second-guess the words.
    for src, tgt in (("对不起", "I'm sorry"), ("我无法帮你", "I cannot help you"),
                     ("查询无结果", "the query returned no results")):
        out = E._translate_execute(_rec({"text": src}), provider=_Prov(F(tgt)))
        check(E._delivered("translation", "translated")(_rec({}), out).status == VERIFIED,
              f"a declared translation {tgt!r} must VERIFY, not false-fail")


def test_summarize_declared_refusal_is_failed():
    print("test_summarize_declared_refusal_is_failed")
    got = (200, "<html><body>" + "real page content here. " * 5 + "</body></html>")
    for decline in (FAIL, "", "Sorry, I cannot help with that request at all."):
        out = E._summarize_execute(_rec({"url": "http://example.com"}),
                                   provider=_Prov(decline), fetch=lambda u, g=got: g)
        v = E._delivered("summary", "summarized")(_rec({}), out)
        check(v.status == FAILED, f"a declined summarize {decline[:20]!r} must be FAILED")


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


def test_reminder_not_in_human_confirm_inbox():
    print("test_reminder_not_in_human_confirm_inbox")
    clear_registry(); caps.register_builtins()
    from harness.actions import ActionStore
    out = E._reminder_execute(_rec({"text": "take pills", "delay_minutes": 240}))
    a = ActionStore(os.path.join(_state, "actions.db"))
    pend_nonces = [p["nonce"] for p in a.pending()]
    # the parked reminder action must NOT appear in the inbox — otherwise a human
    # could click confirm and fire it early (its real fire time then no-ops).
    check(all("reminder" not in (p.get("capability") or "") for p in a.pending()),
          "no reminder machinery in the inbox")
    # more precisely: the scheduled note.append nonce is auto -> hidden
    from harness.jobs import JobStore
    from harness.scheduler import Scheduler
    j = JobStore(os.path.join(_state, "jobs.db"))
    s = Scheduler(a, j, db_path=os.path.join(_state, "jobs.db"))
    parked = [w["nonce"] for w in s.pending_waits() if w["job_id"] == out["reminder_job"]]
    check(parked and all(n not in pend_nonces for n in parked),
          "the parked reminder action is hidden from the human confirm inbox")
    s.close(); a.close(); j.close()


def test_fire_at_parses_ampm_and_relative():
    print("test_fire_at_parses_ampm_and_relative")
    import datetime
    now = int(datetime.datetime(2026, 7, 20, 10, 0, 0).timestamp())

    def fa(at=None, delay=None):
        return E._fire_at(_rec({"at": at, "delay_minutes": delay}), now)

    def hm(ts):
        return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")

    check(hm(fa(at="remind me at 6:15pm")) == "18:15", "6:15pm -> 18:15")
    check(hm(fa(at="at 10:00PM")) == "22:00", "10:00PM -> 22:00")
    check(hm(fa(at="9:30am")) == "09:30", "9:30am -> 09:30")
    check(hm(fa(at="wake me at 7am")) == "07:00", "bare 7am -> 07:00")
    check(hm(fa(at="at 3pm")) == "15:00", "bare 3pm -> 15:00")
    check(hm(fa(at="14:30")) == "14:30", "24h 14:30 unchanged")
    check(fa(at="in 5 minutes") == now + 5 * 60, "in 5 minutes -> +5min")
    check(fa(at="2小时后提醒我") == now + 120 * 60, "2小时后 -> +120min")
    check(fa(at="in 1 hour and 30 minutes") == now + 90 * 60, "1h30m -> +90min")


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


def test_webfetch_dns_pin_fails_closed():
    print("test_webfetch_dns_pin_fails_closed")
    import socket
    from harness import webfetch as wf
    wf._pin.host, wf._pin.infos = "example.com", [(2, 1, 6, "", ("93.184.216.34", 80))]
    try:
        # a mixed-case SAME host must match the pin (no second, un-vetted lookup)
        check(wf._pinned_getaddrinfo("ExAmPle.COM", None) == wf._pin.infos,
              "mixed-case same host must match the pin")
        # a DIFFERENT host during a pinned fetch must FAIL CLOSED (DNS-rebinding)
        try:
            wf._pinned_getaddrinfo("evil.attacker.com", None)
            check(False, "a rebind host must be refused, not re-resolved")
        except socket.gaierror:
            pass
    finally:
        wf._pin.host, wf._pin.infos = None, None


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
