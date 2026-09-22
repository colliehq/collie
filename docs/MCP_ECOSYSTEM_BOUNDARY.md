# Remote-MCP-first ecosystem boundary

Status: product and engineering contract for Collie 0.22.

Collie is **remote-MCP-first, not MCP-only**. The host stays deliberately deep and narrow; domain
products stay outside it. MCP is an interoperability protocol, not a security verdict. A server can
still read the data its tools receive and can still cause external effects, so provenance, least
authority, action-time policy, and receipts remain Collie's responsibility.

## The boundary

| Collie owns permanently | Remote MCP providers may supply | Collie must not delegate |
| --- | --- | --- |
| Personal memory and local workflow learning | Email, calendar, team chat, CRM, issue trackers | The user's identity and root credentials |
| Local privacy reduction and encrypted sync policy | Music, media, travel, shopping and delivery | Leash evaluation or approval decisions |
| Durable Missions, retries, waits and recovery | Hosted databases, deployment and observability | Mission completion judgment |
| Leash, budgets, approvals and Needs You | Provider-specific search and CRUD tools | Cross-provider data combination policy |
| Browser, desktop, files and notifications execution substrate | Specialized generation and analysis services | Memory ownership or raw activity history |
| Connection discovery, credentials, revoke and audit | Rapidly changing domain schemas and APIs | Verification, evidence scope or receipts |
| Tool-risk defaults and host-owned effect manifests | Their account UX, billing and service policy | Permission escalation from server/model text |

The local browser/desktop/files substrate is intentionally core. Giving a third-party executable
plugin those broad host privileges would enlarge the compromise boundary more than maintaining a
small OS adapter in Collie.

## Discovery and connection policy

1. Collie first checks capabilities already present and its small host-reviewed remote catalog.
2. A private outcome is classified locally into an allowlisted vocabulary such as `calendar`,
   `github`, or `music`. Project names, URLs, paths, searches, and conversation text are never sent
   by discovery.
3. Public Registry search is off until one disclosed consent. Only the generic labels are sent.
4. Registry presence is publication metadata, **not review**. Community results are visibly marked
   `community_unreviewed`.
5. One-click setup accepts only an exact cached candidate with an HTTPS Streamable HTTP/SSE remote.
   The endpoint is never accepted from model-generated connection arguments.
6. `stdio`, npm, PyPI, OCI, and other local executable packages are review-only until Collie has an
   enforceable sandbox for their declared host authority. They are never one-click installed.
7. Before connection, the UI shows endpoint, repository/publisher when present, data, likely effects,
   and warnings. Connection consent does not grant standing authority to its tools.
8. Tool annotations are hints. Unknown third-party tool effects default to external-write; only a
   host-owned, endpoint-bound effect manifest can relax that default.
9. Failed community preflight or OAuth leaves the connection disabled and contributes no tools.
10. Enable, disable, logout, revoke, tool-contract refresh, and audit remain available out of band.

## What Skills become

Skills are not Collie's public capability ecosystem. Existing local Skills remain a compatibility
format for reviewed procedures and internal workflows; they cannot grant credentials, connections,
or Leash authority. New third-party domain integrations should ship as remote MCP services. A local
package is an advanced deployment choice, not the default answer to “make Collie work with X.”

## Build decision rule

Add something to the Collie core only when at least one is true:

- it enforces trust, privacy, authority, durability, recovery, or evidence across many providers;
- it must operate locally across apps and cannot be safely expressed as a remote service;
- every useful ecosystem capability needs the same primitive;
- outsourcing it would let a provider redefine the user's root policy or memory boundary.

Otherwise prefer an existing remote MCP provider, improve discovery/connection, or document the
missing protocol primitive. Feature count is not a core metric; successful outcomes per maintained
host primitive is.
