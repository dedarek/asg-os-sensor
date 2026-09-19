"""Automatic, bounded, per-instance onboarding checks from real bound evidence.

Never submits messages to a user's open conversation. Independent effect tests
must use a controlled workspace and register current installed-byte-bound proof.
"""
from runtime import event_vocabulary, io_acceptance
from runtime.hook_acceptance import verified_checks


def assess(records, coverage, instance_id, acceptances=()):
    rows = [r for r in records if r.get('instance_id') == instance_id]
    io = io_acceptance.assess(rows, coverage)
    checks = []
    def add(name, label, passed, detail, failed=False):
        checks.append({'id': name, 'label': label,
                       'status': 'passed' if passed else 'failed' if failed else 'pending',
                       'detail': detail})
    kinds = {r.get('event_type') for r in rows}
    add('loaded', 'Hook 加载', 'hook.loaded' in kinds, '需要当前实例的加载事件')
    for kind, role, label in (('user.input', 'user', '用户正文'), ('assistant.output', 'assistant', '助手正文')):
        present = any(r.get('event_type') == kind and event_vocabulary.text_for(r.get('payload'), role) for r in rows)
        add(kind, label, present, '按当前实例检查非空正文；片段不代表完整回合')
    completed = [t for t in io['turns'] if t['status'] == 'checkpoint_verified']
    add('io_round', '完整回合计数与关联', bool(completed) and not io['issues'],
        '用户/模型/助手事件、编号、序列、完整正文和结束计数必须一致；仅限已上报轮次')
    tool_round = any(t['counts'].get('tool.execute.before', 0) > 0 for t in completed)
    add('tool_pair', '工具参数与结果', tool_round and not io['issues'], '需要同一完整回合内真实工具前后配对；零次工具不算已验证')
    proofs = []
    for v in acceptances:
        target = v.get('target') or {}
        iid = str(target.get('pid')) + ':' + str(target.get('create_time'))
        if v.get('current') is True and iid == instance_id: proofs.append(v)
    effect = any({'allow_effect', 'deny_effect'}.issubset(verified_checks(v.get('checks'))) for v in proofs)
    waiting = any({'decision_wait', 'allow_effect', 'deny_effect'}.issubset(verified_checks(v.get('checks'))) for v in proofs)
    add('control_effect', '放行与拒绝效果', effect, '独立验收核对真实副作用；拒绝回执不作为阻断证明')
    add('decision_wait', '执行前等待决策', waiting, '独立验收需记录 decision_wait：延迟决定期间没有执行副作用')
    source_issue = bool(io['issues'])
    add('source', '采集来源与编号', bool(rows) and not source_issue, ', '.join(io['issues']) or '仅检查当前有界窗口', failed=source_issue)
    passed = sum(c['status'] == 'passed' for c in checks)
    return {'version': 1, 'instance_id': instance_id,
            'status': 'passed' if passed == len(checks) else 'failed' if any(c['status'] == 'failed' for c in checks) else 'pending',
            'passed': passed, 'total': len(checks), 'checks': checks,
            'control': {'effects_verified': effect, 'wait_verified': waiting, 'blocking_verified': effect and waiting},
            'scope': '当前实例、当前 Hook 版本和已读取窗口；不是所有流量或其他实例的保证',
            'next_checks': [c['id'] for c in checks if c['status'] != 'passed']}
