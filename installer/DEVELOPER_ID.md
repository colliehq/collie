# Getting a Developer ID Application certificate

`installer/build_mac.sh --sign` refuses to build without one, because signing with the *Development*
certificate instead produces a `.dmg` that looks shippable and that Gatekeeper rejects on every Mac
but the one that built it. That is what happened to `Collie-0.18.0.dmg`:

```
$ codesign -dv installer/Output/Collie.app
Authority=Apple Development: Daming Wu          # not Developer ID
$ spctl -a -vv -t exec installer/Output/Collie.app
rejected
source=no usable signature
```

## Why you have to do this by hand

Apple forbids creating Developer ID certificates over the App Store Connect API, whatever the key's
role:

```
POST /v1/certificates {certificateType: DEVELOPER_ID_APPLICATION_G2}
403  This operation can only be performed by the Account Holder.
```

There is no key, token or role that lifts this. It is a deliberate restriction: a Developer ID cert
signs software that runs on every Mac in the world, and each team gets at most five, forever.

## The two-minute path (Xcode)

1. Xcode → Settings → Accounts → select your Apple ID → **Manage Certificates…**
2. **+** (bottom left) → **Developer ID Application**
3. Done. The private key stays in your login keychain; nothing to download or import.

Confirm it landed:

```sh
security find-identity -v -p codesigning | grep "Developer ID Application"
```

## Or, from the developer portal

A CSR is already generated at `~/.collie/signing/devid.csr` (the private key sits beside it as
`devid.key`, mode 600 — it never leaves this machine and must not be committed).

1. https://developer.apple.com/account/resources/certificates/add
2. Software → **Developer ID Application** → Continue
3. Upload `~/.collie/signing/devid.csr` → Continue → Download `developerID_application.cer`
4. Import it together with its key:

```sh
security import ~/Downloads/developerID_application.cer -k ~/Library/Keychains/login.keychain-db
security import ~/.collie/signing/devid.key -k ~/Library/Keychains/login.keychain-db \
    -T /usr/bin/codesign
```

## Then the build is one command

Notarisation credentials are already stored (keychain profile `collie`, validated against Apple), so:

```sh
bash installer/build_mac.sh --sign --dmg --bundle-python --notarize collie
```

It signs with the Developer ID cert, notarises, staples, and then asks Gatekeeper for its verdict on
both the `.app` and the `.dmg` — exiting non-zero if either is still refused, so a rejected build
cannot be mistaken for a shippable one.

## Getting it into CI

The release workflow's `dmg` job signs and notarises only if these repository secrets exist, and
builds an unsigned dmg (that it refuses to call shippable) if they don't. Export the certificate
*with its private key* — Keychain Access → My Certificates → right-click the **Developer ID
Application** entry → Export → `.p12`, and pick a password:

```sh
# from the exported file, never pasted into a terminal that logs history
base64 -i ~/Downloads/DeveloperID.p12 | pbcopy      # -> secret MACOS_CERT_P12
```

| Secret | What it is |
|---|---|
| `MACOS_CERT_P12` | base64 of the exported `.p12` (certificate **and** private key) |
| `MACOS_CERT_PASSWORD` | the password you chose during that export |
| `ASC_KEY_P8` | base64 of the App Store Connect API key `.p8` |
| `ASC_KEY_ID` | the key id — the `XXXX` in `AuthKey_XXXX.p8` |
| `ASC_ISSUER_ID` | the issuer UUID from App Store Connect → Users and Access → Integrations |

```sh
gh secret set MACOS_CERT_P12 --repo colliehq/collie < <(base64 -i ~/Downloads/DeveloperID.p12)
gh secret set ASC_KEY_P8     --repo colliehq/collie < <(base64 -i ~/.appstoreconnect/private_keys/AuthKey_XXXX.p8)
gh secret set MACOS_CERT_PASSWORD --repo colliehq/collie
gh secret set ASC_KEY_ID          --repo colliehq/collie
gh secret set ASC_ISSUER_ID       --repo colliehq/collie
```

The `.p12` is the private key that signs software as you. Treat it like the Global API Key: it lives
in a file, it goes into a secret store, and it never appears in a chat, a commit or a log line.

## What a complete macOS release looks like

| Piece | State |
|---|---|
| Standalone bundle (private CPython, no system Python) | done — `--bundle-python` |
| Extensions actually load inside it | done — needed the nested-entitlements fix |
| Signature survives first launch | done — bytecode is precompiled, so the seal is never broken |
| App icon | done — rsvg-convert, else Chrome |
| Both architectures | done in CI — `macos-14` (arm64) + `macos-13` (Intel) |
| Developer ID signature + notarisation | **needs the certificate above** |
| Published next to `Collie-Setup.exe` | done in CI — the `dmg` job feeds the same release |
| Landing page download button | still says "coming soon"; flip it when the first signed dmg ships |
| Auto-update | not built. Homebrew users get `brew upgrade`; dmg users re-download. |
