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
