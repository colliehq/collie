"""Tool contracts shared by the kernel and optional capability adapters.

Concrete tools and their registration live in harness.tools and other components.
Only host-minted ExecReceipt/ToolResult objects establish execution evidence.
"""
from __future__ import annotations
import json
import sys
from dataclasses import dataclass, field
from . import capability_policy

_ARG_ALIASES = {"path": ("file_path",)}

def repair_args(args, schema):
    """Fix known model quirks in tool arguments BEFORE validation, returning (repaired, notes).
    pi's prepareArguments generalized (edit.ts:100-117). Repairs, each noted for the transcript:
      • JSON-string-wrapped array/object args (Opus 4.6 / GLM-5.1 send nested arrays as strings)
      • required-key aliases (file_path -> path)
    A non-dict becomes {} (note 'non_dict'). Never overwrites an existing key; a string that isn't
    valid JSON, or whose parsed type doesn't match the declared type, is left for the tool's own
    error. Well-formed args return unchanged (identity) with no notes — no over-repair churn."""
    if not isinstance(args, dict):
        return {}, ["non_dict"]
    notes = []
    props = (schema or {}).get("properties", {}) or {}
    out = dict(args)
    for key, spec in props.items():
        want = spec.get("type") if isinstance(spec, dict) else None
        if want in ("array", "object") and isinstance(out.get(key), str):
            try:
                parsed = json.loads(out[key])
            except Exception:
                continue                         # leave it — the tool's own error still fires
            if (want == "array" and isinstance(parsed, list)) or \
               (want == "object" and isinstance(parsed, dict)):
                out[key] = parsed
                notes.append("json_str:%s" % key)
    for req in (schema or {}).get("required", []) or []:
        if req in out and out[req] not in (None, ""):
            continue
        for alias in _ARG_ALIASES.get(req, ()):  # fill a missing required key from a known alias
            if alias in out and alias not in props and out.get(alias) not in (None, ""):
                out[req] = out.pop(alias)
                notes.append("alias:%s->%s" % (alias, req))
                break
    return (out, notes) if notes else (args, [])

def _need_str(args, key):
    """Validate a required string arg, returning (value, None) or (None, 'ERROR: …'). Tools should
    return a clean ERROR the model can act on, not KeyError/TypeError (which relies on the caller's
    try/except and surfaces as an opaque 'tool X failed: KeyError')."""
    v = args.get(key) if isinstance(args, dict) else None
    if v is None or v == "":
        return None, "ERROR: missing required arg '%s'" % key
    if not isinstance(v, str):
        return None, "ERROR: arg '%s' must be a string, got %s" % (key, type(v).__name__)
    return v, None

@dataclass
class ToolCtx:
    cwd: str
    project: str
    memory: object          # SqliteMemory
    recorder: object = None
    # Images a tool wants the model to actually SEE. A tool's run() still returns a plain string —
    # so redaction, the result preview and history elision keep working unchanged — and anything it
    # appends here is drained by the loop into a real image block on the conversation. Providers
    # already know how to reshape those per API (Anthropic source / OpenAI image_url / Ollama
    # images), so this is the whole seam: `screenshot` is the first user.
    images: list = field(default_factory=list)
    # The live ToolRegistry, so a tool that CHANGES what tools exist can make the change take effect
    # in this session instead of asking for a restart — `mcpctl_add` registers the new server's tools
    # straight away, the same way enable_capability makes a gated capability usable immediately.
    registry: object = None
    # Undo is more narrowly scoped than project memory. Web runs set this to their session id so
    # two chats in the same repository cannot consume each other's journal.
    checkpoint_scope: str = ""
    # The provider-authored call id currently crossing the execution boundary. Cloud-routed
    # tools derive their idempotency key from it, so a resumed/replayed turn cannot perform the
    # same external action twice. It is set only around Tool.run() and is never model-controlled.
    tool_call_id: str = ""
    # Host-only callback. Child investigation prompts cannot create another
    # execution context or obtain the parent's credentials through tool args.
    delegate_runner: object = None
    # Host-only cooperative cancellation: () -> bool, True once the surface's Stop has been
    # pressed. Set by the loop from the embedding surface, never by a model argument — a tool
    # can ask whether to stop, it can never decide that someone asked. Tools that own a
    # subprocess poll it while waiting, so Stop ends the command instead of leaving it to run
    # out its timeout. Default None (and read via getattr) so every simpler ToolCtx-shaped
    # context — embedders, the small test doubles — keeps working unchanged.
    cancelled: object = None
    # Host-only, tool-SET (the only field here that flows outward): True once a tool has done
    # something whose extent it cannot account for — an executed command whose process tree
    # could not be proved stopped, or one whose deliberate background survivors could not be
    # handed over cleanly. The tool's own text says so too, but text is only advice to the
    # model; the loop needs a fact to fence the turn with, so that a run cannot be finalized
    # as cleanly finished while a command it started may still be writing files. Never
    # model-controlled: no tool argument can set or clear it.
    tool_effect_uncertain: bool = False
    capabilities: dict = field(default_factory=capability_policy.snapshot)

