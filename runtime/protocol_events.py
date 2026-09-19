"""Loss-aware internal envelope; raw evidence stays intact in hook_data.records.

OTel aliases align identifiers, not an assertion of OTLP transport support.
ACP messages must already be captured on an attached transport by a producer.
This mapper never invents missing turn IDs, stream completion or control proof.
"""
from runtime import event_vocabulary as vocabulary


def normalize(record):
    p = record.get('payload') or {}
    if not isinstance(p, dict): p = {}
    detail = p.get('detail') if isinstance(p.get('detail'), dict) else {}
    attributes = dict(p['attributes']) if isinstance(p.get('attributes'), dict) else {}
    def field(name, alias=None):
        for source in (p, detail, attributes):
            for key in (name, alias):
                if key and source.get(key) is not None: return source[key]
        return None
    event = vocabulary.record_kind(record) or record.get('event_type')
    session = field('session_id', 'gen_ai.conversation.id')
    call = field('tool_call_id', 'gen_ai.tool.call.id') or field('call_id') or field('tool_use_id')
    content = p.get('content')
    complete = p.get('content_complete') is True
    protocol = 'otel' if p.get('capture_layer') == 'otel' else 'acp' if p.get('capture_layer') == 'acp_reassembled' else 'hook_events'
    message = p.get('protocol_message', p)
    if isinstance(message, dict) and message.get('jsonrpc') == '2.0':
        protocol = 'acp'
        params = message.get('params') if isinstance(message.get('params'), dict) else {}
        update = params.get('update') if isinstance(params.get('update'), dict) else {}
        session = params.get('sessionId', session)
        # A chunk never becomes a completed assistant response merely by renaming.
        kind = update.get('sessionUpdate')
        if message.get('method') == 'session/prompt':
            event, content, complete = 'user.input', params.get('prompt'), True
        elif message.get('method') == 'session/update':
            if kind in ('agent_message_chunk', 'user_message_chunk'):
                event = 'assistant.output' if kind == 'agent_message_chunk' else 'user.input'
                content, complete = update.get('content'), False
            elif kind in ('tool_call', 'tool_call_update'):
                call = update.get('toolCallId')
                status = update.get('status')
                if status == 'completed':
                    event, content = 'tool.execute.after', update.get('rawOutput', update.get('content'))
                    complete = content is not None
                elif status == 'failed':
                    event, content, complete = 'tool.error', update.get('rawOutput', update.get('content')), False
                elif kind == 'tool_call':
                    event, content = 'tool.execute.before', update.get('rawInput')
                    complete = content is not None
                else:
                    event, content, complete = 'tool.progress', update, False
        elif message.get('method') == 'session/request_permission':
            event, content, complete = 'permission.request', params, True
            call = (params.get('toolCall') or {}).get('toolCallId')
    if content is None:
        if event in ('user.input', 'assistant.output'):
            found = vocabulary.text_for(p, 'user' if event == 'user.input' else 'assistant')
            if found: content = found['text']
        elif event == 'tool.execute.before': content = p.get('tool_input', detail.get('args'))
        elif event == 'tool.execute.after': content = p.get('tool_response', detail.get('result'))
    for name, value in (('gen_ai.conversation.id', session), ('gen_ai.tool.call.id', call), ('gen_ai.agent.id', field('agent_id'))):
        if value is not None: attributes[name] = value
    if event in ('tool.execute.before', 'tool.execute.after', 'tool.error'):
        attributes.setdefault('gen_ai.operation.name', 'execute_tool')
    normalized = {**p, 'event': event, 'session_id': session,
                  'turn_id': field('turn_id'), 'request_id': field('request_id'),
                  'tool_call_id': call, 'event_id': field('event_id'),
                  'sequence': field('sequence'), 'content': content,
                  'content_complete': complete and not bool(p.get('truncated')),
                  'attributes': attributes}
    if field('agent_id') is not None: normalized['agent_id'] = field('agent_id')
    # Preserve checkpoint and producer agent IDs without manufacturing either.
    return {**record, 'event_type': event, 'payload': normalized,
            'normalization': {'version': 1, 'protocol': protocol,
                              'original_event': vocabulary.native_name(record),
                              'missing_identifiers': [k for k in ('session_id', 'turn_id', 'event_id') if not normalized.get(k)]}}
