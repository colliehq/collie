"""Experimental normal-context policy; overflow and image handling stay unchanged."""

INSTRUMENT = '''
from harness.context import ContextComposer as _ExperimentalComposer
_original_context_build = _ExperimentalComposer.build

def _full_tool_history_build(self, session, *args, **kwargs):
    system, selected, meta = _original_context_build(self, session, *args, **kwargs)
    has_images = any(isinstance(message.get("content"), list) and any(
        isinstance(block, dict) and block.get("type") == "image"
        for block in message["content"]) for message in meta.pre_elision)
    if not session.get("_overflow_shrink") and not has_images:
        # Semantic compaction has already produced pre_elision. We retain only
        # what the host's current checkpoint selected, never discarded history.
        selected = meta.pre_elision
        meta.elide_from = 0
    return system, selected, meta

_ExperimentalComposer.build = _full_tool_history_build
'''
