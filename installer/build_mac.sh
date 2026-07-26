#!/usr/bin/env bash
# Build Collie.app — the macOS bundle.
#
#   bash installer/build_mac.sh                 # build + ad-hoc sign (local use)
#   bash installer/build_mac.sh --sign          # sign with a Developer ID / Development identity
#   bash installer/build_mac.sh --sign --dmg    # …and wrap it in Collie-<ver>.dmg
#   bash installer/build_mac.sh --sign --dmg --notarize collie   # …and notarise via a stored profile
#   bash installer/build_mac.sh --bundle-python --sign --dmg      # standalone: no Python required
#        --arch arm64|x86_64   --extras local,tui,desktop
#
# WHY a bundle at all, when `pip install collie-harness` already works: identity. macOS attaches
# TCC permissions (Screen Recording, Camera, Microphone) to the *application*, so a pip install
# means `collie record` asks your terminal to be granted screen recording — the terminal then holds
# blanket screen access forever, and System Settings lists "Terminal", not Collie. A bundle asks as
# Collie, and the desktop wallpaper stops showing up in the window list as "Python".
#
# Without --bundle-python this is the DEVELOPER bundle: it runs the collie already on this machine.
# With it, build_mac_payload.sh stages a private CPython inside the app (the counterpart of
# build_payload.ps1) so it runs on a Mac that has never had Python.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION=$(python3 -c "import re;print(re.search(r'__version__ = \"([^\"]+)\"',open('harness/__init__.py').read()).group(1))")
APP="installer/Output/Collie.app"
SIGN=0; DMG=0; ALLOW_DEV=0; NOTARY_PROFILE=""; BUNDLE_PY=0; ARCH="$(uname -m)"; EXTRAS="local,tui,desktop"
while [ $# -gt 0 ]; do
  case "$1" in
    --sign) SIGN=1 ;;
    --allow-development) ALLOW_DEV=1 ;;
    --dmg) DMG=1 ;;
    --notarize) NOTARY_PROFILE="${2:-}"; shift ;;
    --bundle-python) BUNDLE_PY=1 ;;
    --arch) ARCH="${2:?}"; shift ;;
    --extras) EXTRAS="${2:?}"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

echo "── Collie.app $VERSION ─────────────────────────────────"
rm -rf "$APP"; mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# ── icon: the shipped SVG -> .icns, every size Finder asks for ──────────────────────────────────
ICONSET=$(mktemp -d)/collie.iconset; mkdir -p "$ICONSET"
if command -v rsvg-convert >/dev/null 2>&1; then RENDER=rsvg
elif command -v sips >/dev/null 2>&1 && command -v qlmanage >/dev/null 2>&1; then RENDER=ql
else RENDER=none; fi
for sz in 16 32 64 128 256 512 1024; do
  case "$RENDER" in
    rsvg) rsvg-convert -w $sz -h $sz assets/collie-logo.svg -o "$ICONSET/icon_${sz}x${sz}.png" ;;
    *)    : ;;   # no SVG rasteriser -> skip the icon rather than fail the build
  esac
done
if [ "$RENDER" = "rsvg" ]; then
  # iconutil wants the @2x names too
  for sz in 16 32 128 256 512; do
    cp "$ICONSET/icon_$((sz*2))x$((sz*2)).png" "$ICONSET/icon_${sz}x${sz}@2x.png" 2>/dev/null || true
  done
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/collie.icns" && echo "  icon: collie.icns"
else
  echo "  icon: skipped (no rsvg-convert — brew install librsvg for a real app icon)"
fi

# ── runtime + launcher ───────────────────────────────────────────────────────────────────────────
if [ "$BUNDLE_PY" = "1" ]; then
  bash installer/build_mac_payload.sh "$APP" "$ARCH" "$EXTRAS"
  # $0's own dir, resolved at run time: the app must work from /Applications, a dmg, or anywhere
  # the user dragged it, so nothing here may bake in a build-machine path.
  cat > "$APP/Contents/MacOS/Collie" <<'LAUNCHER'
