# Collie Remote relay (Cloudflare Worker + Durable Object)

Public zero-knowledge meeting point so a phone can drive a desktop `collie web` behind NAT: the
desktop dials out over WSS (`/relay/agent`), while the native phone client sends mandatory encrypted
API envelopes to the single `/r/<room>/sealed` endpoint. The Durable Object multiplexes opaque
records and streams them back without seeing paths, prompts, code, output, or true response status.

Pairing is relay-blind: a 256-bit QR-fragment secret authenticates an X25519 transcript on the
desktop, followed by explicit human approval and a one-shot durable ticket. Per-device bearer tokens
gate the relay; AES-GCM provides content confidentiality/integrity above TLS. See `E2E_DESIGN.md`.

Deploy:  cd relay && npx wrangler deploy
Desktop: COLLIE_RELAY=wss://<your-worker-host> collie web --remote   (or toggle in the /remote panel)
