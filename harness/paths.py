"""Core runtime path resolution, independent of UI and provider construction.

Resolving a path never relocates legacy data. The CLI can keep its explicit
compatibility migration without making journal recovery import the whole app.
"""
from __future__ import annotations
import os
import importlib.util
import threading
from typing import Callable


def source_root(module_file: str | None = None) -> str | None:
    """Recognize a source checkout without discovering or moving user data.

    The runtime lives in one ``harness`` package beside the project's metadata.
    Installed modules do not inherit an arbitrary ancestor's project. No build
    manifest, optional adapter or user state is opened to resolve this path.
    """
    package = os.path.dirname(os.path.abspath(module_file or __file__))
    parent = os.path.dirname(package)
    if os.path.basename(package) != "harness":
        return None
    if os.path.isfile(os.path.join(parent, "pyproject.toml")):
        return parent
    return None


def component_resource_dir(module_name: str, directory: str) -> str:
    """Locate an asset beside its owning module without importing that adapter.

    Use the owning module's source location rather than a caller's directory.
    The compatibility name also works with an embedded host's explicit module
    path; finding a resource never executes the owning adapter.
    """
    if (not isinstance(module_name, str) or not module_name.startswith("harness.")
            or not module_name[len("harness."):].isidentifier()
            or not isinstance(directory, str) or not directory
            or directory in (".", "..") or any(mark in directory for mark in ("/", "\\", "\x00"))):
        raise ValueError("use a named harness owner and one asset directory")
    try:
        spec = importlib.util.find_spec(module_name)
    except (ImportError, ValueError) as exc:
        raise FileNotFoundError("the asset owner is unavailable: " + module_name) from exc
    if spec is None or not spec.origin or spec.origin in ("built-in", "frozen"):
        raise FileNotFoundError("the asset owner is not installed: " + module_name)
    target = os.path.join(os.path.dirname(os.path.abspath(spec.origin)), directory)
    if not os.path.isdir(target):
        raise FileNotFoundError("the owning module's asset directory is missing")
    return target


ROOT = source_root() or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_lock = threading.RLock()
_data_root_resolver: Callable[[], str] | None = None


def set_data_root_resolver(resolver: Callable[[], str] | None):
    """Bind the application's current data root without importing its frontend.

    Returns the previous resolver so an embedded host can restore its binding.
    Explicit directory arguments remain independent of this process-wide default.
    """
    if resolver is not None and not callable(resolver):
        raise ValueError("the data-root resolver must be callable or None")
    global _data_root_resolver
    with _lock:
        previous = _data_root_resolver
        _data_root_resolver = resolver
    return previous


def data_dir(root: str | None = None) -> str:
    """Use the existing override/checkout/default precedence, without migration."""
    if root is None:
        with _lock:
            resolver = _data_root_resolver
        if resolver is not None:
            resolved = os.fspath(resolver())
            if not resolved:
                raise ValueError("the application's data root is empty")
            return resolved
    override = os.environ.get("COLLIE_DATA_DIR")
    if override:
        return override
    state = os.environ.get("COLLIE_STATE_DIR")
    if state:
        return os.path.join(state, "data")
    source = ROOT if root is None else os.fspath(root)
    if os.path.exists(os.path.join(source, "pyproject.toml")):
        return os.path.join(source, "data")
    return os.path.join(os.path.expanduser("~/.collie"), "data")


DATA = data_dir()


def runtime_paths(data: str | None = None, *, create: bool = True) -> tuple[str, str, str, str]:
    target = data_dir() if data is None else os.fspath(data)
    if create:
        os.makedirs(target, exist_ok=True)
    return tuple(os.path.join(target, name) for name in
                 ("memory.db", "runs.db", "dashboard.html", "sandbox"))


def sessions_dir(directory: str | None = None, *, create: bool = True) -> str:
    target = directory or os.environ.get("COLLIE_SESSIONS_DIR")
    if not target:
        target = os.path.join(data_dir(), "sessions")
    if create:
        os.makedirs(target, exist_ok=True)
    return target
