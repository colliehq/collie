# Collie Remote relay (Cloudflare Worker + Durable Object)

Public zero-knowledge meeting point so a phone can drive a desktop `collie web` behind NAT: the
desktop dials out over WSS (`/relay/agent`), while the native phone client sends mandatory encrypted
API envelopes to the single `/r/<room>/sealed` endpoint. The Durable Object multiplexes opaque
records and streams them back without seeing paths, prompts, code, output, or true response status.

Pairing is relay-blind: a 256-bit QR-fragment secret authenticates an X25519 transcript on the
desktop, followed by explicit human approval and a one-shot durable ticket. Per-device bearer tokens
gate the relay; AES-GCM provides content confidentiality/integrity above TLS. Token issue is a
two-phase operation: the relay withholds the bearer until the desktop confirms the exact approved
pair's token hash and device key are durably stored. See `E2E_DESIGN.md`.

Production desktop URLs must use `wss://` (`ws://` is rejected except on exact loopback hosts). The
Worker validates unguessable room/key shapes, permits one authenticated desktop socket per room,
bounds device/hash lists and in-flight sealed streams, and fails requests whose desktop never returns
a response head. JSON and offline responses are non-cacheable and carry defensive browser headers.

## Deploy

`wrangler.toml` owns both production routes (`collie.run/relay/*` and `collie.run/r/*`). Wrangler
does not support declaring encrypted secrets as required config bindings, so the Worker itself
returns `503` on health and Remote routes until all four APNs values are present and structurally
valid. Set each encrypted secret once; never put its value in the file:

```bash
cd relay
npx wrangler secret put APNS_KEY
npx wrangler secret put APNS_KEY_ID
npx wrangler secret put APNS_TEAM_ID
npx wrangler secret put APNS_TOPIC
npx wrangler deploy
curl --fail https://collie.run/relay/health
```

`wrangler secret list --format json` can confirm the binding names before deployment; the health
request above is the authoritative post-deploy check because only the Worker can validate the bound
values. It returns a generic error and never discloses which credential is absent or malformed.

The default desktop URL is the production route:

```bash
COLLIE_RELAY=wss://collie.run collie web --remote
```

For an isolated worker hostname, set `COLLIE_RELAY=wss://<worker-host>` instead. After deployment,
verify `/relay/agent` and `/r/*` reach this Worker before treating Remote as live; source changes do
not update the existing `collie.run` deployment by themselves.
