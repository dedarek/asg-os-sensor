"""Deterministic IO contract validation, scoped to a captured turn, not all traffic.

OTel conversation/tool identifiers are accepted; ASG checkpoint fields are a
local capture contract, not an OpenTelemetry standard or independent oracle.
"""
from collections import Counter, defaultdict

from runtime import event_vocabulary

KINDS = ('user.input', 'model.request', 'model.response', 'tool.execute.before',
         'tool.execute.after', 'assistant.output')


def _kind(record):
    """Canonical kind for a record, falling back to the producer's own label.

    Producers name the same stage differently (Codex emits
    ``user.prompt.submitted`` for the user's own message).  Without this the
    acceptance check reports a captured turn as missing input.
    """
    raw = record.get('event_type')
    return event_vocabulary.canonical(raw) or raw


def contract():
    return {'version': 1, 'events': list(KINDS),
            'required_fields': ['session_id', 'turn_id', 'event_id', 'sequence',
                                'content', 'content_complete'],
            'pairing': 'model: request_id (unique per retry); tool: tool_call_id; both scoped to instance/session/turn/agent_id',
            'otel_aliases': {'session_id': 'gen_ai.conversation.id', 'tool_call_id': 'gen_ai.tool.call.id'},
            'checkpoint': 'io.turn.end with same session_id/turn_id/agent_id, expected_counts for all six events and last_sequence; counters must originate at ingress/call sites, not be calculated from delivered log rows',
            'streaming': 'Emit complete reassembled content only after stream end; content_complete=false for interrupted/truncated streams. Give retries distinct request_id and subagents distinct agent_id.',
            'scope': 'Checkpoint verification is producer-reported coverage; independent canary/ingress comparison is needed for end-to-end loss proof. Never invent missing IDs, content, counters or support.'}


def assess(records, coverage):
    groups = defaultdict(list)
    counts = Counter()
    issues = []
    def field(p, name, alias=None):
        attributes = p.get('attributes')
        attributes = attributes if isinstance(attributes, dict) else {}
        return p.get(name) or (p.get(alias) if alias else None) or attributes.get(alias or name)
    for r in records:
        p = r.get('payload') or {}
        kind = _kind(r)
        if kind not in KINDS and kind != 'io.turn.end':
            continue
        if kind in KINDS:
            counts[kind] += 1
        session = field(p, 'session_id', 'gen_ai.conversation.id')
        turn = p.get('turn_id')
        instance = r.get('instance_id')
        if not all(isinstance(v, str) and v for v in (session, turn, instance)):
            issues.append('missing_instance_session_or_turn')
            continue
        agent = p.get('agent_id', 'root')
        if not isinstance(agent, str):
            issues.append('invalid_agent_id')
            continue
        groups[(instance, session, turn, agent)].append((kind, p))
    turns = []
    for key, rows in groups.items():
        gaps = []
        events = [(k, p) for k, p in rows if k in KINDS]
        checkpoints = [p for k, p in rows if k == 'io.turn.end']
        actual = Counter(k for k, _ in events)
        ids = [p.get('event_id') for _, p in events]
        seq = [p.get('sequence') for _, p in events]
        if any(not isinstance(v, str) or not v for v in ids) or len(set(str(v) for v in ids)) != len(ids):
            gaps.append('missing_or_duplicate_event_id')
        if any(type(v) is not int or v < 1 for v in seq):
            gaps.append('missing_sequence')
        elif sorted(seq) != list(range(1, len(seq) + 1)):
            gaps.append('sequence_gap_or_duplicate')
        if any('content' not in p or p['content'] is None or p.get('content_complete') is not True or p.get('truncated') for _, p in events):
            gaps.append('missing_or_incomplete_content')
        for before, after, idname, alias in (
                ('model.request', 'model.response', 'request_id', None),
                ('tool.execute.before', 'tool.execute.after', 'tool_call_id', 'gen_ai.tool.call.id')):
            pairs = defaultdict(Counter)
            for k, p in events:
                if k not in (before, after):
                    continue
                call = field(p, idname, alias)
                if not isinstance(call, str) or not call:
                    gaps.append('missing_' + idname)
                else:
                    pairs[call][k] += 1
            if any(v[before] != 1 or v[after] != 1 for v in pairs.values()):
                gaps.append('unpaired_' + idname)
        if len(checkpoints) != 1:
            gaps.append('missing_or_duplicate_turn_checkpoint')
        else:
            checkpoint = checkpoints[0]
            expected = checkpoint.get('expected_counts')
            if not isinstance(expected, dict) or any(type(expected.get(k)) is not int or expected[k] < 0 or expected[k] != actual[k] for k in KINDS):
                gaps.append('checkpoint_count_mismatch')
            if type(checkpoint.get('last_sequence')) is not int or checkpoint['last_sequence'] != len(events):
                gaps.append('checkpoint_sequence_mismatch')
        if not actual['user.input'] or not actual['assistant.output'] or not actual['model.request']:
            gaps.append('missing_conversation_or_model_events')
        turns.append({'instance_id': key[0], 'session_id': key[1], 'turn_id': key[2], 'agent_id': key[3],
                      'status': 'gaps' if gaps else 'checkpoint_verified',
                      'counts': dict(actual), 'gaps': sorted(set(gaps))})
    source = coverage.get('source') or {}
    if source.get('truncated') or source.get('partial_lines') or coverage.get('malformed_records') or coverage.get('unavailable_binding_count') or coverage.get('missing_inputs'):
        issues.append('source_incomplete_or_unavailable')
    status = ('checkpoint_verified' if turns and all(t['status'] == 'checkpoint_verified' for t in turns) and not issues
              else 'gaps' if counts else 'not_observed')
    return {'version': 1, 'status': status, 'end_to_end_verified': False,
            'scope': 'registered_instance_bounded_window',
            'capabilities': [{'event': k, 'observed': counts[k] > 0, 'count': counts[k]} for k in KINDS],
            'turns': turns, 'issues': sorted(set(issues)),
            'limitations': ['计数与序列检查仅验证已上报窗口；完全未上报的轮次需独立入口记录或验收消息核对',
                            'checkpoint_verified 不代表所有历史/未来消息已捕获；不自动证明子 Agent 均已登记',
                            '未观察到工具事件可能是本轮未使用工具；需要结束计数声明为零，不能自行推断']}
