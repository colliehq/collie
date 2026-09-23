"""Device-local channel credentials, kept out of public connection metadata.

Like the existing local account token store, this is a private file protected by
the user's OS permissions, not a claim of encryption against device compromise.
Callers return only presence flags to the UI and never pass credentials to agents.
"""
from __future__ import annotations

import json
import os
import re

from . import plat, sessions
from .controlplane import state_dir as active_state_dir

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}\Z")
_FIELDS = frozenset({"password", "auth_token", "api_key", "username", "account_sid"})
_MAX_FILE = 256 * 1024


def _path(state_dir=None):
    return os.path.join(active_state_dir(state_dir), "channel-credentials.json")


def _id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("invalid channel connection id")
    return value


def _load(path):
    try:
        with open(path, encoding="utf-8") as stream:
            raw = stream.read(_MAX_FILE + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > _MAX_FILE:
        raise ValueError("channel credentials store exceeds its size limit")
    try:
        value = json.loads(raw)
    except ValueError:
        raise ValueError("channel credentials store needs repair") from None
    if not isinstance(value, dict) or any(not isinstance(row, dict) for row in value.values()):
        raise ValueError("channel credentials store needs repair")
    return value


def get(connection_id, *, state_dir=None):
    """For the provider adapter only; never expose this dictionary in an API."""
    return dict(_load(_path(state_dir)).get(_id(connection_id)) or {})


def present(connection_id, *, state_dir=None):
    return sorted(key for key, value in get(connection_id, state_dir=state_dir).items() if value)


def put(connection_id, credentials, *, state_dir=None):
    """Replace one connection's credentials, preserving all other connections."""
    connection_id = _id(connection_id)
    if not isinstance(credentials, dict) or not credentials or set(credentials) - _FIELDS:
        raise ValueError("unsupported channel credential fields")
    for key, value in credentials.items():
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise ValueError("invalid channel credential value")
    path = _path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sessions._locked(path):
        data = _load(path)
        if connection_id not in data and len(data) >= 32:
            raise ValueError("too many saved channel credentials")
        data[connection_id] = dict(credentials)
        if len(json.dumps(data).encode("utf-8")) > _MAX_FILE:
            raise ValueError("channel credentials store exceeds its size limit")
        sessions._atomic_dump(data, path)
        plat.chmod_private(path)
    return present(connection_id, state_dir=state_dir)


def delete(connection_id, *, state_dir=None):
    connection_id = _id(connection_id)
    path = _path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with sessions._locked(path):
        data = _load(path)
        data.pop(connection_id, None)
        sessions._atomic_dump(data, path)
        plat.chmod_private(path)
