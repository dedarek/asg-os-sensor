"""Bounded local asset facts. Never export raw config, prompts or credentials.

采集范围是有限的（见 limitations）：只解析"目标进程打开过的、或位于其 CWD 的"有限
JSON 配置文件，并按白名单提取字段名/名称。本地读取与脱敏外发是两回事：read_text
在本机读取文件解析字段，但结果只保留文件名与选定字段，原始内容与凭据绝不外发。
"""
from pathlib import Path
import json
import os
import psutil
from runtime.identity import metadata_identity

_JSON_KEYS = ('config', 'setting', 'mcp')
_CWD_FILES = ('config.json', 'settings.json', 'AGENTS.md', 'CLAUDE.md')


def _declared_config_roots(p, info):
    """Config roots for the convention scan, strongest evidence first.

    1. Environment-declared roots: *_HOME / *_CONFIG_DIR / *_CONFIG_PATH the
       process itself exports. A relocated HOME applies; the real user home
       alone never qualifies.
    2. Home fallback: ONLY when the process entry matched the local identity
       catalog (identities.yaml), scan the catalog-matching dot-directory
       under HOME (codex -> ~/.codex). This mirrors the SOC collection
       contract, which already derives these roots the same way; unidentified
       processes get no fallback, keeping zero-prior discovery brand-free.
    """
    roots, declared = [], []
    try:
        environ = p.environ()
    except (psutil.AccessDenied, psutil.NoSuchProcess, TypeError):
        return roots, declared
    if not isinstance(environ, dict):
        return roots, declared
    home = None
    for key, value in environ.items():
        if not value or key == 'PATH':
            continue
        if key in ('HOME', 'USERPROFILE'):
            candidate = Path(value).expanduser()
            if candidate.is_absolute() and candidate.is_dir():
                home = candidate
        relocated = key in ('HOME', 'USERPROFILE')
        if not (relocated or key.endswith(('_HOME', '_CONFIG_DIR', '_CONFIG_PATH'))):
            continue
        path = Path(value).expanduser()
        if not path.is_absolute() or (relocated and path == Path.home()):
            continue
        if path.is_dir() and path not in roots:
            roots.append(path)
            declared.append(str(path))
    if home is not None:
        from runtime.identity import identify, load_catalog
        identity_id = (identify(info, load_catalog()) or {}).get('id')
        if identity_id:
            fallback = home / ('.' + identity_id)
            if fallback.is_dir() and fallback not in roots:
                roots.append(fallback)
    return roots[:8], declared


def _skill_folder_entries(skills_dir, limit=200):
    """List installed skill folders: every child directory holding SKILL.md."""
    entries = []
    try:
        children = sorted(skills_dir.iterdir())
    except OSError:
        return entries, 'unreadable'
    for child in children[:limit]:
        if child.is_symlink() or not child.is_dir():
            continue
        manifest = child / 'SKILL.md'
        try:
            if not manifest.is_file() or manifest.stat().st_size > 262144:
                continue
        except OSError:
            continue
        name = child.name
        try:
            head = manifest.read_text(encoding='utf-8', errors='replace')[:8192]
            if head.startswith('---'):
                for line in head.split('---', 2)[1].splitlines():
                    if line.startswith('name:'):
                        name = line.split(':', 1)[1].strip() or name
                        break
        except OSError:
            pass
        entries.append({'name': name, 'path': str(manifest)})
    return entries, None


def _convention_config_files(root):
    """Well-known config filenames below an environment-declared root."""
    names = ('config.toml', 'config.json', 'config.jsonc', 'settings.json', 'settings.toml')
    return [(root / name) for name in names if (root / name).is_file()]


