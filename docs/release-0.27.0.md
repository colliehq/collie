# Collie 0.27.0 release review

Reviewed on September 21, 2026 (America/Los_Angeles). This release combines the
committed 0.26.0 development line with the public repository's nine additional
commits. The separate working tree preserves unfinished local product work.

## Runtime versions

| Component | Version reviewed | Evidence |
| --- | --- | --- |
| Codex CLI and official Python SDK | 0.155.1 | Official source at `rust-v0.155.1`, installed SDK, native protocol probe |
| Claude Agent SDK | 0.2.157 | Installed official package and dependency metadata |
| Claude Code in the Windows installer | 2.1.278 | Immutable official npm package, pinned SHA-512 and executable version check |

The core Python package remains dependency-free. SDK dependencies are optional
extras and use exact versions for this release. Updating a dependency does not
change saved model selections or enable paid API fallback.

The Claude SDK's current release has macOS and Linux wheels but no Windows
wheel. Its source distribution does not contain the native CLI. The Windows
installer therefore stages the official Windows executable and license into
the SDK's runtime directory, verifies the archive before copying files, and
executes `--version` before accepting the payload. An import-only check would
miss this installation failure.
The model picker also recognizes the SDK's bundled runtime without requiring a
second CLI on `PATH`; run admission still performs its authentication checks.

## Reliability changes

- Windows Python execution aliases are replaced, within Collie's child
  environment, with launchers for the selected real interpreter. Tests cover
  Git Bash and cmd, Unicode arguments, Job membership and cancellation before
  a delayed child write. Explicit absolute commands are not rewritten; this
  mechanism is not an operating-system sandbox.
- Cold-start locks acquire ownership before writing state. The previous empty
  file bootstrap could race another process and fail before lock contention
  was handled.
- Private directories retain owner traversal on POSIX. Windows temporary
  worktree cleanup handles read-only regular files without changing the
  permissions of links or paths outside that tree.
- Legacy standalone test checks now also fail their collected pytest item.
  Cleanup still runs before a recorded check becomes a failure. The test
  launcher resolves a real Python executable and preserves paths with spaces.
- Both wheel and source-distribution manifests exclude generated browser
  authentication assets.
- Codex 0.155.1 exports `CODEX_VERSION` to child commands. This parent metadata
  is discarded without blocking a worker launch. API keys, endpoint overrides
  and unreviewed environment overrides retain their existing restrictions.
- A failed Codex SDK turn preserves its known thread ID and error, allowing the
  host to distinguish a failed turn from a malformed response. On Windows the
  SDK now applies the same explicit workspace sandbox settings as the other
  Codex workers. Its approval fallback declines instead of accepting requests.
- The published `openai-codex==0.155.1` distribution pins
  `openai-codex-cli-bin==0.155.1`; the SDK continues to use that runtime. The
  release-tag source tree still names an older dependency, so the installed
  package metadata and actual binary version were checked as well. Requiring
  a separate CLI on `PATH` would break a working SDK-only installation.
- Web startup initializes Python's typing dependency before starting background
  services. This avoids a cold-start race in which a concurrent dataclass import
  sees a partially initialized `typing.ClassVar` and Live Copilot fails to start.
- POSIX verification cleanup reaps its own killed child while checking group
  extinction. Retaining that zombie previously made a completed stop look like
  an unconfirmed process cleanup and left an unnecessary recovery fence.
- Pack treats a child path beneath a regular file as absent on POSIX as well as
  Windows, allowing a reviewed file-to-directory change. Parent-path checks,
  conflict detection and link refusal still precede writes.

The upstream automatic model downgrade is not enabled: changing models within
one accepted run would invalidate its model attribution and aggregate pricing.
An overloaded model keeps its bounded retries; a spent plan with a trusted reset
time keeps the durable wait. Changing models remains an explicit selection.

## Product continuity

The previously unpublished development line includes durable follow-up drafts,
per-conversation task settings, explicit recovery for uncertain delivery,
provider-attested reset waits and verification based on host execution receipts.
These changes preserve accepted work and reduce repeated setup while retaining
visible recovery when an action's outcome is unknown. See the 0.25.0 and 0.26.0
changelog entries for the individual workflow changes.

## Release verification

The release pipeline runs the complete gate before constructing artifacts.
Windows installers must verify the expected publisher signature; macOS arm64
images must pass the configured signing and notarization checks. Building or
tagging alone does not establish that a release has shipped.

The native Codex 0.155.1 probe completed initialization, model listing and
ephemeral thread creation with an isolated configuration and no inference.
This proves those protocol operations only; live edit, resume, cancellation,
optional SDK and UI checks are recorded separately in the release validation.

The September 21 Windows live matrix passed all 44 checks across Claude Code,
Codex exec, Codex App Server and Codex SDK. Real edits and same-session follow-ups
were observed for every runner. A separate Claude Agent SDK completion returned
the expected response with `api_key_source=none` and one completed reservation.
The matrix's billing-shape check is not a subscription admission certificate.
The browser audit exercised onboarding dismissal, mock task completion, draft
recovery across navigation and refresh, saved Plan settings and a 390px viewport.

On Windows/Python 3.14, the first live Codex checks exposed an ACL difference:
the restricted-token sandbox could edit a normal project but could not read
`mkdtemp`'s owner-only directory. Conformance now creates its synthetic fixture
with inherited scratch-directory permissions. It never changes the permissions
of an existing workspace. Owner-only private workspaces remain a known vendor
sandbox limitation; passing the ordinary fixture does not certify that case.

## Primary references

- [OpenAI changelog](https://learn.chatgpt.com/docs/changelog)
- [Codex App Server documentation](https://learn.chatgpt.com/docs/app-server)
- [Codex 0.155.1 source](https://github.com/openai/codex/tree/rust-v0.155.1)
- [Claude Agent SDK releases](https://pypi.org/project/claude-agent-sdk/0.2.157/)
- [Claude Code releases](https://github.com/anthropics/claude-code/releases)
