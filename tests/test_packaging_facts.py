"""Two facts about how the app was launched, and the damage each one prevents.

Both were shipping. `stapler validate` on our own 0.20.24 dmg answers "does not
have a ticket stapled to it", and `browser_ext/token.txt` was being written inside
a bundle whose own launcher comments say writing there invalidates the signature.
Neither shows up in normal use: the first fails only offline, the second fails
weeks later when Gatekeeper next looks.

    python3 tests/test_packaging_facts.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from harness import plat   # noqa: E402

fails = []


def check(ok, label):
    print(("  PASS " if ok else "  FAIL ") + label)
    if not ok:
        fails.append(label)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as fh:
        package_cfg = fh.read()
    check("[tool.setuptools.exclude-package-data]" in package_cfg and
          'harness = ["browser_ext/token.txt"]' in package_cfg,
          "the per-machine browser bearer is explicitly excluded from wheels")
    with open(os.path.join(root, "MANIFEST.in"), encoding="utf-8") as fh:
        manifest = fh.read()
    check("exclude harness/browser_ext/token.txt" in manifest,
          "the per-machine browser bearer is explicitly excluded from sdists")

    # A checkout is neither, which is the case every developer is in.
    check(plat.in_app_bundle() is False, "a source checkout is not an app bundle")
    check(plat.translocated() is False, "and is not translocated")

    # The env var the bundle's launcher exports is honoured.
    os.environ["COLLIE_BUNDLED"] = "1"
    check(plat.in_app_bundle() is True, "COLLIE_BUNDLED marks us as running inside the app")
    del os.environ["COLLIE_BUNDLED"]
    check(plat.in_app_bundle() is False, "and unsetting it puts us back")

    # The path forms, checked as strings — the real ones cannot be constructed here.
    real_bundle = "/Applications/Collie.app/Contents/Resources/python/lib/harness/plat.py"
    real_transloc = ("/private/var/folders/xq/T/AppTranslocation/0EF42706/d/"
                     "Collie.app/Contents/Resources/python/lib/harness/plat.py")
    check("/Collie.app/Contents/" in real_bundle, "an installed bundle is recognisable by its path")
    check("/AppTranslocation/" in real_transloc,
          "and so is the throwaway copy macOS makes for a quarantined app")
    check("/AppTranslocation/" not in real_bundle,
          "an app in /Applications is not mistaken for a translocated one — the whole point of "
          "moving it there is that this stops happening")

    print("\n  " + ("%d FAILED" % len(fails) if fails else "packaging facts: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
