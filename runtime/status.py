"""Evidence-bound dashboard status; recipes are proposals, never installation proof."""
ASSET_FIELDS = ('model_routing', 'parsed_config', 'registered_tools_and_mcp',
                'skills', 'system_prompt_rules', 'network_surface', 'child_executions')
DISABLED_REASON = '深度调查已禁用 (ASG_AUTONOMOUS_ANALYSIS=0)'


def asset_state(record=None):
    # Only the collection adapter may supply this record, never a recipe default.
    if not isinstance(record, dict):
        record = {}
    status = record.get('status', 'not_collected')
    allowed = {'collected', 'empty', 'failed', 'unsupported', 'unknown', 'not_collected'}
    if status not in allowed:
        status = 'not_collected'
    value = record.get('value') if status in ('collected', 'empty') else None
    labels = {
        'collected': '已采集',
        'empty': '未发现（已采集）',
        'failed': '采集失败',
        'unsupported': '不支持',
        'unknown': '未知（证据不足）',
        'not_collected': '尚未采集',
    }
    sources = record.get('sources', [])
    if not isinstance(sources, list):
        sources = []
    return {
        'status': status,
        'label': labels[status],
        'value': value,
        'source': record.get('source') or (sources[0] if sources else None),
        'sources': sources,
        'evidence_refs': record.get('evidence_refs', []),
        'uncertainty': record.get('uncertainty', []),
        'message': record.get('message', ''),
    }


def investigation_state(enabled, running=False, result=None):
    result = result or {}
    if not enabled:
        return {'status': 'disabled', 'label': '禁用', 'message': DISABLED_REASON,
                'source': 'ASG_AUTONOMOUS_ANALYSIS', 'can_request': False}
    raw_status = result.get('status')
    status = 'running' if running else {
        'succeeded': 'succeeded', 'failed': 'failed', 'timeout': 'timeout',
        'reused': 'reused',
        'cancelled': 'cancelled', 'unavailable': 'failed', 'blocked': 'failed',
        'queued': 'queued', 'deferred': 'deferred',
        'busy': 'not_scheduled',
    }.get(raw_status, 'not_scheduled')
    message = result.get('message', '')
    # 保守呈现：历史持久化消息中废弃的'接入点建议有证据支持'断言一律改为 proposed/unverified。
    # 当前没有可核对接入点存在的证据映射，结构校验通过不代表 Hook 有证据支持。
    if status == 'succeeded' and message:
        message = message.replace('接入点建议有证据支持', '接入点 proposed/unverified（未核对接入点证据）')
    state = {'running': '执行中', 'succeeded': '成功', 'reused': '指纹复用（未调用Goose）', 'failed': '失败',
             'timeout': '超时（已保留证据）', 'cancelled': '已取消（已保留证据）',
             'queued': '排队中', 'deferred': '暂缓（队列已满）',
             'not_scheduled': '未调度'}[status]
    response = {'status': status, 'label': state, 'message': message,
                'source': 'investigation_scheduler', 'can_request': not running}
    for key in ('log_dir', 'partial_findings', 'lifecycle', 'resume'):
        if key in result:
            response[key] = result[key]
    response['can_continue'] = status in ('failed', 'timeout', 'cancelled') and bool(result.get('log_dir'))
    return response


def presentation(enabled, running=False, result=None, identity=None, collections=None):
    identity = identity or {}
    return {'investigation': investigation_state(enabled, running, result),
            'assets': {key: asset_state((collections or {}).get(key)) for key in ASSET_FIELDS},
            'hook_state': {'status': 'not_installed', 'label': '未安装',
                           'source': 'stage1_no_installer', 'verified': False},
            'sink_state': {'status': 'not_connected', 'label': '未接入／未验证'},
            'host_platform': '桌面应用（本地应用元数据）' if identity.get('source') == 'bundle-metadata' else '未知'}
