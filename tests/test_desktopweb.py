"""Lock the two security shapes of the ambient-desktop web surface (harness.webapp):

  1. /api/desktop/audio is an SSRF surface — it proxies an arbitrary URL so playback is same-origin.
     `_audio_host_ok` must admit ONLY https URLs on the known CDN hosts, matched exactly or as a
     DOTTED subdomain — 'evilgooglevideo.com' and a plain-http URL must be refused.

  2. The CSRF token embedded in a served page (`_embed_token`) must reach a DIRECT loopback browser
     only. A relay-replayed request (a phone, tagged X-Collie-Relay) looks loopback but must get a
     tokenless page — the relay injects ?token= server-side instead. This is the regression that
     would re-leak the bash-running token onto the network.

Deterministic ($0, no sockets): calls the real module helpers / bound methods directly.

Run: python tests/test_desktopweb.py   (exit 0 = all green)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import webapp  # noqa: E402

_fails = []


def check(cond, msg):
    if not cond:
        _fails.append(msg)
        print("  FAIL:", msg)


# ── SSRF: audio-proxy host allow-list ─────────────────────────────────────────────────────────
def test_audio_host_ok_admits_the_cdns():
    print("test_audio_host_ok_admits_the_cdns")
    ok = [
        "https://r5---sn-abc.googlevideo.com/videoplayback?x=1",
        "https://googlevideo.com/x",
        "https://upos-sz-mirror08c.bilivideo.com/a.m4s",
        "https://cn-hostname.bilivideo.cn/a.m4s",
        "https://x.akamaized.net/seg",
        "https://s1.hdslb.com/a",
    ]
    for u in ok:
        check(webapp._audio_host_ok(u), "legit CDN URL is admitted: %s" % u)


def test_audio_host_ok_refuses_lookalikes_and_plain_http():
    print("test_audio_host_ok_refuses_lookalikes_and_plain_http")
    bad = [
        "https://evilgooglevideo.com/x",          # suffix-glued lookalike (the bare-endswith trap)
        "https://googlevideo.com.evil.com/x",      # allowed host as a left label of an attacker domain
        "https://notbilivideo.cn/x",
        "http://r5.googlevideo.com/x",             # right host but plain http (no TLS)
        "https://127.0.0.1/x",                     # SSRF to loopback
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "file:///etc/passwd",
        "",
        "https:///nohost",
    ]
    for u in bad:
        check(not webapp._audio_host_ok(u), "hostile / malformed URL is refused: %r" % u)


# ── CSRF token embedding gate ─────────────────────────────────────────────────────────────────
class _FakeHandler:
    """Just enough of the handler for the token-embedding decision: a peer address and headers."""
    _peer_is_loopback = webapp.Handler._peer_is_loopback
    _is_relay = webapp.Handler._is_relay
    _embed_token = webapp.Handler._embed_token

    def __init__(self, peer="127.0.0.1", relay=False):
        self.client_address = (peer, 51000)
        self.headers = {"X-Collie-Relay": "1"} if relay else {}


def test_embed_token_only_for_direct_loopback():
    print("test_embed_token_only_for_direct_loopback")
    check(_FakeHandler(peer="127.0.0.1")._embed_token() == webapp.TOKEN,
          "a direct loopback browser gets the real CSRF token embedded")
    check(_FakeHandler(peer="127.0.0.1", relay=True)._embed_token() == "",
          "a relay-replayed request (phone) looks loopback but gets a TOKENLESS page")
    check(_FakeHandler(peer="192.168.0.9")._embed_token() == "",
          "a plain non-loopback client gets no token (it paired for one already)")
    check(_FakeHandler(peer="::1")._embed_token() == webapp.TOKEN,
          "IPv6 loopback still counts as a direct local browser")
    check(_FakeHandler(peer="192.168.0.9", relay=True)._embed_token() == "",
          "non-loopback AND relay is doubly tokenless")


def test_relay_marker_detection():
    print("test_relay_marker_detection")
    check(_FakeHandler(relay=True)._is_relay() is True, "the X-Collie-Relay:1 marker is detected")
    check(_FakeHandler(relay=False)._is_relay() is False, "no marker -> not a relay request")


def main():
    test_audio_host_ok_admits_the_cdns()
    test_audio_host_ok_refuses_lookalikes_and_plain_http()
    test_embed_token_only_for_direct_loopback()
    test_relay_marker_detection()
    if _fails:
        print("\n%d FAILED" % len(_fails))
        sys.exit(1)
    print("\nall green")


if __name__ == "__main__":
    main()
