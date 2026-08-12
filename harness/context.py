"""Context composition — the pain-#2 / pain-#4 machinery.

The system prompt is assembled from three cache-ordered tiers:

  STABLE   identity + language/grounding rules + mode role
           + tool NAMES + skill manifest                          (rarely changes)
  CONTEXT  merged project rules (CLAUDE.md / AGENTS.md), char-capped
  VOLATILE core memory blocks + AUTO-PREFETCHED memory + timestamp  (LAST)

Volatile goes last so per-turn churn never invalidates the cached prefix above it.

AUTO-PREFETCH is the key move the internalized embedding unlocks: every turn we
run a cheap local hybrid recall on the user's message and inject the top hits into
VOLATILE — so the model never has to *decide* to search (that decision is why
Claude Code / echomem sit at ~1% recall activation). Retrieval is also still
available as an explicit tool for deeper digs.

TokenBudgeter reports per-section cost (like `/context detail`) and enforces a
fixed-prefix ceiling.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field

from .providers import content_text, est_tokens


# Reply-language display names, keyed by the LANG setting's option values (settings.py SCHEMA).
_LANG_NAMES = {
    "en": "English", "zh": "简体中文 (Simplified Chinese)",
    "zh-tw": "繁體中文 (Traditional Chinese)", "ja": "日本語 (Japanese)",
    "ko": "한국어 (Korean)", "es": "Español (Spanish)", "fr": "Français (French)",
    "de": "Deutsch (German)", "pt": "Português (Portuguese)", "ru": "Русский (Russian)",
}


def _response_language_line() -> str:
    """RESPONSE LANGUAGE directive for the STABLE tier. Policy (highest priority first):
      1. If the user has asked — anywhere in this conversation — to reply in a particular language,
         honour that for the rest of the conversation.
      2. Otherwise, reply in the SAME language as the user's most recent message (following the
         user's input is the desired default).
      3. When the user's language is ambiguous (a very short message, or Han characters that could
         be Chinese OR Japanese — the misfire that answered "打开collie dashboard" in Japanese),
         default to the install/UI language (the LANG setting, chosen in the installer). LANG=auto
         has no fixed install language, so the tiebreaker is the language used earlier instead.
    Byte-stable per session (LANG doesn't change mid-run), so it stays inside the cached prefix."""
    try:
        from . import settings
        lang = (settings.get("LANG", "auto") or "auto").lower()
    except Exception:
        lang = "auto"
    name = _LANG_NAMES.get(lang)
    tiebreak = ("default to %s (the language Collie was set up in)" % name) if name else \
               "default to the language the user has been using earlier in this conversation"
    return ("RESPONSE LANGUAGE: Reply in the SAME language as the user's most recent message. If "
            "the user has asked — anywhere in this conversation — to reply in a particular "
            "language, honour that for the rest of the conversation. IMPORTANT: Han/CJK characters "
            "ALONE are shared between Chinese and Japanese and are NOT evidence of Japanese — a "
            "message written only in Han characters with NO kana (e.g. 下一首) is "
            "Chinese; treat it as Chinese. Only kana (ひらがな / カタカナ) "
            "or an explicit request means Japanese — never default to Japanese from Han characters "
            "alone. When the user's language is still genuinely ambiguous (a very short non-CJK "
            "message), %s." % tiebreak)


def _grounding_line() -> str:
    """GROUNDING + INITIATIVE directives for the STABLE tier.

    Written after a real failure. Asked to work on one of the user's other projects, collie
    grepped ONLY the working directory — a different project entirely — found no matches, and
    replied that the thing did not exist on this machine, while it sat a couple of directories
    away and had been edited minutes earlier. It compounded that by treating a stray
    auto-recalled fragment from an unrelated old task as its whole knowledge of what the project
    was, and by spending two turns asking questions it could have answered itself, delivering
    nothing in the meantime.

    Three failure modes, three rules: a narrow search is not a negative result; recall is a lead,
    not a fact; answer what you can determine and only ask what you truly can't.

    Lives OUTSIDE `identity` on purpose — webapp.py's desktop persona replaces composer.identity
    wholesale, and this must survive that (same reason as _response_language_line). Byte-stable per
    platform, so it rides inside the cached prefix and costs nothing per turn."""
    # Branch first, literal second. As a trailing ternary the platform test sat BELOW the Windows
    # path it guards, which is exactly the shape the purity check cannot see — and the shape that let
    # six Windows-only features ship as silent no-ops on macOS.
    if os.name == "nt":
        roots = r"C:\Apps, C:\Program Files, %LOCALAPPDATA%\Programs, the Desktop, Downloads"
    else:
        roots = "/opt, /usr/local, $HOME, ~/Desktop, ~/Downloads"
    return (
        "GROUNDING — a search that came back empty proves only that YOUR QUERY came back empty, "
        "never that the thing does not exist. Before you tell the user something is missing, is not "
        "a real project, or is not on this machine: search WIDER than the working directory (%s), "
        "and try NAME VARIANTS — spacing, hyphens, case, and any FORMER name the thing may have had "
        "— then state what you actually searched. One narrow query must never become a confident "
        "negative. Auto-recalled memory is a LEAD, not a fact: a weak fragment, especially one left "
        "over from an unrelated task, is not evidence about what something IS — confirm it on disk "
        "before you assert it. And the user often dictates by voice, so an odd or meaningless word "
        "is frequently a mis-transcription of a proper noun (a product, repo, or path): consider "
        "homophones and go look for a near-match before asking what they meant.\n"
        "INITIATIVE — answer the questions you can answer yourself instead of asking them: find the "
        "files, read the build scripts and configs, look it up. Ask ONLY what you genuinely cannot "
        "determine — the user's accounts, billing, credentials, or a judgement that is theirs to "
        "make — and only after finishing everything that does not depend on the answer. Never open "
        "with a questionnaire. Do not present a menu of what you COULD do; do it, then report what "
        "you found. State a caveat once — do not repeat the same limitation or the same offer in a "
        "later turn of the same conversation." % roots)


@dataclass
class ComposeMeta:
    prefix_tokens: int = 0
    section_tokens: dict = field(default_factory=dict)
    prefetched: int = 0
    prefetched_ids: list = field(default_factory=list)
    elide_from: int = 0      # message index below which old tool outputs were stubbed this build;
                             # the loop compares it turn-to-turn to attribute cache misses to 'elide'
                             # (composer stays stateless — it only reports, never remembers)


class TokenBudgeter:
    def __init__(self, prefix_ceiling: int = 6000):
        self.prefix_ceiling = prefix_ceiling

    def report(self, sections: dict) -> dict:
        return {k: est_tokens(v) for k, v in sections.items()}


class ContextComposer:
    def __init__(self, memory, registry, budgeter: TokenBudgeter | None = None,
                 identity: str = "", auto_prefetch: bool = True, prefetch_k: int = 4):
        self.memory = memory
        self.registry = registry
        self.budgeter = budgeter or TokenBudgeter()
        self.identity = identity or (
            "You are collie, a focused coding agent. Use tools to gather facts before "
            "answering. Be concise and correct.")
        self.auto_prefetch = auto_prefetch
        self.prefetch_k = prefetch_k
        # Benchmark runners can disable ambient, repository-controlled prompt inputs.  A cloned
        # repo is untrusted: treating its AGENTS.md or local SKILL.md as system context creates a
        # prompt-injection asymmetry against native CLIs running with their safe/ignore-rules
        # switches.  Defaults preserve normal Collie product behaviour.
        self.include_project_rules = True
        self.include_skills = True
        self._prefetch_cache: dict = {}   # (project,user_msg) -> hits; embed once/msg
        self._skill_cache: dict = {}      # cwd -> (Library generation, skill index)

    def _skill_index(self, cwd: str) -> str:
        """Cache ordinary discovery, but invalidate when the Library lifecycle changes.

        Enable, disable, revocation, rollback, and integrity state must be visible to a 24x7 process;
        otherwise an already-created composer can keep advertising a capability that the user has
        explicitly withdrawn.
        """
        if not self.include_skills:
            return ""
        try:
            from .extensions import registry_generation
            generation = registry_generation()
        except Exception:
            generation = "unavailable"
        cached = self._skill_cache.get(cwd)
        if not cached or cached[0] != generation:
            try:
                from . import settings, skills
                extra = settings.get("SKILL_DIRS", "") or ""
                dirs = [d for d in extra.split(os.pathsep) if d.strip()] if extra else []
                value = skills.format_skill_index(skills.discover_skills(cwd, dirs))
            except Exception:
                value = ""
            self._skill_cache[cwd] = (generation, value)
        return self._skill_cache[cwd][1]

    def _project_rules(self, cwd: str, cap: int = 4000) -> str:
        if not self.include_project_rules:
            return ""
        parts = []                       # merge ALL rule files, not just the first found
        for fn in ("CLAUDE.md", "AGENTS.md", ".collie.md", ".mh.md"):  # .mh.md kept for back-compat
            p = os.path.join(cwd, fn)
            # Reject a symlinked rule file: an untrusted cloned repo could symlink CLAUDE.md at an
            # arbitrary host file (e.g. ~/.ssh/id_rsa, /etc/passwd) and leak its contents into the
            # system prompt. Only read a real regular file living in cwd.
            if os.path.islink(p):
                continue
            if os.path.exists(p):
                try:
                    with open(p, encoding="utf-8", errors="replace") as _f:
                        txt = _f.read().strip()
                    if txt:
                        parts.append("# %s\n%s" % (fn, txt))
                except Exception:
                    pass
        return "\n\n".join(parts)[:cap]

    def build(self, session: dict, user_msg: str, cwd: str, project: str,
              mode: str = "act") -> tuple[str, dict, ComposeMeta]:
        meta = ComposeMeta()

        # ---- STABLE -------------------------------------------------------
        act_role = ("MODE: Act — use tools to gather facts and make changes. "
                    "Prefer edit_file for small changes. After editing code, run "
                    "the tests (python -m pytest -q) to verify before you answer.")
        # unknown/typo'd mode -> ACT (never silently drop the tool-usage + verify contract).
        mode_role = {
            "act": act_role,
            "plan": ("MODE: Plan — inspect the project and produce an editable plan artifact with "
                     "scope, files, risks, and proposed checks. Do not edit project files or run commands."),
            "review": ("MODE: Review — inspect only. Report prioritized findings with concrete "
                       "file paths and line numbers. Do not edit files or run commands."),
            "test": ("MODE: Test — inspect files and run only the proposed verification command. "
                     "Do not edit anything. Return the failing check as evidence for a separate Build run."),
        }.get(mode, act_role)
        tool_names = "TOOLS (always-on): " + ", ".join(
            t.name for t in self.registry.always_on())
        deferred = self.registry.deferred_names()
        if deferred:
            # frozen wording + sorted names (deferred_names is sorted) → this line is byte-stable for
            # the whole session, so activating a tool never re-bills the cached prefix (point-12 A).
            tool_names += ("\nTOOLS (deferred — call load_tools with the exact name before first "
                           "use): " + ", ".join(deferred))
        # Working directory: every tool (bash, read_file, edit_file, glob, grep) already
        # runs FROM here. Without this line the model burns turns guessing its location —
        # observed on pylint-4551: ~15 turns lost to `cd /repo`, `cd /workspace`, `cd ~`,
        # and absolute /home/user/... paths that don't exist.
        # The "don't cd elsewhere" clause is about not GUESSING prefixes for files in THIS repo. It
        # was being over-applied as "nothing outside cwd exists" (the VocalCode miss — see
        # _grounding_line), so the last sentence carves out the case where the user's actual target
        # legitimately lives elsewhere on the machine.
        workdir = ("WORKING DIRECTORY: %s\nAll tools run from this directory. Pass paths "
                   "RELATIVE to it (e.g. `pylint/pyreverse/writer.py`). Do NOT `cd` "
                   "elsewhere and do NOT prepend prefixes like /repo, /workspace, ~, or "
                   "/home/user — the repository root already IS your working directory. That is "
                   "about THIS project's files; when the user asks about something that genuinely "
                   "lives elsewhere on this machine, go find it and use its absolute path — never "
                   "conclude it does not exist merely because it is not in this directory." % cwd)
        # SKILLS index (point 10): lazy name+description+path lines, ~20 tok/skill, read on demand.
        # Ordinary sources remain cached per cwd; a Library lifecycle/integrity generation change
        # deliberately invalidates the prefix so enable/disable/revoke is truthful in 24x7 runs.
        skill_index = self._skill_index(cwd)
        # RESPONSE LANGUAGE + GROUNDING sit right after identity so they survive identity overrides
        # (the desktop persona in webapp.py replaces self.identity wholesale but never touches these
        # lines). Both are byte-stable, so they stay inside the cached prefix.
        stable_parts = [self.identity, _response_language_line(), _grounding_line(),
                        mode_role, tool_names]
        if skill_index:
            stable_parts.append(skill_index)         # after tools, before workdir (STABLE slot)
        stable_parts.append(workdir)
        stable = "\n".join(stable_parts)

        # ---- CONTEXT ------------------------------------------------------
        rules = self._project_rules(cwd)
        context = ("PROJECT RULES:\n" + rules) if rules else ""

        # ---- VOLATILE (last) ---------------------------------------------
        vol_parts = []
        blocks = self.memory.core_blocks([f"project:{project}", "global"])
        if blocks:
            # cap core memory the same way as the prefetch block: a block written with a large
            # char_limit, or many blocks, would otherwise balloon the cached prefix unbounded (the
            # prefix_ceiling was never enforced). Per-block truncate + an aggregate budget.
            budget, blines = 2400, []
            for b in blocks:
                v = str(b["value"])
                v = v[:500] + ("…" if len(v) > 500 else "")
                if budget - len(v) < 0:
                    break
                blines.append("- [%s] %s" % (b["label"], v)); budget -= len(v)
            if blines:
                vol_parts.append("CORE MEMORY:\n" + "\n".join(blines))
        # user_msg is a str for text, but a multimodal message (attached image) is a list of content
        # blocks — content_text() flattens both to plain text so prefetch/recall/cache-key never see a
        # list (a list .strip() crashed the run, and a list is unhashable as a cache key).
        user_text = content_text(user_msg)
        if self.auto_prefetch and user_text.strip():
            # embed once per user message, not once per loop turn (user_msg is
            # constant within a run) — keeps a ~950ms jina-v3 embed off the hot loop.
            ck = (project, user_text)
            if ck not in self._prefetch_cache:
                self._prefetch_cache[ck] = self.memory.recall(
                    user_text, project=project, k=self.prefetch_k)
            hits = self._prefetch_cache[ck]
            if hits:
                # cap the auto-prefetch block: hits carry UNCAPPED h["text"], so k long recalled
                # facts could balloon the (cached, per-turn) prefix past the ceiling unbounded.
                # Per-fact cap + a block budget; hits are score-sorted so the weakest drop first.
                budget, lines, incl_ids = 2000, [], []
                for h in hits:
                    t = h["text"]
                    t = t[:400] + ("…" if len(t) > 400 else "")
                    if budget - len(t) < 0:
                        break
                    lines.append("- " + t); budget -= len(t); incl_ids.append(h["id"])
                # count what ACTUALLY made it into the prompt (the budget loop can drop the weakest),
                # so meta.prefetched / mem_recalls don't over-report facts the model never saw.
                meta.prefetched = len(lines)
                meta.prefetched_ids = incl_ids
                if lines:
                    vol_parts.append("RELEVANT MEMORY (auto-recalled):\n" + "\n".join(lines))
        # date-only, NOT %H:%M — this string is inside the single cached system block, so a
        # per-minute timestamp busted the ENTIRE cached prefix (identity + tool names + rules)
        # on every minute boundary of a multi-minute run, forcing a full re-write and killing the
        # cache_read discount that is collie's core efficiency lever.
        vol_parts.append("NOW: " + time.strftime("%Y-%m-%d"))
        volatile = "\n\n".join(vol_parts)

        system = "\n\n".join(p for p in (stable, context, volatile) if p)
        # Fixed input per turn = system prompt + the tool schemas we send to the
        # model (they live in the API `tools` param, but they ARE cached prefix and
        # must count for a fair comparison vs harnesses that inline everything).
        tool_schema_tok = est_tokens(json.dumps(self.registry.active_schemas()))
        # Report the skill index as its OWN section (skills ⊂ the byte string but NOT double-counted
        # in "stable" — point 10 amendment ③): subtract it from the stable line for accounting only.
        stable_wo_skills = stable.replace(("\n" + skill_index) if skill_index else "", "", 1) \
            if skill_index else stable
        meta.section_tokens = self.budgeter.report(
            {"stable": stable_wo_skills, "context": context, "volatile": volatile})
        meta.section_tokens["skills"] = est_tokens(skill_index)
        meta.section_tokens["tool_schemas"] = tool_schema_tok
        meta.prefix_tokens = est_tokens(system) + tool_schema_tok

        # Bound history growth: over a 35-turn SWE run the message list is dominated by
        # bulky OLD tool outputs (file reads, code_search dumps). Shrink those older than
        # the last ~14 messages to a stub — the model rarely needs the full text of a read
        # it did 20 turns ago, and this keeps per-turn input from ballooning. Message
        # structure (assistant tool_calls ↔ tool results) is preserved, so pairing holds.
        #
        # Overflow-recovery mode (point 9): when a prior turn hit a context-overflow error, the loop
        # sets session["_overflow_shrink"] and rebuilds — tighten the window (14→4), the stub
        # (240→120), and additionally cap the RECENT window's tool content (head+tail keep, middle
        # dropped) so a single huge read can't re-overflow. Never DROP a message (pairing must hold).
        shrink = bool(session.get("_overflow_shrink"))
        window = 4 if shrink else 14
        stub = 120 if shrink else 240
        recent_cap = 4000 if shrink else None
        msgs = session.get("messages", [])
        keep_from = len(msgs) - window
        meta.elide_from = keep_from
        provider_messages = []
        for i, m in enumerate(msgs):
            if m.get("role") == "tool":
                c = m.get("content", "")
                if isinstance(c, str) and i < keep_from and len(c) > stub:
                    m = {**m, "content": c[:stub] + " …[older tool output elided]"}
                elif isinstance(c, str) and recent_cap and len(c) > recent_cap:
                    # shrink mode only: keep head+tail of a big RECENT output, drop the middle
                    # (backlog #2 lesson: never lose the error tail); {**m} copy — don't mutate session.
                    half = recent_cap // 2
                    m = {**m, "content": c[:half]
                         + " …[overflow recovery: middle truncated; re-run the tool if needed] "
                         + c[-half:]}
            elif isinstance(m.get("content"), list) and i < keep_from:
                # Images are the one payload the stub logic above cannot reach: they ride a user
                # message as a base64 block, not a str, so without this a screenshot stays in the
                # cached prefix for the rest of the run and a handful of them overflow it outright.
                # The recent window keeps whatever was just looked at; older frames become a line
                # saying they existed, which is enough for the model to take a fresh one. Applies to
                # user-attached images too — the same cost applies whoever produced them.
                kept, dropped = [], 0
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "image" and b.get("data"):
                        dropped += 1
                    else:
                        kept.append(b)
                if dropped:
                    kept.append({"type": "text", "text":
                                 "[%d older image(s) dropped from history to save context — "
                                 "capture again if you need to look]" % dropped})
                    m = {**m, "content": kept}
            provider_messages.append(m)
        return system, provider_messages, meta
