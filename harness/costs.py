"""Rough $ cost model — turns the token/prefix gap into money.

Prices are USD per 1M tokens: (input, cached_input, output). Local models / mock
are $0. Approximate, provider list-price as of 2026; adjust as needed.
"""
PRICES = {
    "deepseek-reasoner": (0.55, 0.14, 2.19),   # before "deepseek" so the longer id wins the substring match
    "deepseek-chat": (0.27, 0.07, 1.10),
    "deepseek":      (0.27, 0.07, 1.10),
    "sonnet":        (3.00, 0.30, 15.00),
    "haiku":         (0.80, 0.08, 4.00),
    "claude-opus-4-8": (5.0, 0.50, 25.00),
    "opus":          (15.0, 1.50, 75.00),
    "gpt-5.6-sol":   (5.0, 0.5, 30.0),         # GPT-5.6 tiers (ChatGPT Codex sub bills at equiv metered rate)
    "gpt-5.6-terra": (2.5, 0.25, 15.0),
    "gpt-5.6-luna":  (1.0, 0.10, 6.0),
    "gpt-4o-mini":   (0.15, 0.075, 0.60),
    "gemini-2.5-pro":   (1.25, 0.31, 10.0),
    "gemini-2.5-flash": (0.30, 0.075, 2.50),
    "llama-3.3-70b": (0.59, 0.0, 0.79),
    "glm-4-flash":   (0.0, 0.0, 0.0),
}


_LOCAL_FREE = ("mock", "local", "hash", "glm-4-flash")   # legitimately $0
_PRICE_WARNED = set()


def price_for(model: str):
    m = (model or "").split(":")[-1].lower()          # "deepseek:deepseek-chat" -> "deepseek-chat"
    # Prefer a model-specific price over a family fallback regardless of dict/import order.
    # Catalog-discovered prices are registered after this module is imported, so relying on
    # insertion order made e.g. claude-opus-4-8 silently hit the older generic "opus" rate.
    if m in PRICES:
        return PRICES[m]
    match = max((k for k in PRICES if k in m), key=len, default=None)
    if match is not None:
        return PRICES[match]
    if not any(f in m for f in _LOCAL_FREE) and m not in _PRICE_WARNED:
        # an unlisted PAID model silently priced at $0 makes that harness look free in the $/instance
        # comparison — warn once so the misprice is visible instead of silently biasing the result.
        _PRICE_WARNED.add(m)
        import sys
        print("WARN(costs): no price for model %r — billing $0 (add it to PRICES or the $/instance "
              "comparison will understate its cost)" % (model or ""), file=sys.stderr)
    return (0.0, 0.0, 0.0)                              # local / unknown -> $0


NOISE_FLOOR_TOKENS = 1024   # cache-breakpoint granularity noise floor (pi-verified) — misses below
                            # this are ordinary eviction/boundary jitter, not a regression signal.
CACHE_TTL_S = 300           # Anthropic default cache TTL; a long tool run between turns can evict it.


def cache_miss(prev_prompt: int, usage, model: str, reported_cache: bool):
    """Tokens from the previous turn's prompt that were re-billed instead of cache-read this turn,
    and the $ that waste cost. Returns (missed_tokens, missed_usd); (0, 0.0) when nothing countable.

    The prefix SHOULD carry from turn to turn (same system + schemas + older messages). What should
    have been cache_read but wasn't is `min(prev_prompt, this_prompt) - cache_read` above the noise
    floor. Priced at the paid-rate minus the cached-rate (what caching WOULD have saved). A
    detect-miss + NOISE_FLOOR tripwire; never an auto-fail."""
    prompt = usage.input_tokens + usage.cache_read + usage.cache_creation
    if prev_prompt <= 0 or prompt <= 0:
        return 0, 0.0
    if usage.cache_read + usage.cache_creation == 0 and not reported_cache:
        return 0, 0.0                     # provider never reports caching (e.g. ollama) — uncountable
    missed = min(prev_prompt, prompt) - usage.cache_read
    if missed <= NOISE_FLOOR_TOKENS:
        return 0, 0.0
    pin, pcached, _ = price_for(model)
    paid = usage.input_tokens + usage.cache_creation
    paid_rate = ((usage.input_tokens * pin + usage.cache_creation * pin * 1.25) / paid) if paid else pin
    return missed, missed * max(0.0, paid_rate - pcached) / 1e6


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0,
             cache_creation: int = 0) -> float:
    # input_tokens is UNCACHED (both providers normalize to the Anthropic convention),
    # so bill it in full and price cache_read separately — do NOT subtract again.
    # cache_creation (Anthropic cache WRITE) bills at ~1.25x the input rate; DeepSeek reports its
    # writes as ordinary input_tokens so cache_creation is ~0 there — the term is correct either way.
    pin, pcached, pout = price_for(model)
    return ((input_tokens or 0) * pin + (cache_read or 0) * pcached
            + (cache_creation or 0) * pin * 1.25
            + (output_tokens or 0) * pout) / 1e6
