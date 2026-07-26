# Collie iOS app — integration guide (build in Xcode)

The native app is a **thin client to the relay**. The hard parts (NAT traversal, pairing, durable
sessions, SSE) are already done server-side; the app only pairs once, then talks HTTP/SSE to:

```
BASE = https://collie-relay.wudaming00.workers.dev/r/<room>/
```

`<room>` is stable per desktop (bookmarkable). Make the relay host configurable (settings screen) so
you can point at a self-hosted worker later.

> ⚠️ **Set a browser-like `User-Agent` on every request.** Cloudflare returns `error 1010` to
> default `CFNetwork`/bot UAs. Use e.g. `Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 …) Safari/604.1`.

## 1. Pairing (one-time per device)

1. Scan the QR (or open the link). It encodes `https://…/r/<ROOM>#<PAIRCODE>` — parse `room` from the
   path, `paircode` from the URL fragment.
2. Generate a **stable `device_id`** once (a UUID) and keep it in the Keychain. Re-pairing with the
   same `device_id` **reuses** the device row on the desktop (no duplicate); a new id = a new device.
3. `POST {BASE}pair`  JSON `{ "paircode": "<code>", "device_id": "<uuid>", "name": "iPhone" }`
   → `200 { "ok": true, "token": "<session-token>" }`. Store `token` + `room` in the Keychain.
   (`403 bad pairing code`, `429 too many attempts`, `503 desktop offline`.)

## 2. Auth (every request after pairing)

Send `Authorization: Bearer <token>`. (Browsers use a cookie; native uses this header — the relay
accepts both.) A `401` means the device was kicked or the desktop forgot it → prompt to re-pair.

## 3. API (all under BASE)

| Method | Path | Notes |
|---|---|---|
| GET  | `api/sessions` | `{ sessions:[{id,title,turns}] }` — chat list |
| GET  | `api/session/<id>` | `{ messages:[{role,content}] }` — transcript (content is string or, for user images, an array) |
| GET  | `api/stream?q=<text>&mode=normal&session=<id>` | **SSE** run. Omit `session` to start a new one; `start` returns the new id |
| POST | `api/steer`  `{session,text}` | inject text into the in-flight run |
| GET  | `api/mirror?session=<id>` | **SSE** — mirror a run started elsewhere (desktop / another device), token-by-token |
| GET  | `api/settings` | `values.LANG` → match the desktop's UI language |

### SSE events (`api/stream` and `api/mirror`)
`start {session,provider,cwd,prior_turns}` · `token {t}` (append to the assistant bubble) ·
`tool {name,args,ok}` (show a step line) · `steer {text}` · `done {answer,error,cost_usd,…}` ·
`ping {}` (keep-alive, ignore).

### Reading SSE in Swift (URLSession, no library)
```swift
var req = URLRequest(url: URL(string: base + "api/stream?q=\(q)&mode=normal&session=\(sid)")!)
req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
req.setValue(browserUA, forHTTPHeaderField: "User-Agent")
let (bytes, _) = try await URLSession.shared.bytes(for: req)
var event = ""
for try await line in bytes.lines {
    if line.hasPrefix("event:") { event = line.dropFirst(6).trimmingCharacters(in: .whitespaces) }
    else if line.hasPrefix("data:") {
        let json = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
        handle(event, json)   // decode + update UI on MainActor
    }
}
```
The run lives as long as this connection stays open — don't cancel it mid-run unless you mean to stop.
To send while it runs, `POST api/steer`. To observe a desktop-started run, open `api/mirror` for that
session and render the same events (skip your own run to avoid double-rendering).

## 4. Voice (on the phone — Collie ships no STT/TTS model)

- **Speech → text**: `SFSpeechRecognizer` (request `SFSpeechRecognizer.requestAuthorization`; set
  `requiresOnDeviceRecognition = true` for offline). Feed the final transcript as `q` to `api/stream`.
  (iOS Safari/PWA has **no** web STT — this is why voice input belongs in the native app.)
- **Text → speech** (optional, read replies aloud): `AVSpeechSynthesizer` on `done.answer`.

Collie never sees audio — only the transcribed text. Nothing to add server-side.

## 5. Desktop side (already shipped)
`collie web --remote`, or just turn on **Settings → Remote → Phone remote access** once — then remote
auto-starts whenever Collie runs. Manage/rename/kick paired devices at `http://127.0.0.1:<port>/remote`.
Relay source: `relay/`. Pairing/session/mirror logic: `harness/remote.py`, `harness/webapp.py`.