#!/bin/bash
# Bundle entry point — runs the private runtime inside this .app. No system Python involved.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export COLLIE_BUNDLED=1
exec "$HERE/Resources/python/bin/python3" -m harness.cli app "$@"
LAUNCHER
  echo "  launcher -> bundled runtime"
else
  COLLIE_BIN="$(command -v collie || echo "$PWD/.venv/bin/collie")"
  cat > "$APP/Contents/MacOS/Collie" <<LAUNCHER
#!/bin/bash
# Bundle entry point. Everything the app does is collie; the bundle exists to give it a stable
# identity for TCC, the Dock and the window list.
exec "$COLLIE_BIN" app "\$@"
LAUNCHER
  echo "  launcher -> $COLLIE_BIN (developer bundle; --bundle-python for a standalone app)"
fi
chmod +x "$APP/Contents/MacOS/Collie"

# ── Info.plist. The NS*UsageDescription strings are NOT optional: without them macOS kills the
#    process the instant it touches the camera or microphone, instead of prompting. ─────────────
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key>                  <string>Collie</string>
  <key>CFBundleDisplayName</key>           <string>Collie</string>
  <key>CFBundleIdentifier</key>            <string>run.collie.desktop</string>
  <key>CFBundleVersion</key>               <string>$VERSION</string>
  <key>CFBundleShortVersionString</key>    <string>$VERSION</string>
  <key>CFBundleExecutable</key>            <string>Collie</string>
  <key>CFBundleIconFile</key>              <string>collie</string>
  <key>CFBundlePackageType</key>           <string>APPL</string>
  <key>LSMinimumSystemVersion</key>        <string>11.0</string>
  <key>NSHighResolutionCapable</key>       <true/>
  <key>NSCameraUsageDescription</key>
    <string>Collie records a webcam bubble into your screen recordings.</string>
  <key>NSMicrophoneUsageDescription</key>
    <string>Collie records your microphone into your screen recordings.</string>
  <key>NSAppleEventsUsageDescription</key>
    <string>Collie opens finished recordings in your default player.</string>
</dict>
</plist>
PLIST
echo "  Info.plist: run.collie.desktop $VERSION"

# ── sign. Hardened runtime is required for notarisation; it also blocks the JIT-ish tricks
#    PyObjC does not need, so the desktop engine is unaffected. ───────────────────────────────────
ENTITLEMENTS=$(mktemp).plist
cat > "$ENTITLEMENTS" <<ENT
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>com.apple.security.device.camera</key>        <true/>
  <key>com.apple.security.device.audio-input</key>   <true/>
  <key>com.apple.security.cs.allow-jit</key>         <true/>
  <key>com.apple.security.cs.disable-library-validation</key> <true/>
</dict></plist>
ENT

# Every Mach-O inside the bundle must carry its own signature under the hardened runtime, and they
# must be signed BEFORE the enclosing bundle — sign the outside first and the inner writes
# invalidate it. (`codesign --deep` is Apple-discouraged and skips entitlements, so: do it by hand.)
sign_nested() {
  local id="$1"; shift
  local n=0
  while IFS= read -r f; do
    codesign --force --options runtime "$@" --sign "$id" "$f" 2>/dev/null && n=$((n+1)) || true
  done < <(find "$APP/Contents/Resources" -type f \( -name "*.so" -o -name "*.dylib" -o -perm -u+x \) 2>/dev/null \
           | while IFS= read -r f; do file -b "$f" | grep -q "Mach-O" && echo "$f"; done)
  [ "$n" -gt 0 ] && echo "  signed $n nested binaries" || true
}

