# Optional Collie Online

Collie Online is the optional account and coordination layer above local Collie. A device remains
complete without signing in. Online is for people who want the same Collie across devices or inside
a team.

It adds paired-device identity, project-scoped memory and policy sync, sealed learned-workflow
sync, reviewed MCP connections,
Mission handoff to local/home nodes, schedules, journals, and deterministic daily reports. Complex
execution still runs on a node you control. Cloud Light is off by default and accepts only an
explicit, budgeted `summarize`, `classify`, `extract`, or `draft` task using `cloud_indexed` input.

## Data boundaries

| Class | Where it may go |
|---|---|
| `cloud_indexed` | Server-readable sync and an explicitly selected Cloud Light task |
| `sealed` | Client-encrypted before upload; decrypted only on devices holding the recovery key |
| `device_only` | Never uploaded |
| `secret` | Connection Vault or separate device credential store; never prompts, memory, or reports |

Provider login caches, browser cookies, local stdio commands, and desktop sessions are never shared.
Raw procedural observations are also always `device_only`; only locally derived workflow candidates
and accepted routines can enter sealed sync. See [Procedural memory](procedural-memory.md).
For MCP, you select one named remote HTTPS connection, review its exact tool manifest, and upload
only that connection's credential into the envelope-encrypted Vault.

## Start in Connected Mode

```bash
pip install -e ".[online]"
collie online login --server https://api.example.com
collie online project-create "My project" --local-project my-project
collie online sync
collie online node-serve
```

The first sealed object creates a user-held recovery key. Export it only in a private terminal and
import it once on another authorized device:

```bash
collie online key-export --yes
collie online key-import 'collie-seal-v1:…'
```

## Teams and devices

```bash
collie online devices
collie online workspaces
collie online workspace-create "My team"
collie online workspace-use --workspace-id WORKSPACE_ID
collie online workspace-member-add --user-id USER_ID --role member
collie online project-member-add --project-id PROJECT_ID --user-id USER_ID --role member
```

Workspace membership does not grant access to every project. Project membership is checked again
for sync, Missions, schedules, journals, reports, and project-scoped connections. Owners and admins
can manage bounded roles; admins cannot replace owners or promote another admin.

`collie online logout` revokes the current device before removing its local session. Use
`collie online device-revoke DEVICE_ID` to revoke another paired device.

## Authority and execution

Ordinary reads, preparation, project writes, and reversible scoped work do not create repetitive
approval cards. Explicit authenticated instructions such as Send or Publish authorize that exact
commit. Draft does not. Changed MCP manifests, expanded targets or budgets, permanent/high-impact
operations, and person-bound CAPTCHA/passkey/biometric/hardware-key challenges still stop safely.

“Endpoint enforcement” does not mean walking back to the computer to click every prompt. A trusted
phone or control device can approve a high-impact action by signing a short-lived ticket bound to
the exact action, parameters, target endpoint, and expiry. A stored signed mandate can cover bounded
recurring work without another prompt. Only device enrollment, recovery, key replacement, and policy
expansion need the root authorization ceremony.

Every submitted Mission and schedule template is signed by its originating endpoint and bound to one
named execution endpoint. Online stores
and routes the signature but cannot create one. The execution node pulls work over outbound HTTPS,
checks the signer against its local trust pins, verifies every task field and time bound, records the
authorization as consumed to stop replay, and only then invokes the normal Authority engine. OAuth
account membership alone never grants endpoint execution trust.

To allow signed handoff from another device after verifying its key out of band:

```bash
collie online trust-device "My phone" --device-id DEVICE_ID --public-key ED25519_PUBLIC_KEY
collie online trusted-devices
```

Mission and schedule commands default to the issuing device. A deliberate handoff names the
destination explicitly with `--target-device-id DEVICE_ID`; there is no cloud-selected “run on any
computer” mode because an untrusted coordinator could otherwise fan one valid authorization out to
several endpoints.

### MCP compromise boundary

The reference `cloud_proxy` Vault protects a stolen database, not a simultaneous compromise of the
cloud runtime and its Vault KEK. Such an attacker still cannot enter a customer computer, but could
abuse that connection's external SaaS account. Treat it as an explicit convenience mode, not the
strongest security profile. `collie online share-mcp` now defaults to `device_direct`: Online stores
only end-to-end credential ciphertext, cryptographically bound to the reviewed connection definition,
and endpoints holding the user's sealed-sync key decrypt and invoke it locally. Cross-user project
sharing still needs a project group-key ceremony before it can claim the same compromise boundary.

The hosted control plane is maintained as a separate service repository so the open local client,
protocol, privacy boundary, and trust enforcement can evolve without publishing production
infrastructure. The frozen public product and protocol contract is in
[`COLLIE_ONLINE_V1.md`](COLLIE_ONLINE_V1.md).
