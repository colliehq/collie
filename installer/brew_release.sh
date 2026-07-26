#!/usr/bin/env bash
# Cut a Homebrew release of collie: build the sdist, publish it as a release asset on the tap, and
# rewrite the formula's url + sha256 to match.
#
#   bash installer/brew_release.sh            # dry run: build + rewrite the formula, publish nothing
#   bash installer/brew_release.sh --publish  # …and create the tag, release and asset via gh
#
# WHY the tarball lives on the tap rather than on the source repo: wudaming00/collie is private, so
# `brew install` cannot fetch from it and Homebrew will not prompt for credentials. The tap repo is
# public, and a release asset there is a plain public URL. If the source repo is ever made public,
# point `url` at its tag tarball and delete this indirection.
set -euo pipefail

cd "$(dirname "$0")/.."
TAP="${TAP_DIR:-$HOME/projects/homebrew-collie}"
PUBLISH=0; [ "${1:-}" = "--publish" ] && PUBLISH=1

# The default python3 here is a 3.8 framework build with no 'build' module, so probe for one that
# has it. The BUILDER's version does not matter — an sdist is source plus metadata, and the 3.10+
# floor is declared in the metadata and enforced by pip at install time, not at build time.
#
# The probe must run from a neutral cwd: this repo has a build/ directory, and `import build` here
# resolves to it as a namespace package — the check would pass and `-m build` would then fail. That
# is the same trap the builder itself is run from elsewhere to avoid.
PY=""
for c in "${PYTHON:-}" python3.13 python3.12 python3.11 python3 "$HOME/opt/anaconda3/bin/python"; do
  [ -n "$c" ] || continue
  (cd / && "$c" -m build --version) >/dev/null 2>&1 || continue
  PY="$c"; break
done
[ -n "$PY" ] || { echo "no python with the 'build' module (pip install build; or set PYTHON=)" >&2; exit 1; }
echo "== using $PY ($("$PY" -V 2>&1))"

VERSION=$("$PY" -c "import re;print(re.search(r'__version__ = \"(.*?)\"',open('harness/__init__.py').read()).group(1))")
TARBALL="collie_harness-$VERSION.tar.gz"
echo "== collie $VERSION"

# `python -m build` resolves 'build' from the cwd, and this repo HAS a build/ directory that shadows
# the module — so run the builder from somewhere else and point it back here.
rm -rf dist && (cd "$(mktemp -d)" && "$PY" -m build --sdist --outdir "$OLDPWD/dist" "$OLDPWD" >/dev/null)
[ -f "dist/$TARBALL" ] || { echo "no dist/$TARBALL" >&2; exit 1; }
SHA=$(shasum -a 256 "dist/$TARBALL" | cut -d' ' -f1)
echo "   $TARBALL  sha256 $SHA"

URL="https://github.com/wudaming00/homebrew-collie/releases/download/v$VERSION/$TARBALL"
F="$TAP/Formula/collie.rb"
[ -f "$F" ] || { echo "no formula at $F (set TAP_DIR)" >&2; exit 1; }
"$PY" - "$F" "$URL" "$SHA" <<'PY'
import re, sys
path, url, sha = sys.argv[1:4]
s = open(path).read()
s = re.sub(r'^  url ".*"$', '  url "%s"' % url, s, flags=re.M)
s = re.sub(r'^  sha256 ".*"$', '  sha256 "%s"' % sha, s, flags=re.M)
open(path, "w").write(s)
PY
echo "   formula updated: $F"

if [ "$PUBLISH" != "1" ]; then
  echo "== dry run. Re-run with --publish to tag, release and upload."
  exit 0
fi

command -v gh >/dev/null || { echo "gh not installed" >&2; exit 1; }
gh auth status >/dev/null 2>&1 || { echo "gh not authenticated: run 'gh auth login'" >&2; exit 1; }

gh repo view wudaming00/homebrew-collie >/dev/null 2>&1 || {
  echo "== creating the public tap repo"
  gh repo create wudaming00/homebrew-collie --public --source "$TAP" --push \
     --description "Homebrew tap for collie"
}
gh release view "v$VERSION" --repo wudaming00/homebrew-collie >/dev/null 2>&1 \
  && gh release upload "v$VERSION" "dist/$TARBALL" --repo wudaming00/homebrew-collie --clobber \
  || gh release create "v$VERSION" "dist/$TARBALL" --repo wudaming00/homebrew-collie \
       --title "collie $VERSION" --notes "collie $VERSION"

git -C "$TAP" add -A && git -C "$TAP" commit -q -m "collie $VERSION" && git -C "$TAP" push -q
echo "== published. Install with:  brew install wudaming00/collie/collie"
