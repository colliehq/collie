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
import os
import sys

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

    print("\n  " + ("%d FAILED" % len(fails) if fails else "mcp catalog: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
