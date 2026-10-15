"""Compatibility entrypoint for the single Collie runtime and task service."""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import sys

from . import __version__


def component_status():
    """Keep the legacy diagnostic command without inventing split packages."""
    try:
        version = importlib.metadata.version("collie-harness")
    except importlib.metadata.PackageNotFoundError:
        version = None
    return [{"distribution": "collie-harness", "installed": version is not None,
             "version": version, "source_version": __version__,
             "functional_verification": "not_run"}]


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "components":
        print(json.dumps({"components": component_status()}, ensure_ascii=False, indent=2))
        return 0
    if args[:2] == ["task", "serve"]:
        from .evidence_bridge import main as serve
        return serve(args[2:])
    # All application modules belong to this runtime. Missing modules or vendor
    # dependencies are installation errors, never advice to install old splits.
    application = importlib.import_module("harness.cli")
    saved = sys.argv
    try:
        sys.argv = [saved[0], *args]
        return application.main()
    finally:
        sys.argv = saved


if __name__ == "__main__":
    raise SystemExit(main())
