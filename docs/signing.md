# Code signing policy

This page documents how Collie's release binaries are built, reviewed, approved, and signed. It is
the policy referenced in Collie's application to the
[SignPath Foundation](https://signpath.org/) free code-signing program for open-source projects.

## What is signed

The Windows installer **`Collie-Setup.exe`** published on the
[GitHub Releases page](https://github.com/colliehq/collie/releases). The Python wheel/sdist are
published to the same release; the macOS `.dmg` is separately signed and notarised with an Apple
Developer ID.

## How releases are built

Every release is built **from public source by GitHub Actions** — never on a maintainer's machine —
so the build is transparent and auditable:

1. A maintainer pushes a `v*` git tag to `github.com/colliehq/collie`.
2. `.github/workflows/release.yml` runs on GitHub-hosted runners. It builds the installer from the
   tagged commit (`installer/build.ps1` → the Inno backend + shell), the Python wheel (`python -m
   build`), and the macOS bundle, then creates the GitHub Release and uploads the artifacts.
3. The workflow's logs are public: anyone can see exactly which commit produced which artifact.
4. The SHA-256 of each artifact is published by GitHub on the release, and Collie's own updater
   (`collie update`) refuses to install a download whose digest does not match.

There are no manual, off-CI build steps for released artifacts.

## Roles and approval

Collie is a small open-source project. Release approval is exercised by the project **maintainer(s)**,
who are the only accounts with push access to the `colliehq/collie` repository and therefore the only
ones who can create the `v*` tag that triggers a signed build:

| Role | Who | Responsibility |
|---|---|---|
| Committer | Project maintainer(s) | write code, open/merge changes |
| Reviewer | Project maintainer(s) | review changes before a release tag |
| Release approver | Project maintainer(s) | decide a release is ready and push the `v*` tag |

All maintainer accounts have **multi-factor authentication** enabled on GitHub (and on SignPath).
Only a tag on the canonical `colliehq/collie` repository can trigger a signable build — a fork or a
pull request cannot.

## Signing integration

Signing is performed by **SignPath.io** as a CI step in the release workflow: the unsigned
`Collie-Setup.exe` produced by the GitHub Actions build is submitted to SignPath, signed with the
SignPath Foundation certificate (issued by Sectigo), and the signed artifact is what is uploaded to
the GitHub Release. The signing certificate's private key never leaves SignPath's HSM and is never
present in the repository or CI environment.

## How to verify a download

- Check the file's **SHA-256** against the value shown on the
  [release](https://github.com/colliehq/collie/releases) it came from.
- On Windows, right-click the `.exe` → **Properties → Digital Signatures** to see the signer.
- Prefer downloading from the **GitHub Releases page** or from **[collie.run](https://collie.run)**.

## Reporting a problem

Security issues or a suspected tampered binary: open an issue at
[github.com/colliehq/collie/issues](https://github.com/colliehq/collie/issues) (or the contact on the
repository's `SECURITY.md`). Please do not post exploit details publicly before we can respond.
