#!/usr/bin/env bash
# Point the Homebrew formula at a published collie release and push the tap.
#
#   bash installer/brew_release.sh              # latest release; rewrite the formula, publish nothing
#   bash installer/brew_release.sh v0.20.0      # a specific tag
#   bash installer/brew_release.sh v0.20.0 --publish     # …and commit + push the tap
#
# It deliberately does NOT build a tarball. The release workflow already publishes
# collie_harness-<ver>.tar.gz on every tag, and the formula has to name the artifact users actually
# download. Building a second sdist here would hash something nobody else has — sdists are not
# byte-reproducible, so the local one and the published one differ, and `brew install` would fail the
# checksum on a file that is otherwise perfectly fine.
set -euo pipefail

cd "$(dirname "$0")/.."
TAP="${TAP_DIR:-$HOME/projects/homebrew-collie}"
REPO="${COLLIE_REPO:-colliehq/collie}"

PUBLISH=0; TAG=""
for a in "$@"; do
  case "$a" in
    --publish) PUBLISH=1 ;;
    v*)        TAG="$a" ;;
    *)         echo "unknown argument: $a" >&2; exit 2 ;;
  esac
done

if [ -z "$TAG" ]; then
  TAG=$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | python3 -c "import json,sys;print(json.load(sys.stdin)['tag_name'])") \
    || { echo "could not read the latest release of $REPO" >&2; exit 1; }
fi

VERSION="${TAG#v}"
TARBALL="collie_harness-$VERSION.tar.gz"
URL="https://github.com/$REPO/releases/download/$TAG/$TARBALL"
echo "== collie $VERSION  ($REPO $TAG)"

# Hash exactly what a user will download, by downloading it.
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
curl -fsSL -o "$TMP/$TARBALL" "$URL" || { echo "no such release asset: $URL" >&2; exit 1; }
SHA=$(shasum -a 256 "$TMP/$TARBALL" | cut -d' ' -f1)
echo "   $TARBALL  $(du -h "$TMP/$TARBALL" | cut -f1)  sha256 $SHA"

F="$TAP/Formula/collie.rb"
[ -f "$F" ] || { echo "no formula at $F (set TAP_DIR)" >&2; exit 1; }
python3 - "$F" "$URL" "$SHA" <<'PY'
import re, sys
path, url, sha = sys.argv[1:4]
s = open(path).read()
s = re.sub(r'^  url ".*"$', '  url "%s"' % url, s, flags=re.M)
s = re.sub(r'^  sha256 ".*"$', '  sha256 "%s"' % sha, s, flags=re.M)
open(path, "w").write(s)
PY
echo "   formula updated: $F"

if [ "$PUBLISH" != "1" ]; then
  echo "== dry run. Re-run with --publish to commit and push the tap."
  echo "   test it first, without publishing anything:"
  echo "     brew tap-new wudaming00/collie --no-git"
  echo "     cp $F \"\$(brew --repository)/Library/Taps/wudaming00/homebrew-collie/Formula/\""
  echo "     brew install --build-from-source wudaming00/collie/collie && brew test wudaming00/collie/collie"
  exit 0
fi

command -v gh >/dev/null || { echo "gh not installed" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh not authenticated: run 'gh auth login'" >&2; exit 1; }

gh repo view wudaming00/homebrew-collie >/dev/null 2>&1 || {
  echo "== creating the public tap repo"
  gh repo create wudaming00/homebrew-collie --public --source "$TAP" --push \
     --description "Homebrew tap for collie"
}
git -C "$TAP" add -A
git -C "$TAP" diff --cached --quiet && { echo "== formula already at $VERSION, nothing to push."; exit 0; }
git -C "$TAP" commit -q -m "collie $VERSION"
git -C "$TAP" push -q
echo "== published. Install with:  brew install wudaming00/collie/collie"
