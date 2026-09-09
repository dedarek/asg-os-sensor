"""Evidence-bound dashboard status; recipes are proposals, never installation proof."""
ASSET_FIELDS = ('model_routing', 'parsed_config', 'registered_tools_and_mcp',
                'skills', 'system_prompt_rules', 'network_surface', 'child_executions')
DISABLED_REASON = '深度调查已禁用 (ASG_AUTONOMOUS_ANALYSIS=0)'


def asset_state(record=None):
    # Only the collection adapter may supply this record, never a recipe default.
    if not record or not record.get('source'):
        return {'status': 'not_collected', 'label': '尚未采集', 'source': None, 'value': None}
    status = record.get('status')
    if status not in ('collected', 'failed', 'unsupported'):
        return asset_state()
    value = record.get('value') if status == 'collected' else None
    label = {'collected': '已采集' if value else '未发现（已采集）',
             'failed': '采集失败', 'unsupported': '不支持'}[status]
    return {'status': status, 'label': label, 'value': value,
            'source': record['source'], 'message': record.get('message', '')}


def investigation_state(enabled, running=False, result=None):
    result = result or {}
    if not enabled:
        return {'status': 'disabled', 'label': '禁用', 'message': DISABLED_REASON,
                'source': 'ASG_AUTONOMOUS_ANALYSIS', 'can_request': False}
    status = 'running' if running else {'succeeded': 'succeeded', 'failed': 'failed',
        'unavailable': 'failed', 'blocked': 'failed', 'busy': 'not_scheduled'}.get(result.get('status'), 'not_scheduled')
    return {'status': status, 'label': {'running': '执行中', 'succeeded': '成功',
            'failed': '失败', 'not_scheduled': '未调度'}[status],
            'message': result.get('message', ''), 'source': 'investigation_scheduler',
            'can_request': not running}


def presentation(enabled, running=False, result=None, identity=None, collections=None):
    identity = identity or {}
    return {'investigation': investigation_state(enabled, running, result),
            'assets': {key: asset_state((collections or {}).get(key)) for key in ASSET_FIELDS},
            'hook_state': {'status': 'not_installed', 'label': '未安装',
                           'source': 'stage1_no_installer', 'verified': False},
            'sink_state': {'status': 'not_connected', 'label': '未接入／未验证'},
            'host_platform': '桌面应用（本地应用元数据）' if identity.get('source') == 'bundle-metadata' else '未知'}
