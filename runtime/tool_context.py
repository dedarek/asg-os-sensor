"""Bound completed tool history without inventing semantic summaries.

Only protocol-complete older tool groups are elided. Observation ids remain
retrievable from the existing MCP evidence store; system/user instructions and
recent tool call/result pairs are retained unchanged. No product-specific rules.
"""
from __future__ import annotations
import copy
import json


def compact(payload: dict, window: int) -> tuple[dict, dict]:
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise ValueError('tool_context_window must be a positive integer')
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return payload, {'applied': False}
    tool_indices = [i for i, m in enumerate(messages) if m.get('role') == 'tool']
    if len(tool_indices) <= window:
        return payload, {'applied': False}
    cutoff = tool_indices[-window]
    while cutoff > 0 and not (messages[cutoff].get('role') == 'assistant' and messages[cutoff].get('tool_calls')):
        cutoff -= 1
    if cutoff == 0:
        return payload, {'applied': False}
    # Never elide an incomplete call or one whose response lies in the retained suffix.
    prefix = messages[:cutoff]
    calls = {c['id']: c for m in prefix for c in m.get('tool_calls', []) if isinstance(c, dict) and c.get('id')}
    responses = {m.get('tool_call_id'): m for m in prefix if m.get('role') == 'tool'}
    if not calls or not set(calls).issubset(responses):
        return payload, {'applied': False}
    notes, findings = [], {}
    for call_id, call in calls.items():
        msg = responses[call_id]
        try:
            content = json.loads(msg.get('content', '{}'))
        except (ValueError, TypeError):
            content = {}
        if not isinstance(content, dict):
            content = {}
        fn = call.get('function') or {}
        try:
            args = json.loads(fn.get('arguments', '{}'))
        except (ValueError, TypeError):
            args = {}
        args = args if isinstance(args, dict) else {}
        note = {'tool': fn.get('name'), 'evidence_id': content.get('source_evidence_id') or content.get('evidence_id')}
        for field in ('path', 'query', 'scope', 'select'):
            if isinstance(args.get(field), str):
                note[field] = args[field][:400]
        if content.get('status'):
            note['status'] = content['status']
        notes.append(note)
        finding = content.get('finding')
        if isinstance(finding, dict):
            # Keep the most recent model-authored finding per semantic slot, not
            # a new program-written role or asset conclusion.
            key = str(finding.get('asset') or finding.get('kind'))
            if len(json.dumps(finding)) <= 3000:
                findings[key] = finding
    preserved = [m for m in prefix if m.get('role') in ('system', 'developer', 'user')]
    memory = {'archived_observations': notes[-48:], 'saved_model_findings': findings,
              'instruction': 'Older completed tool outputs were removed from this request to fit the configured model context. Their original evidence remains on disk. Retrieve specific evidence with read_evidence, not a full rescan. These are references, not proof of hook activation. Recent tool pairs are unchanged.'}
    result = copy.deepcopy(payload)
    result['messages'] = preserved + [{'role': 'user', 'content': '[Supervisor evidence index]\n' + json.dumps(memory, ensure_ascii=False)}] + copy.deepcopy(messages[cutoff:])
    return result, {'applied': True, 'elided_tool_results': len(responses),
                    'retained_tool_results': sum(m.get('role') == 'tool' for m in messages[cutoff:]),
                    'input_messages': len(messages), 'output_messages': len(result['messages'])}
