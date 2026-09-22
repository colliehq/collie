"""Experiment only: restate the existing plain response contract after a delta."""
REMINDER = (
    '\n\n[Response protocol reminder]\n'
    'Reply with EXACTLY ONE JSON object and nothing else. '
    'Request one host tool with {"tool":"<name>","args":{...}}, '
    'or finish with {"answer":"<final answer>"}. '
    'Do not combine multiple JSON objects or add prose. '
    'After requesting one tool, stop and wait for its result.'
)

INSTRUMENT = '''
_before_reminder_payload = ClaudeAgentSdkProvider._payload

def _reminded_payload(self, messages, tool_schemas):
    text = _before_reminder_payload(self, messages, tool_schemas)
    routed = getattr(self._route, "plan", None)
    if tool_schemas and not self.structured_output and routed and not routed[1].first:
        text += REMINDER_TEXT
    return text

ClaudeAgentSdkProvider._payload = _reminded_payload
'''.replace('REMINDER_TEXT', repr(REMINDER))
