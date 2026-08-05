"""Connecting a service should not start with "go and find its URL".

Asked to connect Slack, Collie reached for `@modelcontextprotocol/server-slack` — a stdio server
wanting a bot token and a team id you mint by hand in Slack's admin UI — and the Settings panel
opened with a form asking for a URL or a command line. Meanwhile Slack runs a remote endpoint that
does OAuth in a browser, and Collie has had the full handshake (2.1, PKCE, dynamic registration)
the whole time. The missing piece was never the capability. It was the address.

Every entry in CATALOG was probed and answered `401` with a `WWW-Authenticate: Bearer` challenge,
which is what an endpoint that will do the browser handshake looks like. This test does not re-probe
the network — a suite that fails when Stripe has a bad afternoon is a suite people learn to ignore —
it checks the shape those probes established, and that lookup is forgiving enough to survive how
people and models actually type.

    python3 tests/test_mcp_catalog.py
"""
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def main():
    from harness import mcpclient as m

    check(len(m.CATALOG) >= 8, "there is a catalog at all (%d services)" % len(m.CATALOG))
    bad = [k for k, v in m.CATALOG.items() if not str(v.get("url", "")).startswith("https://")]
    check(not bad, "every entry is an https endpoint%s" % ("" if not bad else ": " + str(bad)))
    unlabelled = [k for k, v in m.CATALOG.items() if not v.get("label")]
    check(not unlabelled, "and has a name a person would recognise%s"
          % ("" if not unlabelled else ": " + str(unlabelled)))

    check("slack" in m.CATALOG, "Slack is in it — the one that started this")
    check(m.CATALOG["slack"]["url"] == "https://mcp.slack.com/mcp",
          "pointing at the remote endpoint, not an npm package that wants a bot token")

    # However it was typed, by a person or by a model.
    for typed in ("slack", "Slack", "SLACK", " slack ", "slack-mcp"):
        check(m.known(typed) is not None, "'%s' resolves" % typed)
    hit = m.known("Slack")
    check(hit and hit["name"] == "slack", "and normalises to the config key")

    # People ask for the product; the catalog is filed under the vendor.
    for product in ("jira", "confluence"):
        got = m.known(product)
        check(got is not None and got["name"] == "atlassian",
              "'%s' finds Atlassian's server" % product)

    check(m.known("definitely-not-a-service") is None,
          "something unknown stays unknown, rather than resolving to whatever sorts first")
    check(m.known("") is None and m.known(None) is None, "empty input does not match anything")

    # add_server must accept every entry as-is. The catalog handing over something the writer
    # rejects is exactly the disagreement this was built to remove, and it would only show up on
    # the click.
    import tempfile
    cfg = os.path.join(tempfile.mkdtemp(prefix="collie_mcpcat_"), "mcp.json")
    old_cfg = m._CONFIG
    m._CONFIG = cfg
    try:
        rejected = []
        for k, v in m.CATALOG.items():
            err = m.add_server(k, {"url": v["url"]}, replace=True)
            if err:
                rejected.append("%s: %s" % (k, err))
        check(not rejected, "every catalog entry is accepted by add_server%s"
              % ("" if not rejected else ": " + "; ".join(rejected[:3])))
        import json
        written = json.load(open(cfg))["servers"]
        check(set(written) == set(m.CATALOG), "and all of them land in the config file")
        check(written["slack"]["url"] == m.CATALOG["slack"]["url"],
              "with the url the catalog promised")
    finally:
        m._CONFIG = old_cfg

    # ---- the promise the tools make to the model ------------------------------------------------
    # mcpctl_add's description tells the model to pass ONLY the name for a known service. That was
    # not true of the code: it answered "a server needs either `url` or `command`", which reads as
    # "the name was wrong" and sends the model back to hunting for an npm package — the exact
    # behaviour the catalog exists to stop. A description and an implementation that disagree is
    # worse than neither, because only one of them is visible from the chat.
    os.environ["COLLIE_MCP_MANAGE"] = "1"
    tmpdir = tempfile.mkdtemp(prefix="collie_mcptool_")
    old_cfg, old_tok = m._CONFIG, m._TOKENS
    m._CONFIG = os.path.join(tmpdir, "mcp.json")
    m._TOKENS = os.path.join(tmpdir, "tokens.json")       # never touch the real credential store
    try:
        out = m.MCPAddTool().run({"name": "Linear"}, None)
        check("ERROR" not in out, "mcpctl_add takes a bare name: %s" % out.split(".")[0][:58])
        written = json.load(open(m._CONFIG))["servers"]
        check(written.get("linear", {}).get("url") == m.CATALOG["linear"]["url"],
              "and the catalog address is what landed in the config")
        check("mcpctl_connect" in out, "and it says what finishes the job (the browser sign-in)")

        out = m.MCPAddTool().run({"name": "not-a-real-service"}, None)
        check("ERROR" in out and "slack" in out,
              "an unknown bare name errors AND names what is known, instead of a bare refusal")

        # mcpctl_connect: resolve, add, then the browser handshake — without leaving the chat.
        seen = {}

        def fake_login(nm, cfg=None, timeout=300, announce=None):
            seen.update(name=nm, url=(cfg or {}).get("url"), timeout=timeout)
            if announce:
                announce("https://auth.example/authorize?x=1")
            return {"access_token": "t"}

        real_login, real_reg, real_cache = m.login, m._register_live, m._read_cache
        m.login, m._register_live, m._read_cache = fake_login, (lambda *a, **k: "(1 tool)"), (lambda: {})
        try:
            out = m.MCPConnectTool().run({"name": "jira"}, None)
            check(seen.get("name") == "atlassian" and "Connected" in out,
                  "mcpctl_connect('jira') adds Atlassian and signs in, in one call")
            check(seen.get("url") == m.CATALOG["atlassian"]["url"], "at the catalog address")
            check(0 < (seen.get("timeout") or 0) < 300,
                  "with a bounded wait — a tool call that hangs for five minutes reads as a hung agent")
            seen.clear()
            out = m.MCPConnectTool().run({"name": "not-a-real-service"}, None)
            check("ERROR" in out and not seen, "an unknown service is refused without a browser opening")
            check("not-a-real-service" not in json.load(open(m._CONFIG))["servers"],
                  "and nothing is written to the config on the way out")
        finally:
            m.login, m._register_live, m._read_cache = real_login, real_reg, real_cache

        # ---- the entries a press CANNOT finish -------------------------------------------------
        # Answering 401 with a Bearer challenge does not mean a client can register itself. Slack,
        # HubSpot and GitHub advertise no registration_endpoint, so connect died at "no client_id"
        # AFTER adding the server — which is how a button comes to look like it did nothing.
        for svc in ("slack", "hubspot", "github"):
            check(m.CATALOG[svc].get("byo_client") is True,
                  "%s is marked as needing an OAuth app of your own" % svc)
        for svc in ("linear", "neon", "vercel"):
            check(not m.CATALOG[svc].get("byo_client"),
                  "%s is not marked (its server registers clients dynamically)" % svc)

        out = m.MCPAddTool().run({"name": "github"}, None)
        check("client_id" in out, "mcpctl_add on one of them says so instead of pointing at connect")

        opened = []
        real_login = m.login
        m.login = lambda *a, **k: opened.append(a[0]) or {"access_token": "t"}
        try:
            # `hubspot` has not been added by anything above — so this also pins that a refused
            # connect writes nothing, which is the difference between "not set up" and "set up and
            # permanently one press away from working".
            out = m.MCPConnectTool().run({"name": "hubspot"}, None)
            check("ERROR" in out and "client_id" in out,
                  "mcpctl_connect('hubspot') refuses with the reason, not a dead browser tab")
            check(not opened, "and no browser handshake was started")
            check("hubspot" not in json.load(open(m._CONFIG))["servers"],
                  "and no half-usable server is left in the config")
            m.remove_server("github")

            # With a client_id of your own it is an ordinary remote server again.
            m.add_server("slack", {"url": m.CATALOG["slack"]["url"], "client_id": "abc"}, replace=True)
            m._register_live = lambda *a, **k: "(1 tool)"
            out = m.MCPConnectTool().run({"name": "slack"}, None)
            check(opened == ["slack"] and "Connected" in out,
                  "but with a client_id configured it signs in normally")
            m.remove_server("slack")
        finally:
            m.login = real_login

        # ---- a pinned redirect, which is what makes a BYO client_id usable at all ---------------
        # The ephemeral port is right when the server registers the client on the spot. When you
        # have to type the redirect URL into an app-management page first, a port that changes every
        # sign-in can never match — so `redirect_port` fixes it and `redirect_host` covers providers
        # that accept `localhost` but not `127.0.0.1`.
        import urllib.parse as _up
        import webbrowser as _wb
        m.add_server("pinned", {"url": "https://mcp.example.invalid/mcp", "client_id": "cid",
                                "redirect_port": 8971, "redirect_host": "localhost"}, replace=True)
        real_disc, real_open, seen = m._discover_oauth, _wb.open, {}
        m._discover_oauth = lambda u: {"authorization_endpoint": "https://auth.example.invalid/authorize",
                                       "token_endpoint": "https://auth.example.invalid/token"}
        _wb.open = lambda *a, **k: True                    # never actually open a browser in a test
        try:
            try:
                m.login("pinned", timeout=1, announce=lambda u: seen.setdefault("url", u))
            except Exception:
                pass                                       # nothing comes back; the URL is the point
        finally:
            m._discover_oauth, _wb.open = real_disc, real_open
        q = _up.parse_qs(_up.urlsplit(seen.get("url", "")).query)
        check(q.get("redirect_uri") == ["http://localhost:8971/callback"],
              "the authorize URL carries the redirect you registered (%s)" % q.get("redirect_uri"))
        check(q.get("client_id") == ["cid"], "and your own client_id, not one it tried to mint")
        check("code_challenge" in q, "still PKCE")
        m.remove_server("pinned")

        # The CLI path people type, and the empty-list screen that has to name it.
        from harness import cli
        import argparse
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = cli.cmd_mcp(argparse.Namespace(action="add", name="notion", value="", force=False))
        written = json.load(open(m._CONFIG))["servers"]
        check(rc == 0 and written.get("notion", {}).get("url") == m.CATALOG["notion"]["url"],
              "`collie mcp add notion` — no URL — resolves through the catalog")
        check("collie mcp connect notion" in buf.getvalue(),
              "and points at the one command that finishes it")
    finally:
        m._CONFIG, m._TOKENS = old_cfg, old_tok
        os.environ.pop("COLLIE_MCP_MANAGE", None)

    print("\n  " + ("%d FAILED" % len(fails) if fails else "mcp catalog: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
