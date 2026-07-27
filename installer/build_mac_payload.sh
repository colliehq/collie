#!/usr/bin/env bash
# Stage the private runtime inside Collie.app — a relocatable CPython with collie installed into it,
# so the bundle runs on a Mac that has never seen Python. The macOS counterpart of
# build_payload.ps1; called by build_mac.sh --bundle-python, and idempotent (the tarball is cached,
# so a rebuild is a re-extract, not a re-download).
#
#   bash installer/build_mac_payload.sh <app-path> [arch] [extras]
#
# arch defaults to this machine's, and collie SHIPS arm64 ONLY. python-build-standalone has no
# universal2 build, so covering Intel would mean either a second download or lipo-merging every
# Mach-O in the payload — and macOS 26 Tahoe is the last release to run on Intel Macs at all, which
# Apple stopped selling in 2023. --arch stays so an Intel user can still build for their own
# machine; what it will not do is build for an architecture that cannot be tested here.
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
# Build on a machine of the architecture you are building FOR. Cross-staging via the host pip is
# possible in principle (--target --platform), but it can neither run compileall for the staged
# interpreter's magic number nor be smoke-tested here, and collie ships arm64 only — so refuse
# loudly rather than emit a bundle nobody has ever executed.
if ! "$RES/python/bin/python3" -c "pass" 2>/dev/null; then
  echo "  the staged $PBS_ARCH interpreter cannot run on this $(uname -m) machine." >&2
  echo "  Build the payload on a $ARCH Mac (or in a $ARCH runner)." >&2
  exit 2
fi
"$RES/python/bin/python3" -m pip install --quiet --upgrade pip
"$RES/python/bin/python3" -m pip install --quiet --no-warn-script-location ".[$EXTRAS]"
echo "  collie:  installed via the staged interpreter"

rm -rf "$RES/python/lib/python$PYVER/test" "$RES/python/lib/python$PYVER/idlelib" \
       "$RES/python/lib/python$PYVER/tkinter" "$RES/python/share" 2>/dev/null || true

# ── bytecode: precompile it INTO the bundle, do not strip it ─────────────────────────────────────
# Stripping .pyc looks tidy and is actively harmful here. A signed .app is sealed: every file is
# hashed into the signature. If the bundle ships without bytecode, the first run writes 242 .pyc
# files into it and the seal breaks —
#     spctl: rejected, "a sealed resource is missing or invalid"
# — on the user's machine, after they installed it. So compile everything first, and compile it with
# `unchecked-hash` invalidation so the .pyc stay valid no matter what happens to source mtimes when
# the app is copied out of the dmg.
find "$RES/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$RES/python" -name "*.pyc" -delete 2>/dev/null || true
"$RES/python/bin/python3" -m compileall -q -f --invalidation-mode unchecked-hash \
    "$RES/python/lib/python$PYVER" >/dev/null 2>&1 || true
echo "  bytecode: precompiled $(find "$RES/python" -name '*.pyc' | wc -l | tr -d ' ') files (sealed, so runtime never writes)"

echo "  payload: $(du -sh "$RES/python" | cut -f1)"
