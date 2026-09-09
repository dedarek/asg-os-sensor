"""Bounded local asset facts. Never export raw config, prompts or credentials.

采集范围是有限的（见 limitations）：只解析"目标进程打开过的、或位于其 CWD 的"有限
JSON 配置文件，并按白名单提取字段名/名称。本地读取与脱敏外发是两回事：read_text
在本机读取文件解析字段，但结果只保留文件名与选定字段，原始内容与凭据绝不外发。
"""
from pathlib import Path
import json
import psutil
from runtime.identity import metadata_identity

_JSON_KEYS = ('config', 'setting', 'mcp')
_CWD_FILES = ('config.json', 'settings.json', 'AGENTS.md', 'CLAUDE.md')


def collect(p):
    info = {'pid': p.pid, 'exe': p.exe(), 'cmdline': p.cmdline(), 'name': p.name()}
    identity = metadata_identity(info)
    assets = {}
    candidates = set()
    sys_errors = []       # psutil 系统错误（无权限/进程退出）: 不覆盖已采集内容
    cfg_failures = []     # 配置解析失败: 逐文件记录，保留已成功项
    try:
        candidates.update(Path(f.path) for f in p.open_files())
    except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
        sys_errors.append(type(exc).__name__)
    try:
        cwd = Path(p.cwd())
        candidates.update(cwd / n for n in _CWD_FILES)
    except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
        sys_errors.append(type(exc).__name__)
    observations = []
    cfg_parse_ok = 0
    for path in sorted(candidates)[:100]:
        # 仅处理白名单规则路径；.env 与任意命令行路径从不读取
        if path.name in ('AGENTS.md', 'CLAUDE.md') and path.is_file():
            observations.append({'field': 'system_prompt_rules', 'value': [path.name], 'source': str(path)})
        if path.suffix != '.json' or not any(t in path.name.lower() for t in _JSON_KEYS):
            continue
        try:
            if not path.is_file():
                continue
            if path.stat().st_size > 262144:
                cfg_failures.append(path.name + ':size_limit')
                continue
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                cfg_failures.append(path.name + ':not_object')
                continue
            cfg_parse_ok += 1
            observations.append({'field': 'parsed_config', 'value': {'file': path.name}, 'source': str(path)})
            model = data.get('model')
            if isinstance(model, str):
                observations.append({'field': 'model_routing', 'value': {'model': model[:160]}, 'source': str(path)})
            for key in ('mcpServers', 'mcp'):
                if key in data and isinstance(data[key], dict):
                    observations.append({'field': 'registered_tools_and_mcp', 'value': list(data[key])[:50], 'source': str(path)})
            if 'instructions' in data and isinstance(data['instructions'], list):
                # 只声明存在配置化指令，不外发实际内容或路径
                observations.append({'field': 'system_prompt_rules', 'value': ['configured instruction reference'] * min(len(data['instructions']), 50), 'source': str(path)})
        except (OSError, ValueError) as exc:
            cfg_failures.append(path.name + ':' + type(exc).__name__)
    for obs in observations:
        key = obs['field']
        msg = '本机读取受限 JSON 解析所得字段名，不外发原始内容/凭据'
        if key not in assets:
            assets[key] = {'status': 'collected', 'value': obs['value'], 'source': obs['source'], 'message': msg}
        elif isinstance(obs['value'], list):
            assets[key]['value'] += obs['value']
    # 局部失败：保留已采集成功项，仅在 message 中说明；全部失败才 failed
    if cfg_failures:
        partial = '部分配置解析失败: ' + ', '.join(sorted(set(cfg_failures))[:8])
        parsed = assets.get('parsed_config', {'status': 'failed', 'source': 'bound-process-config-scan', 'message': ''})
        if cfg_parse_ok > 0 and parsed.get('status') == 'collected':
            parsed['message'] = '已保留成功项；' + partial
            assets['parsed_config'] = parsed
        else:
            assets['parsed_config'] = {'status': 'failed', 'source': 'bound-process-config-scan',
                                       'message': partial if not sys_errors else partial + '；' + ', '.join(sorted(set(sys_errors))[:4])}
    elif sys_errors and 'parsed_config' not in assets:
        assets['parsed_config'] = {'status': 'failed', 'source': 'bound-process-config-scan',
                                   'message': '配置目录不可读: ' + ', '.join(sorted(set(sys_errors))[:4])}
    try:
        connections = p.net_connections(kind='inet')
        assets['network_surface'] = {'status': 'collected', 'source': 'psutil.net_connections:target-only',
            'value': [{'status': c.status, 'local_port': c.laddr.port if c.laddr else None,
                       'remote_port': c.raddr.port if c.raddr else None} for c in connections[:100]],
            'message': '仅当前目标进程瞬时快照，不涵盖子进程或历史；不代表安全结论'}
    except (psutil.AccessDenied, psutil.NoSuchProcess) as exc:
        assets['network_surface'] = {'status': 'failed', 'source': 'psutil.net_connections:target-only',
                                     'message': '观测未完成: ' + type(exc).__name__ + '（无数据≠无连接，更≠安全）'}
    return {'identity': identity, 'assets': assets,
            'limitations': ['仅本机读取受限 JSON 配置解析字段名；原始配置内容/凭据只读不导出（read_text 为本地读取，外发结果已脱敏）',
                            '未覆盖: 非 JSON 配置、任意路径参数、.env、插件/市场集合、依赖版本、网络行为',
                            '字段缺失 = 未采集，不是空值']}
