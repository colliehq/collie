#!/usr/bin/env bash
# Stage the private runtime inside Collie.app — a relocatable CPython with collie installed into it,
# so the bundle runs on a Mac that has never seen Python. The macOS counterpart of
# build_payload.ps1; called by build_mac.sh --bundle-python, and idempotent (the tarball is cached,
# so a rebuild is a re-extract, not a re-download).
#
#   bash installer/build_mac_payload.sh <app-path> [arch] [extras]
#
# arch defaults to this machine's. python-build-standalone ships one build per architecture and no
# universal2, so a distributable really is two disk images (Collie-<ver>-arm64.dmg and -x86_64.dmg)
# rather than one fat app — cross-staging works because nothing here has to *run* the staged python
# except pip, which we run through the host interpreter when the arch isn't ours.
set -euo pipefail
cd "$(dirname "$0")/.."

APP="${1:?usage: build_mac_payload.sh <app-path> [arch] [extras]}"
ARCH="${2:-$(uname -m)}"
EXTRAS="${3:-local,tui,desktop}"
CACHE="${COLLIE_BUILD_CACHE:-$HOME/.cache/collie-build}"
PYVER="3.12"

case "$ARCH" in
  arm64|aarch64) PBS_ARCH="aarch64-apple-darwin" ;;
  x86_64)        PBS_ARCH="x86_64-apple-darwin" ;;
  *) echo "unsupported arch: $ARCH (expected arm64 or x86_64)" >&2; exit 2 ;;
esac

mkdir -p "$CACHE"
echo "── runtime payload · $PBS_ARCH · extras=[$EXTRAS] ──"

# ── resolve + fetch the relocatable CPython ─────────────────────────────────────────────────────
URL=$(curl -fsSL -m 60 "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest" \
  | python3 -c "
import sys, json
rel = json.load(sys.stdin)
want = [a for a in rel['assets']
        if a['name'].startswith('cpython-$PYVER.')
        and '$PBS_ARCH' in a['name']
        and a['name'].endswith('install_only.tar.gz')]
if not want:
    sys.exit('no python-build-standalone asset for $PYVER/$PBS_ARCH in ' + rel['tag_name'])
print(sorted(want, key=lambda a: a['name'])[-1]['browser_download_url'])
")
TARBALL="$CACHE/$(basename "$URL")"
if [ -s "$TARBALL" ]; then
  echo "  cpython: cached $(basename "$TARBALL")"
else
  echo "  cpython: downloading $(basename "$URL")"
  curl -fsSL -m 600 "$URL" -o "$TARBALL.part" && mv "$TARBALL.part" "$TARBALL"
fi

RES="$APP/Contents/Resources"
rm -rf "$RES/python"
mkdir -p "$RES"
tar -xzf "$TARBALL" -C "$RES"          # unpacks to ./python
[ -x "$RES/python/bin/python3" ] || { echo "  payload layout unexpected" >&2; exit 1; }
echo "  cpython: staged $("$RES/python/bin/python3" -V 2>&1 || echo "(foreign arch)")"

# ── install collie into it ───────────────────────────────────────────────────────────────────────
# Use the staged interpreter when it can run here; otherwise drive pip from the host with --target,
# which is what makes cross-arch staging possible at all (pure-Python collie, wheels resolved for
# the target platform).
if "$RES/python/bin/python3" -c "pass" 2>/dev/null; then
  "$RES/python/bin/python3" -m pip install --quiet --upgrade pip
  "$RES/python/bin/python3" -m pip install --quiet --no-warn-script-location ".[$EXTRAS]"
  echo "  collie:  installed via the staged interpreter"
else
  SITE=$("$RES/python/bin/python3" -c "import sys;print(sys.version_info[:2])" 2>/dev/null || echo "")
  python3 -m pip install --quiet --target "$RES/python/lib/python$PYVER/site-packages" \
      --platform macosx_11_0_$( [ "$PBS_ARCH" = "aarch64-apple-darwin" ] && echo arm64 || echo x86_64 ) \
      --only-binary=:all: ".[$EXTRAS]" 2>/dev/null \
    || python3 -m pip install --quiet --target "$RES/python/lib/python$PYVER/site-packages" "."
  echo "  collie:  installed cross-arch via the host pip"
fi

# strip build detritus that would otherwise be signed and shipped
find "$RES/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$RES/python" -name "*.pyc" -delete 2>/dev/null || true
rm -rf "$RES/python/lib/python$PYVER/test" "$RES/python/lib/python$PYVER/idlelib" \
       "$RES/python/lib/python$PYVER/tkinter" "$RES/python/share" 2>/dev/null || true

echo "  payload: $(du -sh "$RES/python" | cut -f1)"
