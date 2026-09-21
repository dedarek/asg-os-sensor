"""Pure command-Hook payload normalization and response semantics.

The old executable entrypoint wrote to ASG's local event log and called the
local control client.  Production installation now belongs to the SOC package
runtime, so this module intentionally contains no filesystem, process-launch
or service dependency.
"""
import time
import uuid
import subprocess

# Structured event-name dialects that share this payload shape. Aliases come
# from hosts whose own migration tooling converts between the two spellings;
# recognition stays structural (exact keys), never product-name inference.
EVENTS = {'SessionStart': 'hook.loaded', 'UserPromptSubmit': 'user.input',
          'PreToolUse': 'tool.execute.before', 'PostToolUse': 'tool.execute.after',
          'Stop': 'assistant.output',
          'BeforeAgent': 'user.input', 'BeforeTool': 'tool.execute.before',
          'AfterTool': 'tool.execute.after', 'AfterAgent': 'assistant.output'}


def handle(payload, config, target, *, decide=None, emit=None):
    """Pure injectable protocol boundary; production identity is resolved below."""
    native = payload.get('hook_event_name')
    kind = EVENTS.get(native)
    if not kind: return {}, 0
    call = payload.get('tool_use_id') or payload.get('tool_call_id')
    content = (payload.get('prompt') if kind == 'user.input' else
               payload.get('last_assistant_message') or payload.get('prompt_response')
               if kind == 'assistant.output' else
               payload.get('tool_input') if kind == 'tool.execute.before' else
               payload.get('tool_response') if kind == 'tool.execute.after' else None)
    event = {'event': kind, **target, 'timestamp': time.time(), 'event_id': uuid.uuid4().hex,
             'session_id': payload.get('session_id'), 'tool_call_id': call, 'call_id': call,
             'tool': payload.get('tool_name'), 'content': content,
             'content_complete': content is not None, 'capture_layer': 'command_hooks',
             'native_payload': payload}
    if kind != 'hook.loaded':
        emit({'event': 'hook.loaded', **target, 'timestamp': time.time(),
              'source': 'command_hook_callback_executed', 'session_id': payload.get('session_id')})
    emit(event)
    if kind != 'tool.execute.before' or not config.get('control_client'): return {}, 0
    try:
        decision = decide({**target, 'call_id': call or event['event_id'],
                           'tool': payload.get('tool_name'), 'input': payload.get('tool_input')})
        allowed = decision.get('decision') == 'allow'
    except (OSError, ValueError, subprocess.SubprocessError):
        allowed = False
    # The response mirrors the dialect the host just used (its own event name).
    # Both structured variants treat a missing decision as allow, so the
    # blocking form is emitted only on refusal; either way this is only our
    # declared answer, not proof the host obeys.
    response = {'hookSpecificOutput': {'hookEventName': native,
                'permissionDecision': 'allow' if allowed else 'deny',
                'permissionDecisionReason': 'ASG execution policy'}}
    if not allowed:
        response.update({'decision': 'block', 'reason': 'ASG execution policy'})
    return response, 0 if allowed else 2
