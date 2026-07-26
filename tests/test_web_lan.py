"""`collie web --lan` — the opt-in that lets a phone (CollieIOS) reach the server at all.

Locks the security shape of the widening, because the whole point of `_host_ok` is anti-DNS-rebinding:
  • default (no --lan): loopback Host only, LAN_HOSTS empty — byte-for-byte the old behaviour
  • with --lan: exactly this machine's own addresses are added, never "any host"
  • an attacker Host is still refused with --lan on
  • the token still gates the code-executing routes regardless of Host

…and the pairing handshake that makes --lan safe at all. The first cut of --lan was NOT safe: `/`
embeds the token for same-origin JS, so any device on the Wi-Fi could fetch the page, read the token
and run bash. The fix is a peer gate (non-loopback clients must present a token) plus /api/pair,
which trades a one-shot secret shown on this machine's screen for that token. Both are locked here.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import webapp                                        # noqa: E402

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


class _FakeHandler:
    """Just enough of BaseHTTPRequestHandler for the guards: a Host header and a peer address."""

    _host_ok = webapp.Handler._host_ok
    _peer_ok = webapp.Handler._peer_ok
    _peer_is_loopback = webapp.Handler._peer_is_loopback
    _authed = webapp.Handler._authed

    def __init__(self, host, peer="127.0.0.1"):
        self.headers = {"Host": host}
        self.client_address = (peer, 51000)


def host_ok(host):
    return _FakeHandler(host)._host_ok()


def test_default_is_loopback_only():
    check(webapp.LAN_HOSTS == set(), "LAN_HOSTS is empty until --lan asks for it")
    for host in ("127.0.0.1:8787", "localhost:8787", "collie.localhost:8787", "[::1]:8787", ""):
        check(host_ok(host), "loopback Host accepted: %r" % host)
    for host in ("192.168.0.4:8787", "evil.example.com", "10.1.2.3:8787"):
        check(not host_ok(host), "non-loopback Host refused by default: %r" % host)


def test_lan_adds_only_own_addresses():
    ips = webapp._own_ipv4()
    check(all(not ip.startswith("127.") for ip in ips), "_own_ipv4 excludes loopback")
    webapp.LAN_HOSTS.update(ips or ["192.168.0.4"])          # CI may have no LAN address at all
    try:
        for ip in (ips or ["192.168.0.4"]):
            check(host_ok("%s:8787" % ip), "with --lan, this machine's own Host accepted: %s" % ip)
        check(not host_ok("evil.example.com"),
              "with --lan, a rebinding Host is STILL refused (not an any-host allow)")
        check(host_ok("127.0.0.1:8787"), "with --lan, loopback keeps working")
    finally:
        webapp.LAN_HOSTS.clear()
    check(webapp.LAN_HOSTS == set(), "LAN_HOSTS resets — no leak into other tests")


def test_token_still_guards_the_agent():
    """The Host allow-list is not authentication: `_authed` is what stops a drive-by run."""
    import urllib.parse

    class _Authed:
        _authed = webapp.Handler._authed

    ok = _Authed()._authed(urllib.parse.urlparse("/api/stream?q=hi&token=" + webapp.TOKEN))
    bad = _Authed()._authed(urllib.parse.urlparse("/api/stream?q=hi"))
    stale = _Authed()._authed(urllib.parse.urlparse("/api/stream?q=hi&token=" + "0" * 32))
    check(ok, "correct token authorizes the code-executing route")
    check(not bad, "missing token refused")
    check(not stale, "stale token refused")


def test_cli_flag_reaches_the_server():
    """`collie web --lan` must actually forward the flag — cmd_web rebuilds webapp's argv by hand,
    so a new flag is silently dropped unless it is added there too."""
    import argparse

    from harness import cli
    seen = {}
    real = webapp.main
    webapp.main = lambda argv: seen.setdefault("argv", argv) and 0
    try:
        cli.cmd_web(argparse.Namespace(port=8787, open=False, lan=True, qr=False))
        check("--lan" in seen.get("argv", []), "cmd_web forwards --lan to webapp.main")
        seen.clear()
        cli.cmd_web(argparse.Namespace(port=8787, open=False, lan=False, qr=False))
        check("--lan" not in seen.get("argv", []), "cmd_web omits --lan when not asked")
        seen.clear()
        cli.cmd_web(argparse.Namespace(port=8787, open=False, lan=True, qr=True))
        check("--qr" in seen.get("argv", []), "cmd_web forwards --qr to webapp.main")
    finally:
        webapp.main = real


def test_peer_gate_closes_the_token_leak():
    """A non-loopback client gets nothing without a token — the hole the first --lan cut had."""
    import urllib.parse
    lan = _FakeHandler("192.168.0.4:8787", peer="192.168.0.9")
    loop = _FakeHandler("127.0.0.1:8787", peer="127.0.0.1")

    def ok(handler, path):
        return handler._peer_ok(urllib.parse.urlparse(path))

    check(ok(loop, "/"), "loopback keeps unrestricted access (the local browser is untouched)")
    check(ok(loop, "/api/sessions"), "loopback needs no token for reads, as before")
    check(not ok(lan, "/"), "a LAN client canNOT fetch the token-bearing page")
    check(not ok(lan, "/api/sessions"), "a LAN client canNOT read runs without a token")
    check(not ok(lan, "/api/stream?q=whoami"), "a LAN client canNOT run the agent without a token")
    check(ok(lan, "/api/sessions?token=" + webapp.TOKEN), "a LAN client WITH the token is allowed")
    check(ok(lan, "/api/pair"), "/api/pair is the one route open before pairing")
    check(_FakeHandler("x", peer="::1")._peer_is_loopback(), "IPv6 loopback counts as loopback")
    check(_FakeHandler("x", peer="::ffff:127.0.0.1")._peer_is_loopback(),
          "IPv4-mapped loopback counts as loopback")
    check(not _FakeHandler("x", peer="10.0.0.5")._peer_is_loopback(), "a LAN peer is not loopback")


def test_pairing_secrets_are_one_shot_and_expire():
    a = webapp._pair_mint()
    b = webapp._pair_mint()
    check(a != b, "each mint is a fresh secret")
    check(len(a) == 16, "secrets are 8 bytes (16 hex chars)")
    ok, _ = webapp._pair_redeem(a)
    check(ok, "a fresh secret redeems")
    ok2, detail = webapp._pair_redeem(a)
    check(not ok2, "the same secret cannot be redeemed twice (%s)" % detail)
    ok3, _ = webapp._pair_redeem("ff" * 8)
    check(not ok3, "an unknown secret is refused")
    ok4, _ = webapp._pair_redeem("")
    check(not ok4, "an empty secret is refused")

    expired = webapp._pair_mint()
    with webapp._PAIR_LOCK:
        webapp._PAIR_LIVE[expired] = 0.0                 # pretend it was minted long ago
    ok5, detail5 = webapp._pair_redeem(expired)
    check(not ok5, "an expired secret is refused (%s)" % detail5)
    check(webapp._pair_redeem(b)[0], "an unrelated live secret still works")


def test_pairing_is_rate_limited():
    with webapp._PAIR_LOCK:
        webapp._PAIR_FAILS.clear()
    for _ in range(10):
        webapp._pair_redeem("ab" * 8)
    good = webapp._pair_mint()
    ok, detail = webapp._pair_redeem(good)
    check(not ok and "too many" in detail,
          "11th attempt in a minute is rate-limited even with a valid secret (%s)" % detail)
    with webapp._PAIR_LOCK:
        webapp._PAIR_FAILS.clear()
    check(webapp._pair_redeem(good)[0], "the same secret works once the limiter window clears")


def main():
    test_default_is_loopback_only()
    test_lan_adds_only_own_addresses()
    test_token_still_guards_the_agent()
    test_peer_gate_closes_the_token_leak()
    test_pairing_secrets_are_one_shot_and_expire()
    test_pairing_is_rate_limited()
    test_cli_flag_reaches_the_server()
    if _fails:
        print("\n%d FAILED" % len(_fails))
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