if [ "$SIGN" = "1" ]; then
  ID=$(security find-identity -v -p codesigning | { grep "Developer ID Application" || true; } \
       | head -1 | sed -E 's/.*"(.*)"/\1/')
  if [ -z "$ID" ]; then
    ID=$(security find-identity -v -p codesigning | { grep -E "Apple Develop(ment|er)" || true; } \
         | head -1 | sed -E 's/.*"(.*)"/\1/')
    if [ "$ALLOW_DEV" != "1" ]; then
      echo "  no 'Developer ID Application' certificate." >&2
      echo "  Signing with the Development cert instead would produce a .dmg that LOOKS shippable and" >&2
      echo "  that Gatekeeper rejects on every Mac but this one — so this is an error, not a warning." >&2
      echo "  Create one (Account Holder only, Apple forbids it over the App Store Connect API):" >&2
      echo "    Xcode -> Settings -> Accounts -> Manage Certificates -> + -> Developer ID Application" >&2
      echo "  Or pass --allow-development if you genuinely only want a local build." >&2
      exit 1
    fi
    echo "  !! --allow-development: signing with $ID"
    echo "     LOCAL USE ONLY. Gatekeeper rejects this on anyone else's Mac; notarisation refuses it."
  fi
  [ -n "$ID" ] || { echo "  no codesigning identity at all" >&2; exit 1; }
  sign_nested "$ID" --timestamp
  codesign --force --options runtime --timestamp --entitlements "$ENTITLEMENTS" \
           --sign "$ID" "$APP"
  echo "  signed: $ID"
else
  sign_nested -
  codesign --force --sign - "$APP"        # ad-hoc: enough for a stable local TCC identity
  echo "  signed: ad-hoc (local only)"
fi
codesign --verify --deep --strict --verbose=1 "$APP" 2>&1 | sed 's/^/  verify: /'

# ── dmg ──────────────────────────────────────────────────────────────────────────────────────────
if [ "$DMG" = "1" ]; then
  DMG_PATH="installer/Output/Collie-$VERSION.dmg"
  STAGE=$(mktemp -d); cp -R "$APP" "$STAGE/"; ln -s /Applications "$STAGE/Applications"
  rm -f "$DMG_PATH"
  hdiutil create -volname "Collie" -srcfolder "$STAGE" -ov -format UDZO "$DMG_PATH" >/dev/null
  echo "  dmg: $DMG_PATH"
  if [ -n "$NOTARY_PROFILE" ]; then
    echo "  notarising (profile: $NOTARY_PROFILE) …"
    xcrun notarytool submit "$DMG_PATH" --keychain-profile "$NOTARY_PROFILE" --wait
    xcrun stapler staple "$DMG_PATH"
    echo "  stapled."
  else
    echo "  not notarised. Store credentials once (an App Store Connect API key beats an"
    echo "  app-specific password: it does not expire on a password change and is scoped):"
    echo "     xcrun notarytool store-credentials collie \\"
    echo "       --key ~/.appstoreconnect/private_keys/AuthKey_<KEYID>.p8 \\"
    echo "       --key-id <KEYID> --issuer <ISSUER-UUID>"
    echo "   then re-run with:  --notarize collie"
  fi
fi

# ── the check that matters ───────────────────────────────────────────────────────────────────────
# `codesign --verify` only says the signature is internally consistent — a Development-signed bundle
# passes it happily and is still refused on every other Mac. Gatekeeper's own verdict is the only one
# that predicts what a downloader sees, so ask for it by name and let it set the exit status.
verdict() {
  local what="$1" path="$2"; shift 2
  local out; out=$(spctl -a -vv "$@" "$path" 2>&1) || true
  if grep -q "accepted" <<<"$out"; then
    echo "  gatekeeper: $what ACCEPTED — $(grep -o 'source=.*' <<<"$out" | head -1)"
    return 0
  fi
  echo "  gatekeeper: $what REJECTED — $(head -2 <<<"$out" | tail -1)" >&2
  return 1
}

ok=0
verdict "app" "$APP" -t exec || ok=1
[ "$DMG" = "1" ] && { verdict "dmg" "$DMG_PATH" -t open --context context:primary-signature || ok=1; }
if [ "$ok" != "0" ] && [ -n "$NOTARY_PROFILE" ]; then
  echo "  notarisation ran but Gatekeeper still refuses this build — do NOT ship it." >&2
  exit 1
fi

echo "── done: $APP"
