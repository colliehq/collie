"""Live A/B: does the stepped elision boundary raise real prompt-cache hits on codex-oauth?

Replays one growing tool-reading session through Collie's own composer, turn by turn, and sends each
composed request to the ChatGPT Codex backend (the developer's everyday route). Arm "sliding" sets
context.ELIDE_STEP = 1 (the old per-message boundary); arm "stepped" uses the new value. Each arm has
its own provider session and a nonce at the top of its system prompt, so neither warms the other's
cache. The model's replies are ignored; only the usage it reports is recorded.

Refuses to run unless the access token has more than two hours left, so nothing refreshes or writes
~/.codex/auth.json. The token is decoded locally for its expiry only and never printed.
"""
import base64
import json
import os
import sys
import tempfile
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))       # repository root
sys.path.insert(0, ROOT)
state = tempfile.mkdtemp(prefix="collie-elide-ab-")
os.environ.update({"COLLIE_STATE_DIR": state, "COLLIE_SESSIONS_DIR": os.path.join(state, "s"),
                   "COLLIE_BROWSER_BRIDGE_NOSPAWN": "1", "COLLIE_BROWSER_BRIDGE": "0"})

from harness import codex_oauth, context  # noqa: E402
from harness.providers import ToolCall  # noqa: E402

doc = codex_oauth._load_auth()
access = ((doc.get("tokens") or {}).get("access_token") or "")
payload = access.split(".")[1] if access.count(".") == 2 else ""
exp = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)) or b"{}").get("exp", 0) \
    if payload else 0
left = exp - time.time()
if left < 7200:
    print("token has %.0f min left; not running (it would refresh and rewrite auth.json)" % (left / 60))
    sys.exit(2)
print("token life ok (%.1f h)" % (left / 3600))

MODEL = os.environ.get("AB_MODEL", "gpt-6-astra")
OUT = os.path.join(HERE, "results.jsonl")


def history(turns):
    msgs = [{"role": "user", "content": "Read the modules under src/ one at a time and tell me which "
                                        "one defines the retry policy. Read each before deciding."}]
    for i in range(turns):
        msgs.append({"role": "assistant", "tool_calls": [ToolCall("tc%d" % i, "read_file",
                                                                  {"path": "src/mod_%02d.py" % i})]})
        body = "".join("def helper_%d_%d(x):\n    return x * %d + %d  # module %d, helper %d\n"
                       % (i, j, j, i, i, j) for j in range(40))
        msgs.append({"role": "tool", "tool_call_id": "tc%d" % i, "name": "read_file",
                     "content": body})
    return msgs


from harness.cli import make_harness  # noqa: E402
h = make_harness(state, provider="mock", project="elide-ab", embed="hash")
schemas = [{"name": "read_file", "description": "Read a file from the repository.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}},
                             "required": ["path"]}}]
out = open(OUT, "w", encoding="utf-8")
summary = {}
for arm, step in (("stepped", context.ELIDE_STEP), ("sliding", 1)):
    context.ELIDE_STEP = step
    nonce = "Run %s %s." % (arm, uuid.uuid4().hex[:12])
    prov = codex_oauth.CodexOAuthProvider(model=MODEL, effort="low")
    tot_in = tot_cached = 0
    for k in range(6, 25):
        system, msgs, meta = h.composer.build({"messages": history(k)}, "next", state, "elide-ab")
        prov.cache_stable_upto = meta.elide_from
        t0 = time.time()
        comp = prov.complete(nonce + "\n\n" + system, msgs, schemas)
        u = comp.usage
        row = {"arm": arm, "turn": k, "elide_from": meta.elide_from, "input": u.input_tokens,
               "cached": getattr(u, "cache_read", 0), "ms": int((time.time() - t0) * 1000),
               "stop": comp.stop_reason, "error": (comp.text or "")[:120] if comp.stop_reason == "error" else ""}
        out.write(json.dumps(row) + "\n"); out.flush()
        print(row)
        if k >= 10:                       # past the warm-up, where elision is in play
            tot_in += u.input_tokens + getattr(u, "cache_read", 0)   # uncached + cached = sent
            tot_cached += getattr(u, "cache_read", 0)
    summary[arm] = {"sent": tot_in, "cached": tot_cached, "uncached": tot_in - tot_cached,
                    "cached_share": round(tot_cached / tot_in, 3) if tot_in else None}
print(json.dumps(summary, indent=1))
