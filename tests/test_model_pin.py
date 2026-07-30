"""A model picker that cannot win must say so, and `mock` must not be one of the choices.

Both come from the same afternoon. A `collie web` had been started with COLLIE_PROVIDER=mock (a
screenshot fixture server that outlived its purpose), and a phone paired to it. Every answer came
back canned, and every model the picker sent was accepted, written to settings.json, echoed back —
and ignored, because an env var set before the process started outranks the panel. Two things were
indistinguishable from a broken app: the replies, and the picker.

The rule itself is right — `COLLIE_PROVIDER=x collie web` must beat a saved setting. What was wrong
is that it happened silently, and that `mock` sat in the picker between real models where one tap
replaces every future answer with a fixture.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

fails = []


def check(ok, what):
    print(("  PASS " if ok else "  FAIL ") + what)
    if not ok:
        fails.append(what)


def _fresh(env):
    """Import settings/catalog with a chosen environment — _HARD_ENV is read at import time.

    sys.modules.pop alone is not enough: `from harness import settings` finds the attribute still
    bound on the already-imported package and hands back the OLD module, whose _HARD_ENV was frozen
    under a different environment. import_module after dropping both is what actually re-executes.
    """
    import harness
    import importlib
    old = {k: os.environ.get(k) for k in ("COLLIE_PROVIDER", "COLLIE_MODEL")}
    os.environ.pop("COLLIE_PROVIDER", None)
    os.environ.pop("COLLIE_MODEL", None)
    os.environ.update(env)
    for m in ("settings", "catalog"):
        sys.modules.pop("harness." + m, None)
        if hasattr(harness, m):
            delattr(harness, m)
    settings = importlib.import_module("harness.settings")
    catalog = importlib.import_module("harness.catalog")
    return settings, catalog, old


def _restore(old):
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def main():
    # 1. nothing pinned: the picker is in charge and says nothing about environments
    settings, catalog, old = _fresh({})
    check(not settings.pinned("PROVIDER"), "with no env var, PROVIDER is not pinned")
    ids = {e["id"] for e in catalog.list_entries()}
    check("mock:mock" not in ids, "mock is not offered in the catalog (%d entries)" % len(ids))
    check(any(i.startswith("anthropic-oauth:") for i in ids), "real providers are still listed")
    _restore(old)

    # 2. pinned to mock: the picker must admit it cannot change anything...
    settings, catalog, old = _fresh({"COLLIE_PROVIDER": "mock", "COLLIE_MODEL": "mock"})
    check(settings.pinned("PROVIDER") and settings.pinned("MODEL"),
          "a COLLIE_PROVIDER set before start is reported as pinned")
    check(settings.get("PROVIDER") == "mock", "and it is what get() returns")

    # ...and the machine must still be able to see what it is stuck on
    ids = {e["id"] for e in catalog.list_entries()}
    check("mock:mock" in ids, "a machine already on mock still sees the row it is on")
    row = [e for e in catalog.list_entries() if e["id"] == "mock:mock"][0]
    check("canned" in row["label"].lower(), "and the row calls it canned (%r)" % row["label"])
    _restore(old)

    # 3. a knob nobody pinned stays unpinned even while another one is
    settings, catalog, old = _fresh({"COLLIE_PROVIDER": "mock"})
    check(settings.pinned("PROVIDER") and not settings.pinned("MODEL"),
          "pinning is per key, not all-or-nothing")
    _restore(old)

    # 4. the wire shape the app reads
    settings, catalog, old = _fresh({"COLLIE_PROVIDER": "mock"})
    pin = [k for k in ("PROVIDER", "MODEL") if settings.pinned(k)]
    body = json.dumps({"ok": not pin, "pinned": pin})
    check(json.loads(body)["ok"] is False, "a selection under a pinned env reports ok=false")
    _restore(old)

    print("\n  " + ("%d FAILED" % len(fails) if fails else "model pin: all green"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
