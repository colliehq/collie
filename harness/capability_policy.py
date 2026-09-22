"""Request-scoped grants with live global revocation.

Settings may allow future requests. A running task must not acquire new desktop
or MCP authority merely because a different task enabled it. ToolCtx carries
the snapshot explicitly, including through tool-broker threads.
"""
KEYS = ("DESKTOP_CONTROL", "SCREEN_CAPTURE", "MCP_MANAGE", "MCP_DISCOVERY")


def _current(key):
    from . import settings
    return str(settings.get(key, "off")).lower() in ("1", "on", "true", "yes")


def snapshot():
    return {key: _current(key) for key in KEYS}


def freeze():
    return {"version": 1, "values": snapshot()}


def from_payload(payload):
    """Replay accepted grants; live global revocation still applies in allowed()."""
    if payload is None:
        return snapshot()
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise ValueError("unsupported saved capability policy")
    values = payload.get("values")
    if (not isinstance(values, dict) or set(values) != set(KEYS) or
            any(type(value) is not bool for value in values.values())):
        raise ValueError("incomplete or invalid saved capability policy")
    return dict(values)


def allowed(key, ctx=None):
    if key not in KEYS:
        return False
    current = _current(key)
    if ctx is None:
        return current
    policy = getattr(ctx, "capabilities", None)
    if policy is None:
        # Legacy embedders snapshot at their first call. Native runs snapshot
        # when ToolCtx is constructed, before their first model call.
        policy = snapshot()
        ctx.capabilities = policy
    return current and policy.get(key) is True


def grant(key, ctx):
    if key not in KEYS or ctx is None:
        raise ValueError("A capability grant needs the requesting tool context")
    if not _current(key):
        raise ValueError("The capability remains disabled by the effective settings or environment")
    policy = getattr(ctx, "capabilities", None)
    if policy is None:
        policy = snapshot()
        ctx.capabilities = policy
    policy[key] = True
