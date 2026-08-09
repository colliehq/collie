# Collie Remote relay (Cloudflare Worker + Durable Object)

Public meeting point so a phone can drive a desktop `collie web` behind NAT: the desktop dials out
over WSS (`/relay/agent`), the phone hits `/r/<room>/*` over HTTPS, the `RelayRoom` Durable Object
multiplexes requests onto the agent WS and streams responses (incl. SSE) back.

Auth is self-hosted: a per-launch pairing code + durable per-device session tokens (the desktop is
the source of truth). No Cloudflare Access. Both legs are TLS.

Deploy:  cd relay && npx wrangler deploy
Desktop: COLLIE_RELAY=wss://<your-worker-host> collie web --remote   (or toggle in the /remote panel)

## Dog presence

The same Worker also hosts Collie's authenticated online roster. A shared deployment creates one
`PresencePack` Durable Object per Slack workspace/pack; each dog renews a 75-second lease, so a
crashed or powered-off machine becomes offline without needing to announce its own failure.

Presence is separate from phone-remote `RelayRoom` traffic and from Slack's native green dot. The
Collie roster is implemented here; native Slack presence is not currently wired to it. See
[Collie Presence](../docs/presence.md) for identity, enrollment, runtime configuration, privacy, and
deployment details.
