# Collie Remote — end-to-end encryption design (hosted, zero-knowledge relay)

**Status:** design, not yet built. Target: the native-app + hosted-relay product path.

## 1. Why / threat model

Self-hosters run their own Worker → their traffic only touches their own Cloudflare account, so no
E2E is needed. But making every customer register Cloudflare + `wrangler deploy` is too much friction
for a product. So we host **one** relay for everyone — and the moment the relay carries *other people's*
dev sessions, "the relay operator can read plaintext" becomes unacceptable.

**Goal:** the hosted relay is a **zero-knowledge pipe** — it routes and forwards, but cannot read the
content (code, commands, output). A bug or breach in the Worker leaks only ciphertext.

**In scope (attacker = the relay / its operator / a passive network observer):** content confidentiality
+ integrity, and MITM resistance during pairing.
**Out of scope:** hiding *metadata* (room id, session id, request id, timing, byte counts) from the
relay — it needs those to route. Also out of scope: a compromised endpoint (phone or desktop) — E2E
can't help if a device is owned.

## 2. Deployment modes (keep both)

- **Self-host (plaintext):** current v0. Own Worker, `COLLIE_RELAY` → your worker. Relay may see
  plaintext because it's *yours*. HTML-injection/shim path stays (web UI served through the relay).
- **Hosted (E2E):** our worker, zero customer setup. **Relay never sees plaintext.** Client is the
  **native app** (or a PWA served from a static origin — NOT injected by the relay), talking the API
  over an encrypted envelope. The relay does **no** HTML injection in this mode (it can't read HTML).

## 3. Keys & handshake (once per device, at pairing)

Primitives: **X25519** (ECDH), **HKDF-SHA256**, **AES-256-GCM**, **HMAC-SHA256**. All native:
iOS `CryptoKit`, desktop `cryptography` (the `[remote]` extra), web `crypto.subtle`.

The pairing code (`~40-bit`, shown on the trusted desktop, scanned/typed into the trusted phone
out-of-band) **authenticates** the public-key exchange so the relay can't MITM it:

```
Desktop D and Phone P each generate an X25519 keypair (privD/pubD, privP/pubP).
Exchange pubkeys THROUGH the relay (relay could tamper — the code check below catches that):
  transcript = "collie-e2e-v1" ‖ room ‖ pubD ‖ pubP
  confirmD   = HMAC(paircode, transcript ‖ "D")
  confirmP   = HMAC(paircode, transcript ‖ "P")
Each side verifies the other's confirm tag. The relay doesn't know paircode → can't forge a tag →
if it swapped a pubkey, the tags mismatch → ABORT.
  S      = X25519(own priv, peer pub)          # shared secret
  K_dev  = HKDF(S, salt=room, info="collie-remote-device")   # long-term per-device key
```

Store `K_dev`: phone → Keychain; desktop → `~/.collie/remote.json` device entry (ideally OS-encrypted
at rest). `K_dev` never leaves the device; the relay never sees `S` or `K_dev`.

**Security note:** this is *authenticated ECDH via a short shared code*, not a formal PAKE. It's sound
here because the attack is **online, rate-limited (5/10 min), and the code is single-use + short-TTL**
— an attacker gets a handful of guesses at a ~40-bit code, not an offline brute force. If we ever want
offline-transcript resistance too, swap the HMAC step for **SPAKE2**; the rest is unchanged.

## 4. Encryption boundary (what's ciphertext vs plaintext)

Per session, derive `K_sess = HKDF(K_dev, info=session_id)`.

- **Encrypted (AES-256-GCM, fresh 96-bit random nonce per frame):**
  - request envelope = `{method, path, query, headers, body_b64}`
  - every response/SSE payload (the raw `event:…\ndata:…` bytes, or each chunk)
- **Plaintext (the relay needs it to route/forward):** `room`, request `id`, `session`, frame type
  (`req`/`chunk`/`end`/…), nonce.
- **AAD** on every seal = `room ‖ id ‖ session ‖ direction ‖ seq` — binds ciphertext to its context so
  the relay can't replay/reorder frames across requests.

The desktop's relay-client decrypts the request envelope → replays to `127.0.0.1` **with the local
CSRF token injected** (unchanged); encrypts the response frames on the way back. The phone never holds
the desktop's local token; the relay never holds `K_*`.

## 5. Wire frames (extend the existing relay protocol)

```
Phone → relay → desktop   {"t":"req","id":N,"session":S,"enc":{"n":<b64 nonce>,"ct":<b64>}}
desktop → relay → phone    {"t":"res","id":N,"enc":{...}}         # encrypted {status,headers}
desktop → relay → phone    {"t":"chunk","id":N,"enc":{...}}       # encrypted SSE bytes, frame-by-frame
desktop → relay → phone    {"t":"end","id":N}                     # no payload
```
Identical routing/multiplexing to today; only the payloads become `enc`. `read1()` streaming still
applies (encrypt each flushed chunk). The relay's `checkSession` (Bearer/cookie) still gates room
access — E2E is the *content* layer on top of the *auth* layer, not a replacement.

## 6. Per-client implementation

| | STT/handshake/crypto |
|---|---|
| **iOS (native, primary)** | `CryptoKit`: `Curve25519.KeyAgreement`, `HKDF<SHA256>`, `AES.GCM`, `HMAC<SHA256>`. Keychain for `K_dev`. |
| **Desktop** | `cryptography` (`[remote]` extra): `x25519`, `hkdf`, `AESGCM`, `hmac`. Degrade: if the extra is missing AND the relay is hosted, refuse to connect (don't fall back to plaintext on a shared relay). |
| **Web / PWA** | `crypto.subtle`: `deriveBits` (X25519 — or P-256 if a target lacks X25519), `HKDF`, `AES-GCM`, `HMAC`. Served from a static origin, not relay-injected. |

## 7. Lifecycle
- Pairing establishes `K_dev`; kick/forget a device → desktop drops `K_dev` + the session hash → its
  frames fail to decrypt / auth. Rotating the pairing code does not affect already-paired `K_dev`.
- Desktop restart: `K_dev` persists (in the device store) → returning device keeps working, no re-pair.
- Nonces are per-frame random; never reused under a key (GCM requirement).

## 8. Hosted-relay ops (separate from E2E, but required for the product)
- **Abuse gating:** tie room creation to a Collie auth/license token you control, so only legit installs
  use your relay (else it's a free tunnel for anyone). Rate-limit per token.
- **Cost:** Workers + Durable Objects, usage-based; dev-session traffic is small text — cheap per user,
  but it's your bill. WebSocket hibernation keeps idle rooms cheap.
- **Isolation (already true):** one DO per room, unguessable room id, AGENTKEY first-claim, pairing gate.

## 9. What changes vs today
- New: handshake at `/pair` (carry `pubP`/`confirmP`; desktop returns `pubD`/`confirmD` via the agent WS).
- New: encrypt/decrypt envelope + frames on desktop + client.
- Removed in E2E mode: relay HTML injection / shim / pairing-bootstrap (relay can't read HTML) → the
  mobile UI ships in the app bundle (or a static PWA) instead of being served through the relay.
- Unchanged: routing, multiplexing, `read1()` streaming, Bearer/cookie auth, device_id dedup, durable
  pairing, rate-limit, mirror.

## 10. Rollout
1. Build the native app against the **plaintext** API first (fast, already works) to nail UX.
2. Add the handshake + envelope encryption behind a flag; test desktop↔app zero-knowledge.
3. Flip the hosted relay to **require** E2E; keep plaintext only for self-host mode.
4. (Optional) SPAKE2 upgrade if offline-transcript resistance is ever wanted.

## 11. Non-goals / open
- Metadata privacy (traffic analysis) — not addressed; acceptable.
- Group/multi-desktop key management — current design is per (device, desktop-room) pair.
- Forward secrecy across the device's lifetime — `K_dev` is long-term; add a periodic ECDH ratchet
  later if wanted (not needed for v1).
