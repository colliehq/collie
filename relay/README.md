# Collie Remote relay (Cloudflare Worker + Durable Object)

Public meeting point so a phone can drive a desktop `collie web` behind NAT: the desktop dials out
over WSS (`/relay/agent`), the phone hits `/r/<room>/*` over HTTPS, the `RelayRoom` Durable Object
multiplexes requests onto the agent WS and streams responses (incl. SSE) back.

Auth is self-hosted: a per-launch pairing code + durable per-device session tokens (the desktop is
the source of truth). No Cloudflare Access. Both legs are TLS.

Deploy:  cd relay && npx wrangler deploy
Desktop: COLLIE_RELAY=wss://<your-worker-host> collie web --remote   (or toggle in the /remote panel)
