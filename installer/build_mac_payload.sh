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
EXTRAS="${3:-local,tui,desktop,remote,claude}"
CACHE="${COLLIE_BUILD_CACHE:-$HOME/.cache/collie-build}"
PYVER="3.12"

case "$ARCH" in
  arm64|aarch64) PBS_ARCH="aarch64-apple-darwin" ;;
  x86_64)        PBS_ARCH="x86_64-apple-darwin" ;;
  *) echo "unsupported arch: $ARCH (expected arm64 or x86_64)" >&2; exit 2 ;;
esac

# Cross-building is refused rather than quietly attempted. It "works" — the host pip resolves wheels
# for the other platform — but nothing about the result can be run, imported or smoke-tested on this
# machine, and this build has already shipped twice with a payload whose extensions did not load. An
# untestable artifact is worse than no artifact. Releases are arm64 only (installer/README.md); an
# Intel Mac can still build for itself, on itself.
if [ "$ARCH" != "$(uname -m)" ]; then
  echo "refusing to cross-build: --arch $ARCH on a $(uname -m) machine." >&2
  echo "The payload could be staged, but not run — so the smoke test that catches a broken" >&2
  echo "payload cannot run either. Build on a $ARCH machine." >&2
  exit 2
fi

mkdir -p "$CACHE"
echo "── runtime payload · $PBS_ARCH · extras=[$EXTRAS] ──"

# ── resolve + fetch the relocatable CPython ─────────────────────────────────────────────────────
# The GitHub API rate-limits by IP, and CI runners share theirs — an anonymous call from a hosted
# runner gets a 403 while the same call from a laptop is fine, so this is a failure that only ever
# appears in CI. Authenticate when a token is around (Actions always provides one), and say what
# happened rather than feeding an empty body to a JSON parser:
#     curl: (56) The requested URL returned error: 403
#     json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
API="https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
AUTH=(); TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")
# ${AUTH[@]+"${AUTH[@]}"}, not "${AUTH[@]}": macOS ships bash 3.2, where expanding an EMPTY array
# under `set -u` is an unbound-variable error. The empty case IS the no-token case — so the branch
# written to handle "no GITHUB_TOKEN" was the one that aborted, on every laptop, while CI (which
# always has a token) never saw it. The comment above predicted a failure that only shows up in CI;
# this one only showed up outside it, and announced itself as the 403 it was not.
RELEASE=$(curl -fsSL -m 60 ${AUTH[@]+"${AUTH[@]}"} -H "X-GitHub-Api-Version: 2022-11-28" "$API") || {
  echo "  cannot reach the python-build-standalone release API." >&2
  echo "  A 403 here is almost always the anonymous rate limit — set GITHUB_TOKEN and retry." >&2
  exit 1
}

URL=$(printf '%s' "$RELEASE" | python3 -c "
import sys, json
try:
    rel = json.load(sys.stdin)
except json.JSONDecodeError:
    sys.exit('the release API returned something that is not JSON (rate limited?)')
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
