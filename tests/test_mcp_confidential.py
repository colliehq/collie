"""A registered client is not always a public one.

`byo_client` covers the services that will not register a client on the spot: you make an app on
their side and hand collie its Client ID. For Slack that is still not enough to finish. Its metadata
says `token_endpoint_auth_methods_supported: ["client_secret_post"]`, so an id alone opens the
authorize page, survives the login and the Allow button, and dies at the exchange — the one point
where the failure can no longer be read as "I have not signed in yet".

So the secret goes on the exchange *and* on every refresh after it. Refresh is where a half-done
version of this hides: it works for an hour and then logs you out, looking like a flaky server.
Slack also refuses inside a 200 (`{"ok": false}`) and nests the grant under `authed_user`, both of
which quietly produce a stored token with no token in it.

    python3 tests/test_mcp_confidential.py
"""
import json
import os
import sys
import tempfile
import threading
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


# --- a token endpoint that behaves like Slack's -----------------------------------------------
# 200 OK with {"ok": false} for a refusal, the grant nested under "authed_user", and a hard
# requirement that the client authenticate with its secret in the form body.
class _FakeAS:
    def __init__(self):
        import http.server
        self.seen = []                      # every form body posted to /token
        self.refuse = False
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = dict(urllib.parse.parse_qsl(self.rfile.read(n).decode()))
                outer.seen.append(form)
                if outer.refuse:
                    body = {"ok": False, "error": "invalid_client_id"}
                elif form.get("grant_type") == "refresh_token":
                    body = {"ok": True, "access_token": "xoxp-refreshed",
                            "refresh_token": "rt-2", "expires_in": 3600}
                else:
                    body = {"ok": True, "authed_user": {
                        "access_token": "xoxp-fresh", "refresh_token": "rt-1", "expires_in": 3600}}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):
                pass

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def meta(self):
        """Slack's shape: authorize + token, and nothing that would let us self-register."""
        return {"authorization_endpoint": self.url + "/authorize",
                "token_endpoint": self.url + "/token",
                "token_endpoint_auth_methods_supported": ["client_secret_post"]}

    def stop(self):
        self.srv.shutdown()


def _autoclick(m):
    """Stand in for the human at the browser: follow the authorize URL's redirect_uri immediately,
    carrying back the code and the state login() is waiting to match."""
    def _open(auth_url):
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(auth_url).query)
        cb = "%s?code=the-code&state=%s" % (q["redirect_uri"][0], q["state"][0])
        threading.Thread(target=lambda: urllib.request.urlopen(cb, timeout=10).read(),
                         daemon=True).start()
        return True
    import webbrowser
    webbrowser.open = _open
    return m


def main():
    from harness import mcpclient as m

    tmp = tempfile.mkdtemp(prefix="collie-mcp-conf-")
    m._CONFIG = os.path.join(tmp, "mcp.json")
    m._TOKENS = os.path.join(tmp, "mcp_tokens.json")

    as_ = _FakeAS()
    m._discover_oauth = lambda _url: as_.meta()
    _autoclick(m)

    # 1. No credentials, no way to register: still a refusal, and the byo-client instructions have
    #    to ask for the second half of the credential — a Client ID alone gets a person through the
    #    authorize page and fails at the exchange, which is the worst place to find out.
    m.save_config({"slack": {"url": "https://mcp.slack.com/mcp"}})
    try:
        m.login("slack", m._load_config()["slack"])
        check(False, "login without a client id fails")
    except RuntimeError:
        check(True, "login without a client id fails")
    help_text = m.byo_client_help("slack", "Slack", "https://mcp.slack.com/mcp")
    check("client_secret" in help_text, "and the instructions name client_secret, not just the id")
    check("Client Secret" in help_text, "in the words the provider's console uses for it")

    # 2. With an app's credentials, the exchange authenticates as a confidential client.
    m.save_config({"slack": {"url": "https://mcp.slack.com/mcp",
                             "client_id": "cid-1", "client_secret": "sec-1", "scope": "chat:write"}})
    tok = m.login("slack", m._load_config()["slack"])
    ex = as_.seen[-1]
    check(ex.get("client_secret") == "sec-1", "the code exchange carries client_secret")
    check(ex.get("code_verifier"), "and still proves PKCE (secret does not replace the verifier)")
    check(tok.get("access_token") == "xoxp-fresh",
          "the grant nested under authed_user is unwrapped, not stored empty")
    check(m._get_token("slack").get("access_token") == "xoxp-fresh", "and it lands in the store")

    # 3. Refresh is the half-done-version trap: same secret, an hour later.
    stored = m._get_token("slack")
    stored["obtained_at"] = 0                                  # expired
    m._put_token("slack", stored)
    got = m._access_token("slack")
    rf = as_.seen[-1]
    check(rf.get("grant_type") == "refresh_token", "an expired token triggers a refresh")
    check(rf.get("client_secret") == "sec-1", "and the refresh carries client_secret too")
    check(got == "xoxp-refreshed", "so the caller gets the new access token")

    # 4. Slack refuses with HTTP 200 and {"ok": false} — that must not be stored as a token.
    as_.refuse = True
    m._save_tokens({})
    try:
        m.login("slack", m._load_config()["slack"])
        check(False, "an {ok: false} refusal raises instead of storing a tokenless token")
    except RuntimeError as e:
        check("invalid_client_id" in str(e),
              "an {ok: false} refusal raises, quoting the server's reason")
    check(m._get_token("slack") is None, "and nothing is written to the token store")

    # 5. The secret is client configuration, not a session: logout must not take it with it.
    check(m._load_config()["slack"].get("client_secret") == "sec-1",
          "the credentials stay in mcp.json across a failed login")

    as_.stop()
    print("\n  " + ("%d FAILED" % len(fails) if fails else "mcp confidential client: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
