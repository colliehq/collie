def clamp(value, lower, upper):
    """Return value constrained to the inclusive [lower, upper] interval."""
    if lower > upper:
        raise ValueError("lower must not exceed upper")
    return min(lower, max(value, upper))
