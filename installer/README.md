# Collie — installers

`collie.iss` builds **`Collie-Setup.exe`**: a single file a non-technical user double-clicks to get
Collie — a real desktop app with a Start-menu/desktop icon, no Python, no terminal, no `pip`, no PATH
surgery. Everything ships inside: an embeddable CPython with `collie-harness[local]` (semantic memory
included), the WebView2-based desktop window and live-wallpaper engine, the browser extension, and
the WebView2 bootstrapper.

| Audience | Path |
|---|---|
| **Everyone** | `Collie-Setup.exe` — bundles Python + collie + WebView2. From the [releases page](https://github.com/colliehq/collie/releases). |
| **Developers** | Clone the repo and run `pip install -e ".[local]"`, or install the wheel from a release; then `collie setup`. |

## What's in this directory

| File | Role |
|---|---|
| `collie.iss` | The Inno Setup script: branded wizard, a custom card-style language page (33 languages, Simplified Chinese up front), tasks, uninstall. |
| `build.ps1` | **The one command to build the exe.** Reads the version, generates art + language data, stages the payload, compiles. |
| `build_payload.ps1` | Recreates `payload/` — the embeddable-Python runtime with collie installed. Called by `build.ps1`; idempotent. |
| `make_art.py` | Generates the wizard's star-map branding BMPs from the logo (reproducible). |
| `gen_langs.py` | Emits `languages.iss` + `langdata.iss` and normalizes vendored translations into warning-clean `lang_compat/` files for the installed Inno version. Edit the `CHIPS`/`MORE` lists here to change which languages are offered. |
| `gen_zhtw.py` | Regenerates the webui's Traditional-Chinese dict from the Simplified one via OpenCC (maintainer tool). |
| `fetch_languages.py` | Downloads Inno's unofficial upstream translations into `lang/` and test-compiles each. Run once when adding new languages. |
| `lang/` | Upstream `.isl` translation sources not bundled with Inno (committed; never hand-patched merely to silence a newer compiler). |

Generated/large paths (`payload/`, `Output/`, `art/`, `languages.iss`, `langdata.iss`,
`lang_compat/`) are `.gitignore`d — `build.ps1` recreates them.

## Build

```powershell
# prerequisites (maintainer/CI machine):
#   - Inno Setup 6+       winget install JRSoftware.InnoSetup
#   - a system Python with Pillow (make_art) — pip install pillow
#   - network access (build_payload downloads the embeddable CPython + WebView2 on first run)

powershell -File installer\build.ps1                 # -> installer\Output\Collie-Setup.exe
powershell -File installer\build.ps1 -CleanPayload   # also rebuild the bundled runtime
```

The version comes from `harness/__init__.py` (single source of truth) and is passed to `iscc` as
`/DAppVer`. CI does the same in `.github/workflows/release.yml`, triggered by pushing a `v*` tag.
The payload build verifies the reviewed SHA-256 of the embeddable-Python archive and pinned
`get-pip.py`, plus the Authenticode publishers of `python.exe` and the Evergreen WebView2
bootstrapper, before executing or packaging them. A new Python version therefore requires an
explicit reviewed `-PyEmbedSha256` value rather than silently trusting a changed download.
Transitive wheels resolved from the optional `local,remote` extras are not yet protected by a
hash-locked requirements file, so the build is fail-closed at its executable bootstrap boundary but
is not bit-for-bit reproducible across dates. Release CI builds on a fresh hosted runner; adding a
reviewed Windows wheel lock remains the boundary for fully hermetic dependency resolution.
The local Collie wheel is built with `--no-build-isolation`, so pip does not create a second hidden
environment and download an additional unreviewed build backend during that step.

Vendored installer translations can lag Inno Setup itself. At build time, `gen_langs.py` treats the
installed compiler's `Default.isl` as the message schema: it retains compatible translated values,
drops obsolete keys, and fills newly introduced keys with Inno's exact English defaults. This
preserves every offered language without guessing at translations and produces the same English
fallback users already received from Inno. `build.ps1` fails the release if `iscc` emits any warning,
so a future compiler/schema change cannot silently reintroduce translation drift.

## What the installer does

- Lays down `{localappdata}\Programs\Collie\python` (the bundled runtime, per-user, no admin).
- On upgrade, atomically renames the previous runtime to an installer-owned backup and restores it
  if extraction/copy is cancelled or fails; it deletes the backup only at Setup's successful
  `ssDone` boundary. User state under `~/.collie` is never part of this rollback.
- Silently ensures the WebView2 runtime (needed by the desktop window).
- Applies the language you picked to Collie itself (`collie config LANG <code>`), so the first launch
  is already in your language.
- Registers a per-user supervisor at logon. It crash-restarts the Web app, job daemon, automation
  daemon, optional browser bridge, and discovered Slack launchers.
- Optional tasks: the live star-map wallpaper and the real-browser bridge. The bridge is enabled or
  disabled in the supervisor config; the wallpaper retains its own logon entry.
- Start-menu + desktop shortcuts to `collie app` (the native window), plus a *Collie Settings*
  shortcut.

On uninstall it requests a graceful supervisor stop, ends/removes its Scheduled Task (or Startup
fallback), stops the wallpaper, removes its logon entry, and deletes `{app}`.

## macOS — `build_mac.sh`

```bash
bash installer/build_mac.sh                 # build + ad-hoc sign (local use)
bash installer/build_mac.sh --sign          # sign with the best identity in the keychain
bash installer/build_mac.sh --sign --dmg    # …and wrap it in Collie-<ver>.dmg
bash installer/build_mac.sh --sign --dmg --notarize <profile>

# standalone — bundles a private CPython, so it runs on a Mac with no Python at all
bash installer/build_mac.sh --bundle-python --sign --dmg
#   --arch arm64|x86_64      (defaults to this machine; see below)
#   --extras local,tui,desktop,remote,claude
```

`--bundle-python` calls `build_mac_payload.sh`, the counterpart of `build_payload.ps1`: it stages a
relocatable CPython from python-build-standalone into `Contents/Resources/python` and installs
collie into it. ~225 MB staged, ~98 MB as a dmg, and every one of the ~198 nested Mach-O binaries
gets signed before the enclosing bundle (the hardened runtime requires it, and signing the outside
first would just be invalidated by the inner writes — `codesign --deep` is Apple-discouraged, so
the script walks them itself). The tarball is cached in `~/.cache/collie-build`, so a rebuild is a
re-extract, not a re-download.

**Releases are arm64 only** — one dmg, one download link, no "which Mac do I have?" question put to
the user. python-build-standalone has no universal2 build, so covering Intel would mean either a
second download or lipo-merging every Mach-O in the payload (~1.5x the size, plus a build step whose
Intel half can't be smoke-tested on an Apple Silicon machine). Against that: macOS 26 Tahoe is the
last release that runs on Intel Macs, and Apple stopped selling them in 2023. `--arch` remains, so
someone on an Intel Mac can still build for their own machine; it refuses to cross-build.

**Why a bundle when a source/wheel install already works: identity.** macOS attaches TCC
permissions to the *application*, so a pip install makes `collie record` ask for Screen Recording on
behalf of your **terminal** — which then holds blanket screen access forever, and System Settings
lists "Terminal" rather than Collie. The PyObjC desktop wallpaper has the same problem in reverse:
unbundled, it appears in the window list as "Python". The bundle fixes both.

Distribution needs a **Developer ID Application** certificate. An *Apple Development* cert signs for
local use only: `codesign --verify` passes, and Gatekeeper still refuses it on every Mac but the one
that built it. `--sign` therefore **errors out** rather than quietly downgrading — a dmg that looks
shippable and isn't is worse than no dmg. Pass `--allow-development` if a local build is what you
actually want. See [DEVELOPER_ID.md](DEVELOPER_ID.md) for how to get the certificate (Account Holder
only — Apple forbids it over the App Store Connect API).

Notarisation credentials, once:

```bash
xcrun notarytool store-credentials collie \
  --key ~/.appstoreconnect/private_keys/AuthKey_<KEYID>.p8 \
  --key-id <KEYID> --issuer <ISSUER-UUID>
```

An API key beats an app-specific password here: it survives an Apple ID password change and is
scoped to what it is allowed to do.

However it was built and signed, the script ends by asking **Gatekeeper** for its verdict on the app
and the dmg, and exits non-zero if either is refused after a notarisation run. `codesign --verify`
only attests that a signature is internally consistent — it is `spctl` that predicts what someone
downloading the dmg will see.

## Homebrew — `brew_release.sh`

For developers, `brew install wudaming00/collie/collie` beats a dmg. The tap lives at
`wudaming00/homebrew-collie`; the formula installs the stdlib-only core into its own virtualenv and
leaves the heavy extras (`local`, `tui`, `desktop`) to `collie setup`.

```bash
bash installer/brew_release.sh              # dry run: build the sdist, rewrite url + sha256
bash installer/brew_release.sh --publish    # …and tag, release, upload and push the tap
```

The tarball is published as a **release asset on the tap**. Keeping that reviewed artifact URL makes
the formula independent of GitHub source-archive layout and gives the tap an explicit checksum.

## Notes

- **Per-user, no admin.** `PrivilegesRequired=lowest`; the supervisor uses a least-privilege
  Task Scheduler logon trigger and degrades to a per-user Startup entry if registration is refused.
- **The desktop engine `.exe`** is compiled once on first run from the shipped C# source via the
  in-box .NET Framework `csc` (no .NET SDK needed).
- **Code signing** is out of scope of the `.iss`. The release workflow signs the setup with Azure
  Artifact Signing and then runs `signtool verify /pa`; an unsigned/invalid installer is not
  published by that workflow.
- **The Inno `.iss` is Windows-only.** macOS uses `build_mac.sh`; Linux and source users install an
  editable checkout or a release wheel. Outside the packaged apps, the desktop window degrades to
  the browser GUI and the wallpaper to a borderless window.
