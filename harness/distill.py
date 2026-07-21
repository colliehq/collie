"""Extraction/distillation for memory writes — the Mem0/A-MEM lesson made concrete.

collie's LOCOMO end-to-end gap (42% vs Mem0 ~67%) traced to storing RAW conversation turns
instead of distilled facts. A `distiller` turns noisy raw text into one clean atomic fact
(or None to drop chit-chat) before it's embedded and stored, so recall surfaces facts, not
transcript. The memory layer stays LLM-free by default; this is opt-in and pluggable.

    from harness.distill import make_distiller
    mem = SqliteMemory(db, embedder=..., distiller=make_distiller("deepseek"))
"""
import json
import os
import time
import urllib.request

_ENDPOINTS = {  # name -> (base_url, api_key_env, model)
    "deepseek": ("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", "deepseek-chat"),
    "qwen": ("https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY",
             "qwen2.5-7b-instruct"),
}

_SYS = ("Extract the single durable, memorable fact from the message as ONE concise "
        "third-person sentence — include who, what, and any date/number/place. Resolve "
        "pronouns to names when obvious. If the message is only greeting, filler, or "
        "chit-chat with no fact worth remembering, reply with exactly: NONE")


def _chat(base, key, model, system, user, max_tokens=80):
    body = json.dumps({"model": model, "temperature": 0.0, "max_tokens": max_tokens,
                       "messages": [{"role": "system", "content": system},
                                    {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
                                 headers={"content-type": "application/json",
                                          "authorization": "Bearer " + key})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())["choices"][0]["message"]["content"].strip()
        except Exception:
            if attempt == 2:
                return ""
            time.sleep(2 ** attempt)


class LLMDistiller:
    def __init__(self, base, key, model):
        self.base, self.key, self.model = base, key, model
        self.name = "distill:" + model

    def __call__(self, text: str, keys: str = "") -> str | None:
        out = _chat(self.base, self.key, self.model, _SYS, text)
        out = (out or "").strip().strip('"')
        # only an EXACT "NONE" is the drop sentinel — not "None of the tests passed…" (starts with
        # NONE) and not a valid short fact (an id/name under 4 chars), both of which were dropped.
        if not out or out.strip().upper() == "NONE":
            return None
        return out


def make_distiller(name):
    if not name or name in ("none", "off", "0", "", "false"):
        return None
    if name in _ENDPOINTS:
        base, env, model = _ENDPOINTS[name]
        key = os.environ.get(env, "")
        if not key:
            raise RuntimeError("%s not set (needed for distiller %s)" % (env, name))
        return LLMDistiller(base, key, model)
    raise ValueError("unknown distiller: %s" % name)


# --- CHUNK-level extraction (the actual Mem0 design; per-turn distillation HURT LOCOMO) --
# See a whole window of turns at once, so cross-turn context and detail survive, and emit a
# SET of atomic facts. This is the follow-up to the honest per-turn negative.
_CHUNK_SYS = (
    "From this conversation excerpt, extract EVERY durable fact worth remembering as a JSON "
    "array of concise third-person sentences. Each: who did/said what, with dates, numbers, "
    "and places, and enough detail to answer later questions. Resolve pronouns to names. "
    "Return ONLY a JSON array of strings; return [] if nothing is memorable.")


class ChunkExtractor:
    def __init__(self, base, key, model):
        self.base, self.key, self.model = base, key, model
        self.name = "chunk:" + model

    def __call__(self, chunk_text: str) -> list:
        out = _chat(self.base, self.key, self.model, _CHUNK_SYS, chunk_text, max_tokens=700)
        try:
            arr = json.loads(out[out.find("["): out.rfind("]") + 1])
            return [str(x).strip() for x in arr if str(x).strip()]
        except Exception:
            # model didn't emit clean JSON -> store NOTHING. Line-splitting the prose stored the
            # model's preamble ("Here are the durable facts:") as a fake memory, corrupting recall.
            return []


def make_chunk_extractor(name):
    if not name or name in ("none", "off", "0", ""):
        return None
    if name in _ENDPOINTS:
        base, env, model = _ENDPOINTS[name]
        key = os.environ.get(env, "")
        if not key:
            raise RuntimeError("%s not set" % env)
        return ChunkExtractor(base, key, model)
    raise ValueError("unknown chunk extractor: %s" % name)