@dataclass(frozen=True)
class ExecReceipt:
    """Host-minted, immutable record of what a tool ACTUALLY executed.

    The exit codes here are read off ``subprocess.CompletedProcess`` inside the tool — they are
    never parsed back out of the text the executed command printed. That distinction is the whole
    point: ``run_in_env`` frames its two runs with ``--- ORIGINAL code [exit N] ---`` /
    ``--- WITH YOUR EDITS [exit N] ---`` headers, and a command can print those exact bytes to its
    own stdout, so a gate that reads the frame from the result text can be told any verdict the
    command likes. It binds to ONE call (``tool`` + the provider's ``call_id`` + the exact
    ``command`` string that was run) so a receipt cannot be replayed for a different call.

    Frozen, and only tools construct it: no model argument and no caller-supplied dict can become
    execution evidence.
    """
    tool: str
    call_id: str
    command: str
    base_rc: int | None = None      # the run WITHOUT the model's edits, when one happened
    edit_rc: int | None = None      # the run WITH the model's edits applied
    dual: bool = False              # both halves above really executed, in that order

class ToolResult(str):
    """A tool's ordinary result string carrying one ``ExecReceipt``.

    A ``str`` subclass because every consumer downstream — redaction, the transcript, hooks, the
    result preview, JSON serialization — must keep seeing exactly the text the tool returned. The
    Harness lifts the receipt off at the execution boundary (before redaction, which returns plain
    ``str``) and hands it to accounting by value.
    """
    __slots__ = ("_receipt",)

    def __new__(cls, text: str, receipt: ExecReceipt):
        if not isinstance(receipt, ExecReceipt):
            raise TypeError("ToolResult requires an ExecReceipt, got %s" % type(receipt).__name__)
        obj = super().__new__(cls, text)
        obj._receipt = receipt
        return obj

    @property
    def receipt(self) -> ExecReceipt:
        return self._receipt

def exec_receipt(out) -> ExecReceipt | None:
    """The execution receipt carried by a tool result, or None.

    Deliberately narrow: only a real ``ExecReceipt`` on a real ``ToolResult`` counts, so an
    arbitrary object with a ``.receipt`` attribute (or a dict a third-party tool returns) is not
    evidence.
    """
    if isinstance(out, ToolResult):
        r = out.receipt
        return r if isinstance(r, ExecReceipt) else None
    return None

class Tool:
    name = ""
    description = ""
    tier = "always"         # "always" | "deferred"
    schema: dict = {}       # JSON schema of args (Anthropic tool input_schema)

    def run(self, args: dict, ctx: ToolCtx) -> str:
        raise NotImplementedError

    def provider_schema(self) -> dict:
        return {"name": self.name, "description": self.description,
                "input_schema": self.schema or {"type": "object", "properties": {}}}

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}
        self._activated: set[str] = set()   # deferred tools promoted to active this session

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def always_on(self) -> list[Tool]:
        return [t for t in self._tools.values() if t.tier == "always"]

    def deferred_names(self) -> list[str]:
        # ALL deferred tool names, SORTED — byte-stable across the whole session. Point-12 stage A:
        # previously this dropped activated tools from the advert (to avoid a dup mention), but that
        # shrank the STABLE prompt section on every load_tools call and busted the cached prefix —
        # the exact lever collie's context ordering exists to protect. A re-advertised active tool is
        # a cheap no-op (a load_tools re-call just re-resolves it), so stability wins over de-duping.
        return sorted(t.name for t in self._tools.values() if t.tier == "deferred")

    def activate(self, names) -> list[str]:
        """Promote named deferred tools to the active set (their schema then rides the next turn).
        Returns the names that actually resolved to a known deferred tool."""
        ok = []
        for n in (names or []):
            t = self._tools.get(n)
            if t is not None and t.tier == "deferred":
                self._activated.add(n); ok.append(n)
        return ok

    def active_schemas(self) -> list[dict]:
        out = [t.provider_schema() for t in self.always_on()]
        for n in self._activated:               # + any deferred tool the model has loaded
            t = self._tools.get(n)
            if t is not None:
                out.append(t.provider_schema())
        return out

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def retain(self, names) -> list[str]:
        """Keep only an explicit tool subset and return the names that survived.

        Evaluation presets use this to expose the same narrow contract to every coding arm.  It
        is deliberately opt-in: normal Collie sessions keep the full registry.  Activated state is
        intersected too, so a previously loaded deferred tool cannot leak back into the contract.
        """
        wanted = {str(name) for name in (names or [])}
        self._tools = {name: tool for name, tool in self._tools.items() if name in wanted}
        self._activated.intersection_update(self._tools)
        return list(self._tools)


_REPLAY_SAFE_BUILTINS = (
    ("harness.tools", "ReadFileTool"),
    ("harness.tools", "GlobTool"),
    ("harness.tools", "GrepTool"),
    ("harness.tools", "MemorySearchTool"),
    ("harness.delegate", "DelegateTool"),
)


def replay_safe_builtin(tool) -> bool:
    """Attest the exact built-in implementation without loading a capability.

    A constructed built-in already has its real module loaded. Looking up that
    module's actual class preserves the former exact-type check, including the
    refusal of subclasses and tools which merely copy a built-in's name.
    """
    implementation = type(tool)
    return any(implementation is getattr(sys.modules.get(module), name, None)
               for module, name in _REPLAY_SAFE_BUILTINS)