def _collect_convention_assets(p, info):
    """Layer-1 convention scan: skills folders and MCP declarations.

    Emits the same field keys the investigation layer uses so downstream SOC
    registration keeps working unchanged. Values carry names and paths only;
    file contents stay local.
    """
    found = {}
    skill_items, mcp_names = [], []
    mcp_files = []
    roots, declared = _declared_config_roots(p, info)
    if not roots:
        return found
    source = ('convention-scan:env-declared-roots' if declared and roots[0] in [Path(d) for d in declared]
              else 'convention-scan:home-fallback')
    for root in roots:
        entries, _ = _skill_folder_entries(root / 'skills')
        skill_items.extend(entries)
        mcp_files.extend(_convention_config_files(root))
    for path in mcp_files[:24]:
        try:
            if path.stat().st_size > 262144:
                continue
            text = path.read_text(encoding='utf-8', errors='replace')
            if path.suffix == '.toml':
                try:
                    import tomllib
                    data = tomllib.loads(text)
                except ImportError:
                    import tomlkit
                    data = tomlkit.parse(text)
            elif path.suffix == '.jsonc':
                import json5
                data = json5.loads(text)
            else:
                data = json.loads(text)
        except (OSError, ValueError, ImportError):
            continue
        if not isinstance(data, dict):
            continue
        for key in ('mcp_servers', 'mcpServers', 'mcp'):
            block = data.get(key)
            servers = block.get('servers') if isinstance(block, dict) and isinstance(block.get('servers'), dict) else block
            if isinstance(servers, dict):
                for name, definition in servers.items():
                    if isinstance(definition, dict):
                        mcp_names.append({'name': str(name), 'config_path': str(path)})
    if skill_items:
        found['skills'] = {'status': 'collected',
                           'value': {'items': skill_items, 'count': len(skill_items)},
                           'source': source,
                           'message': '按进程环境声明的配置目录扫描 skills 目录；仅名称与路径，未验证当前会话加载'}
    if mcp_names:
        found['registered_tools_and_mcp'] = {'status': 'collected',
                                             'value': {'items': mcp_names, 'count': len(mcp_names)},
                                             'source': source,
                                             'message': '按进程环境声明的配置文件解析 MCP 声明；仅名称与配置文件路径'}
    return found


def collect(p, workspace=None):
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
    # Explicit target workspace from the installation plan, not a global home scan.
    if workspace:
        root = Path(workspace)
        try:
            candidates.update(root / name for name in _CWD_FILES)
            for directory in sorted(root.iterdir())[:100]:
                if directory.is_dir() and not directory.is_symlink() and directory.name.startswith('.') and directory.name != '.git':
                    candidates.update(directory / name for name in ('config.json', 'settings.json', 'mcp.json', 'AGENTS.md'))
        except OSError as exc:
            sys_errors.append(type(exc).__name__)
    observations = []
    protocol_observations = []
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
            from runtime.integration_protocol import detect
            protocol_observations.extend({**v, 'source': str(path)} for v in detect(data))
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
    # Layer 1: platform-convention scan (no model). Roots come only from
    # environment-declared config dirs (CODEX_HOME etc.); processes without a
    # declared root are untouched, keeping zero-prior discovery brand-free.
    for key, value in _collect_convention_assets(p, info).items():
        assets.setdefault(key, value)
    # 第 1 层：平台约定路径扫描（无模型）。配置根目录只来自进程环境声明
    # （CODEX_HOME 等 *_HOME/*_CONFIG_DIR 变量），绝不盲扫用户 home；
    # 查不到的进程完全不进入本层，保持零先验发现不为品牌改规则。
    convention = _collect_convention_assets(p, info)
    for key, value in convention.items():
        if key not in assets:
            assets[key] = value
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
    return {'identity': identity, 'assets': assets, 'protocol_observations': protocol_observations,
            'limitations': ['仅本机读取受限 JSON 配置解析字段名；原始配置内容/凭据只读不导出（read_text 为本地读取，外发结果已脱敏）',
                            '未覆盖: 非 JSON 配置、任意路径参数、.env、插件/市场集合、依赖版本、网络行为',
                            '字段缺失 = 未采集，不是空值']}
