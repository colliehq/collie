# Code signing and release integrity

This page describes the release path implemented by
`.github/workflows/release.yml`. A downloadable release is built from its public tagged commit on
GitHub-hosted runners after the complete Python, Node, and browser quality gate passes.

## Published artifacts

A tagged release contains:

- **`Collie-Setup.exe`** for Windows, signed with Azure Artifact Signing;
- **`Collie-arm64.dmg`** for Apple-silicon Macs, Developer ID signed, notarised, and stapled;
- **`Collie-VSCode.vsix`** for VS Code;
- the Python wheel and source distribution.

The canonical download location is the
[GitHub Releases page](https://github.com/colliehq/collie/releases). A manual workflow run may build
unsigned macOS output for diagnostics, but it does not publish a release. A tag fails instead of
publishing an unsigned or un-notarised DMG.

## Windows signing

The release job builds the plain Inno Setup installer, then signs it through **Azure Artifact
Signing** using SHA-256 and an RFC 3161 timestamp. GitHub exchanges a short-lived OIDC token for the
signing authority; no long-lived Azure client secret or signing private key is stored in the
repository. The expected publisher is **Daming Wu** and the certificate chains to Microsoft's public
trust infrastructure.

After signing, CI runs `signtool verify /pa /v` and fails if Windows does not accept the signature.
Collie's Windows updater separately requires both the release asset's SHA-256 digest and a valid
Authenticode signature from that publisher before it launches an installer.

## macOS signing

The macOS job imports a Developer ID Application certificate into a temporary keychain and stores
App Store Connect credentials only for the life of the hosted runner. `build_mac.sh` signs nested
Mach-O files and the app, signs the DMG, submits it for notarisation, staples the ticket, and asks
Gatekeeper (`spctl`) for the final verdict. See the
[Developer ID setup guide](https://github.com/colliehq/collie/blob/main/installer/DEVELOPER_ID.md)
for the maintainer setup.

## Release authority

Only a `v*` tag on the canonical `colliehq/collie` repository can trigger publication. The workflow
first checks that the tag matches `harness.__version__`; every artifact job then depends on the full
quality gate. Pull requests and forks cannot enter the release environment or use its signing
authority.

## Verify a download

- Download from [GitHub Releases](https://github.com/colliehq/collie/releases) or a link on
  [collie.run](https://collie.run) that points there.
- Compare the file's SHA-256 with the digest shown for that release asset.
- On Windows, open **Properties → Digital Signatures** and confirm a valid signature from
  **Daming Wu**. PowerShell users can also run `Get-AuthenticodeSignature .\Collie-Setup.exe`.
- On macOS, `spctl -a -vv -t open Collie-arm64.dmg` should report an accepted notarised Developer ID
  artifact.

## Report a problem

For a suspected tampered binary or security issue, open a minimal report on
[GitHub Issues](https://github.com/colliehq/collie/issues) with the affected release, filename, and
observed digest. Do not include exploit details, credentials, or private data in a public issue; ask
the maintainer to establish a private reporting channel first.
