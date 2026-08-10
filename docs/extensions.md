# Build a Collie extension

Collie's Library installs local extension packages without giving them ambient authority. An
installed package is inert. Collie only activates a version after you review its exact SHA-256,
declared components, connections, and permissions.

The first contract is intentionally narrow: packages can contribute Skills, exact-hash host Hook
definitions, connection descriptors, templates, and assets. They cannot inject Python modules,
model tools, workers, risk reclassification, credentials, or changes to the verification gate.
`host_hooks: true` is a high-authority declaration because a Hook can run a local command; use it
only for packages whose complete contents you have reviewed.

## Try the example

From a Collie checkout:

```powershell
collie library scaffold ./my-extension --id org.example.my-extension --name "My Extension" --publisher "Example Org"
collie library validate examples/extensions/release-helper
collie library plan examples/extensions/release-helper
collie library install examples/extensions/release-helper
collie library enable org.collie.release-helper --approve
collie library show org.collie.release-helper
```

`validate` prints the deterministic package digest and file hashes. `plan` shows the authority and
file diff against the active version. Installation copies the package but does not expose any of
its components. `enable --approve` is the explicit activation boundary.

To distribute the package privately, send the directory through a channel you trust and send its
`validate` digest through an independent channel. The recipient can pin it at install:

```powershell
collie library install ./release-helper --digest <64-character-sha256>
collie library enable org.example.release-helper --approve
```

A correct pin proves the received bytes match the reviewed bytes. It does not prove publisher
identity by itself. Public discovery, publisher signing, and review are deliberately future
distribution-layer work; the local runtime never treats popularity or installation as trust.

## Package layout

Every byte must be declared. Symlinks, path traversal, case-colliding names, undeclared files,
secret-like manifest fields, unbounded packages, incompatible versions, and unsupported component
types are rejected. Package files are not a general-purpose secret scanner: publishers must still
exclude credentials and recipients must review the declared inventory.

```text
release-helper/
├── collie-extension.json
└── skills/
    └── release/
        └── SKILL.md
```

Minimal manifest:

```json
{
  "schema_version": 1,
  "id": "org.example.release-helper",
  "name": "Release Helper",
  "version": "1.0.0",
  "publisher": "Example Org",
  "description": "A reviewable release workflow.",
  "license": "MIT",
  "collie": { "min_version": "0.21.0", "max_version": "1.0.0" },
  "platforms": ["windows", "macos", "linux"],
  "files": ["skills/release/SKILL.md"],
  "components": {
    "skills": ["skills/release/SKILL.md"],
    "hooks": [],
    "connections": [],
    "templates": [],
    "assets": []
  },
  "permissions": {
    "filesystem": [],
    "network": [],
    "credentials": [],
    "browser": [],
    "desktop": [],
    "external_actions": [],
    "host_hooks": false
  },
  "data": { "retention": "none", "export": true, "uninstall": "remove" },
  "verification": []
}
```

The editor-facing [JSON Schema](extension-manifest-v1.schema.json) mirrors schema v1. Runtime
validation remains authoritative because it additionally checks the real file tree, package bytes,
platform compatibility, connection/network relationships, Hook structure, and secret policy.

Rules that matter:

- `id` is a stable lowercase identifier; `version` is semantic versioning.
- `files` lists every package file except `collie-extension.json` itself.
- Every packaged `SKILL.md` is an explicit Skill component; placing another one below an exported
  Skill directory cannot silently add model-visible instructions.
- An `id` keeps one publisher across versions. Name, description, files, and the complete component
  mapping remain versioned review material and never relabel an already-active older version.
- A connection uses an uncredentialed `https://` URL whose host appears in `permissions.network`.
- API-key connections name an environment-variable reference in `secret_ref`; the same uppercase
  name must appear in `permissions.credentials`. Secret values never belong in a package.
- Hook JSON must pass Collie's Hook validator and requires `host_hooks: true`. Its exact file hash
  is trusted only while the approved package version remains enabled and intact.
- `verification` declares evidence a publisher says it ran; installation does not execute or
  elevate that claim. Collie's own package validation and runtime integrity checks remain separate.

## Lifecycle and failure behavior

| State or action | Runtime behavior |
|---|---|
| Installed, not enabled | Inert; components are not discoverable by the agent. |
| Enabled after approval | Exact version's components become available. |
| New version | Installed separately; changed files/scopes require review. |
| Disable | Immediately removes components from runtime discovery. |
| Rollback | Activates the prior *previously active*, approved, intact version; it never promotes a merely installed version. |
| Digest revoked | Matching active package is disabled and cannot be re-enabled. |
| Installed bytes changed or added | Exact inventory/integrity becomes false and long-lived Skill/Hook discovery fails closed. |
| Uninstall | Requires disabling the active version first, unless an explicit force is used. |

Use `collie library audit` for lifecycle receipts, `collie library connections` for active
connection descriptors, and the desktop Library for the same review and lifecycle controls.
`--state-dir` targets an alternate store for that CLI command; set the same path in
`COLLIE_STATE_DIR` when you want the normal Collie runtime to discover that store.
