# Collie iOS hosted-relay integration (protocol v2)

The shipped native client is the reference implementation. Hosted mode is mandatory E2E; do not add
a plaintext fallback.

## Pairing

Scan the v2 HTTPS QR fragment described in [E2E_DESIGN.md](E2E_DESIGN.md). It carries the room,
256-bit one-shot secret and desktop public key. Never accept a query-string secret or send the secret
to `/pair`.

Generate a Keychain-backed stable device id and an ephemeral X25519 keypair. POST only
`{device_id,name,pub,confirm}`. A successful start is `202`, not `200`. Preserve its ticket and poll
`pair/wait` through `validating` and `approval`; verify the returned desktop key/HMAC before showing
the comparison number. Continue polling until explicit `200`, `403`, or expiry. Never resubmit the
pair POST after a lost poll.

Store the returned bearer token and derived `K_dev` in ThisDeviceOnly Keychain storage. `401` means
revoked/re-pair; `403` means authenticated but forbidden; do not collapse them.

## Requests and streams

All hosted API requests use `POST {BASE}sealed`. The actual method, path, query, headers and body are
inside the AES-GCM envelope. The only visible request fields are bearer auth and opaque routing ids.
See the wire format and key derivation in [E2E_DESIGN.md](E2E_DESIGN.md).

Require response sequence `0,1,…`, one head, zero or more data records, and exactly one authenticated
terminal record. Any gap, duplicate, reorder, decrypt error, record after terminal, mismatched
`last_data_seq`, or EOF without terminal is a protocol failure.

Never automatically replay a streamed/non-idempotent run after disconnect. If a `start` event supplied
a session id, reopen that run to reattach; otherwise tell the user to check Runs before explicitly
starting another.

## Device lifecycle

- **Revoke this iPhone:** authenticated `POST device/revoke`. The relay blocks the bearer immediately,
  persists a tombstone, and returns `200` only after the desktop durably deletes its token hash and
  `K_dev` and acknowledges it. Clear Keychain only after that `200`; keep it and offer retry on `503`.
- **Forget locally:** clear local Keychain/settings only. It does not claim remote revocation.
- LAN direct uses the per-process Collie web token and has no per-phone revoke operation.

APNs registration remains a session-authenticated relay control route. Push alerts are intentionally
generic; desktop-supplied titles/output are discarded so notification delivery does not bypass E2E.

## iOS packaging

The iOS 17 target declares local-network usage and ATS local-network access for honest `--lan` setup.
`PrivacyInfo.xcprivacy` declares UserDefaults reason `CA92.1` and the APNs token honestly as an
unlinked Device ID used only for App Functionality, with no tracking. The camera and photo-library
descriptions cover QR scanning. Validate the generated Info.plist and privacy manifest in an Xcode
archive before release.
