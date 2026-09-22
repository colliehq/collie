"""Observation and selection wrapper for paired native session experiments."""

def instrument(base, session_enabled):
    observed = base.replace(
        'from harness.claude_agent_sdk import ClaudeAgentSdkProvider',
        'from harness.experimental_session_sdk import ExperimentalSessionSdkProvider as ClaudeAgentSdkProvider\n'
        'import harness.claude_agent_sdk as _sdk_module')
    return observed + '''
_experimental_init = ClaudeAgentSdkProvider.__init__
_experimental_instances = []
def _session_configured_init(self, *args, **kwargs):
    kwargs["session_enabled"] = SESSION_ENABLED
    kwargs["subscription_only"] = True
    _experimental_init(self, *args, **kwargs)
    _experimental_instances.append(self)
ClaudeAgentSdkProvider.__init__ = _session_configured_init
_sdk_module.ClaudeAgentSdkProvider = ClaudeAgentSdkProvider
_original_predict = swe.predict_collie
def _session_predict(*args, **kwargs):
    try:
        return _original_predict(*args, **kwargs)
    finally:
        rows = []
        for provider in _experimental_instances:
            had_session = provider._session is not None
            closed = provider.close_session("benchmark_finished")
            if provider._unresolved:
                provider.cancel_current()
            rows.append({
                "session_enabled": provider.session_enabled,
                "structured_output": provider.structured_output,
                "had_session": had_session,
                "close_reported": closed,
                "unresolved_workers": len(provider._unresolved),
                "active_registrations": len(provider._active_runs),
                "session_retired": provider._session is None,
                "evidence": provider.session_evidence,
            })
        (root/"provider-session-final.json").write_text(
            json.dumps(rows,ensure_ascii=False), encoding="utf-8")
swe.predict_collie = _session_predict
'''.replace('SESSION_ENABLED', repr(bool(session_enabled)))
