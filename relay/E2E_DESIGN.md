# Collie Remote hosted protocol v2

**Status:** implemented by `harness/remote.py`, `relay/worker.js`, and `collie-ios`.

## Security goal and threat model

The hosted relay is treated as untrusted. It may inspect, modify, drop, duplicate, reorder, delay, or
replay anything it handles. A passive network observer is also in scope. Protocol v2 protects API
content (methods, paths, queries, prompts, headers, request bodies, response bodies and SSE) and
authenticates the desktop during pairing.

The relay still sees unavoidable metadata: room, opaque request/session ids, bearer-token hashes,
APNs registration metadata, sizes, timing, connection IPs, and whether pairing was approved. It can
deny service and perform traffic analysis. A compromised phone/desktop, malicious local Collie web
process, endpoint screenshots, and endpoint key extraction are out of scope.

Hosted v2 is native-app-only and fail closed. There is no hosted plaintext mode, injected browser UI,
legacy short-code flow, or optional encryption branch.

## Relay-blind, desktop-authenticated pairing

The desktop generates a fresh 256-bit `secret` and an ephemeral X25519 keypair. Its QR contains:

```
https://<relay>/r/<room>#pair=<base64url({
  "v": 2, "room": room, "secret": secret, "desktop_pub": base64(pubD)
})>
```

The fragment is never included in an HTTP request. The desktop WebSocket `hello` also never contains
the secret. The phone creates `pubP` and sends `/pair` only:

```
{device_id, name, pub: base64(pubP), confirm: base64(confirmP)}
```

The existing cross-language crypto framing in `harness/e2e.py` and `CollieIOS/Pairing/E2E.swift` is:

```
transcript = LP("collie-e2e-v1") || LP(room) || LP(pubD) || LP(pubP)
confirmP   = HMAC-SHA256(secret, transcript || "P")
confirmD   = HMAC-SHA256(secret, transcript || "D")
S          = X25519(privD, pubP) = X25519(privP, pubD)
K_dev      = HKDF-SHA256(S, salt=room, info="collie-remote-device")
```

The relay stores a durable ticket in `validating` state and forwards the proof. Only the desktop can
verify it. A valid proof immediately consumes and rotates the secret, before approval or token issue.
The desktop allows five failed proofs per ten minutes and burns the secret at the limit. It derives a
four-digit comparison number from `confirmD`, returns `pair_ready`, and asks the human. Every pairing,
including a known device id, requires approval.

The phone polls `/pair/wait?ticket=…`: `validating` → `approval` (with authenticated `pubD`,
`confirmD`, and number) → approved/denied. It verifies `pubD`, `confirmD`, and the number before
display. An approved ticket is claimed transactionally and can mint exactly one bearer token. `K_dev`
is saved only after that token is issued. Pairing secrets and device keys never reach the relay.

This is authenticated ECDH with a high-entropy out-of-band secret, not a short-password protocol; it
does not need a PAKE's offline-dictionary protection. The QR remains sensitive until its 180-second
expiry or one-shot consumption.

## Fixed encrypted transport

Every hosted API call is externally identical:

```
POST /r/<room>/sealed
Authorization: Bearer <device token>
X-Collie-Rid: <random UUID>
X-Collie-Session: <opaque session id>
Content-Type: application/octet-stream

{n,ct}
```

The request body is the encrypted envelope and contains `{method,path,headers,body_b64}`. Keeping it
out of request headers avoids the platform's much smaller header-size ceiling. `/api/...` outer paths,
legacy `X-Collie-Enc` envelopes, and unsealed requests are never proxied. Missing E2E support aborts
desktop hosted startup.

Per session:

```
K_sess = HKDF-SHA256(K_dev, info="collie-remote-session" || LP(session))
AAD    = LP(room)||LP(request_id)||LP(session)||LP(direction)||UInt64BE(seq)
seal   = AES-256-GCM(K_sess, fresh random 96-bit nonce, plaintext, AAD)
```

Responses are newline-delimited opaque frames with exact contiguous `seq` values. Their decrypted
records are:

```
seq 0: {"kind":"head","status":N,"headers":{...}}
seq n: {"kind":"data","data_b64":"..."}
last:  {"kind":"terminal","ok":true|false,"last_data_seq":N,"error":"..."?}
```

The Worker rejects a duplicate head, plaintext frame, duplicate sequence, or gap. The phone performs
the same checks, authenticates every record, rejects malformed/decrypt-failed frames, rejects records
after terminal, and treats EOF without a matching authenticated terminal as failure. Plaintext `end`
only closes transport; it does not prove completion.

## Replay and lifecycle

Phone request ids are random UUIDs. The desktop persists a bounded 30-day ledger of accepted
non-idempotent ids and answers a duplicate with authenticated `409 duplicate_request`; GET/HEAD remain
retryable. The iOS run client never automatically replays a streamed question after disconnect,
because it cannot know whether execution already began. It tells the user to reopen/reattach to the
session or decide explicitly whether to create another run.

Bearer tokens and `K_dev` live in iOS Keychain (`AfterFirstUnlockThisDeviceOnly`). Desktop `K_dev` and
token hashes live in its private device store. Hosted “revoke” removes authorization at the relay
edge immediately, persists a reconnect-safe tombstone, and returns success only after the desktop
acknowledges durable deletion of the token hash and key. iOS retains its credential on failure so it
can retry; “forget locally” only clears the phone. A `401` requires re-pairing; an authenticated `403`
is authorization failure.

`/pair`, `/pair/wait`, `/push/register`, and `/device/revoke` are explicit relay-control endpoints.
They expose only pairing proof/ticket state, APNs metadata, or token hashes—not application content.
Push alert text is fixed and generic; caller-supplied run titles/output never enter the relay.

## Verification

- `node tests/relay_pairing_test.js`
- `node tests/relay_sealed_test.js`
- `node tests/relay_push_test.js`
- `pytest -q tests/test_remote_protocol_v2.py tests/test_e2e.py`
- On macOS: generate the Xcode project and run the iOS E2E checker/build.
