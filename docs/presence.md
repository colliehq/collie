# Collie Presence

Collie Presence answers one narrow question: **which dogs in this Slack workspace can accept work
right now?** It is a lease-based roster owned by Collie, not Slack's UI presence indicator.

## Shape of the service

One deployed Cloudflare Worker can serve every Collie workspace. It does not put every customer in
one global object: the `PRESENCE` binding maps each `pack` to its own `PresencePack` Durable Object,
using the Slack workspace ID as the key. That object is the strongly consistent roster for just that
workspace. The existing `RELAY` binding and its `RelayRoom` objects continue to handle iOS remote
sessions separately.

Use Slack's stable IDs on the wire:

- `pack` is the Slack workspace/team ID returned by `auth.test` (`team_id`, normally `T...`).
- `dog` is that app's bot user ID returned by `auth.test` (`user_id`, normally `U...`).

The dog's friendly Collie/Slack name is still what people see, but it can change. It must not be the
lease identity. Enrollment, the runtime socket, and status authentication must all use the same
stable IDs.

## Lease semantics

Each enrolled dog owns one 75-second lease. The listener sends a heartbeat every 20 seconds and
samples local health on every heartbeat:

- `ok` means both the Slack Socket Mode connection and the Collie worker are usable. An unexpired
  `ok` lease is `online: true`.
- `degraded` means the process can still reach the relay, but the dog is not ready for work. It stays
  visible as `connected: true` and `online: false` for diagnosis.
- `offline` means there is no current lease.

A graceful stop sends `bye` and removes the lease immediately. If a machine crashes, loses its
network, sleeps, or is powered off, it cannot send `bye`; the Durable Object alarm expires it no
later than 75 seconds after the last accepted hello or heartbeat. A new session for the same dog
fences the old socket, so delayed heartbeats or a late `bye` cannot overwrite the replacement.

## Deploy and enroll

`relay/wrangler.toml` keeps the existing `RelayRoom` migration as `v1` and adds `PresencePack` as
the `v2` SQLite Durable Object migration. Set the service-level enrollment secret before deploying:

```sh
cd relay
npx wrangler secret put PRESENCE_ADMIN_TOKEN
npx wrangler deploy
```

Do not put the admin secret in `wrangler.toml`, source control, a URL, or a dog launcher. It can mint
or rotate credentials for any pack served by this Worker.

Enrollment is an explicit operator action. Substitute the IDs returned by the dog's Slack
`auth.test` call:

```sh
curl -X POST \
  "https://<worker-host>/presence/enroll?pack=<team-id>&dog=<bot-user-id>" \
  -H "Authorization: Bearer $PRESENCE_ADMIN_TOKEN"
```

The response contains a new per-dog `credential`. Capture it as a secret; the Worker stores only a
domain-separated SHA-256 verifier. Enrolling the same `(pack, dog)` again rotates the credential,
revokes its lease, and closes its old socket.

Retire a dog with the same authenticated endpoint and `DELETE`. This is idempotent: it removes the
verifier and lease, closes any live socket, and removes the otherwise-permanent offline roster row.

```sh
curl -X DELETE \
  "https://<worker-host>/presence/enroll?pack=<team-id>&dog=<bot-user-id>" \
  -H "Authorization: Bearer $PRESENCE_ADMIN_TOKEN"
```

## Configure a dog

Save the Worker socket URL and the credential in the dog's private kennel:

```sh
collie slack setup --name <collie-name> \
  --presence-url "wss://<worker-host>" \
  --presence-token "<per-dog-credential>"
```

This updates `~/.collie/slack.json`, including for an already-provisioned dog. The file is also home
to its Slack tokens and is written with private permissions where the platform supports them. Never
commit or paste it into logs.

At runtime, `--presence-url` or `COLLIE_PRESENCE_URL` can supply the public `wss://` endpoint, and
`COLLIE_PRESENCE_TOKEN` can supply the per-dog bearer credential instead of the kennel value. The
runtime client sends the credential only in the WebSocket `Authorization` header; it is never placed
in the URL, heartbeat frames, logs, generated launchers, or long-lived listener arguments. The
one-time setup command above does receive it as an argument, so treat that terminal and its shell
history as sensitive. Autostart may persist the public URL, but reads the credential from the private
kennel. If the endpoint, credential, workspace ID, or bot user ID is unavailable, Presence stays
disabled and must not claim the dog is online.

The socket protocol is versioned (`v: 1`). A session begins with `hello`, renews with monotonically
increasing `heartbeat` frames, and may end with `bye`; every frame binds the same `pack`, `dog`, and
random session ID. Health is only `ok` or `degraded`.

## Read the roster

Status is available only to an enrolled dog in the same pack. The `dog` query parameter names the
credential owner making the request:

```sh
curl \
  "https://<worker-host>/presence/status?pack=<team-id>&dog=<caller-bot-user-id>" \
  -H "Authorization: Bearer $COLLIE_PRESENCE_TOKEN"
```

The response contains `pack`, `lease_ms`, and a sorted `dogs` array with only `dog`, `connected`,
`online`, `health`, and `expires_at`. It is sent with `Cache-Control: no-store`. It does not expose
credentials, verifier hashes, machine details, tasks, socket owners, or session IDs. A credential
cannot read a different workspace because its verifier is bound to both pack and dog.

## Collie roster versus Slack's green dot

The roster above is the implemented authority for Collie liveness. Its `online` field means the dog
is healthy enough to accept work; it does **not** currently change the green dot shown by Slack.
The existing Slack delegation prompt still gets channel membership from Slack and does not yet
filter it through `/presence/status`, so online-aware delegation is a separate read-side integration.

Socket Mode connectivity cannot drive an Events API bot's native presence directly. Slack's
supported control is the app manifest's `features.bot_user.always_online` setting, which requires
high-privilege, workspace-scoped app-configuration credentials. `relay/slack_presence.js` contains
an optional controller that safely exports the full live manifest, changes only that boolean, and
rotates configuration tokens. It is deliberately not connected to `PresencePack` or a credential
store yet, so deploying this Worker does not enable native green-dot synchronization.

If that optional integration is added, it should react only to roster online/offline edges and keep
Slack configuration credentials in an encrypted, workspace-scoped operator store. Those credentials
must never be mixed with per-dog heartbeat credentials or sent by a Collie listener.
