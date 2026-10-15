"""Trusted application assembly callbacks consumed by the execution kernel.

The kernel never imports a CLI or HTTP frontend to find an executor. Installed
frontends bind callbacks explicitly. Missing bindings are unavailable services,
not permission to invent an executor or replay a pending action.
"""
from __future__ import annotations

import threading
from typing import Callable


class ServiceUnavailable(RuntimeError):
    pass


_lock = threading.RLock()
_bindings: dict[str, Callable] = {}


def register(name: str, resolver: Callable) -> None:
    if not isinstance(name, str) or not name or not callable(resolver):
        raise ValueError("a service binding needs a name and a callable resolver")
    with _lock:
        _bindings[name] = resolver


def unregister(name: str) -> None:
    with _lock:
        _bindings.pop(name, None)


def resolve(name: str):
    with _lock:
        resolver = _bindings.get(name)
    if resolver is None:
        raise ServiceUnavailable("application service %r is not installed or configured" % name)
    return resolver()


def web_surface():
    return resolve("web_surface")


def cli_factory(name: str):
    """Compatibility name for an explicitly registered application factory."""
    return resolve("factory:" + name)
