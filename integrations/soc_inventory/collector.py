"""Refresh concrete resource paths learned by ASG, without model calls.

Input is an explicitly selected, retained investigation. Its ownership is historical;
refreshing bytes does NOT prove a current process loads the resource. No commands or
MCP servers are executed. Only structured evidence paths are accepted.
"""
import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

LIMIT = 1024 * 1024


def digest(value):
    data = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with path.open('rb') as stream:
        value = stream.read(LIMIT + 1)
    if len(value) > LIMIT:
        raise ValueError('file_size_limit')
    return value


def structured(record):
    value = record.get('value') or {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def absolute(value):
    return isinstance(value, str) and value.startswith('/') and '\n' not in value and len(value) < 2048


def manifest(data, fallback):
    import yaml
    text = data.decode('utf-8')
    head = {}
    if text.startswith('---'):
        head = yaml.safe_load(text.split('---', 2)[1]) or {}
    if not isinstance(head, dict):
        head = {}
    meta = head.get('metadata') if isinstance(head.get('metadata'), dict) else {}
    return {'name': str(head.get('name') or fallback),
            'description': str(head.get('description') or '')[:1200],
            'declared_version': str(head.get('version') or meta.get('version') or '') or None,
            'validation_status': 'valid' if head.get('name') and head.get('description') else 'invalid'}


def mcp_definitions(data):
    """Recognize declaration structure, not an Agent brand."""
    if not isinstance(data, dict):
        return
    for key, value in data.items():
        if key in ('mcpServers', 'mcp_servers', 'mcp') and isinstance(value, dict):
            servers = value.get('servers', value)
            for name, config in servers.items():
                if isinstance(config, dict) and any(k in config for k in ('command', 'url', 'transport', 'type')):
                    yield str(name), config
        elif isinstance(value, dict):
            yield from mcp_definitions(value)


def safe_definition(config):
    # Command arguments and arbitrary configuration values are intentionally not
    # exported in v1: they may contain secrets without recognizable key names.
    result = {'enabled': config.get('enabled', not config.get('disabled', False)),
              'transport': str(config.get('transport') or config.get('type') or 'unknown')}
    command = config.get('command')
    executable = command[0] if isinstance(command, list) and command else command
    if isinstance(executable, str):
        result['command'] = executable.split()[0]
        if result['transport'] in ('unknown', 'local'):
            result['transport'] = 'stdio'
    url = config.get('url')
    if isinstance(url, str):
        parts = urlsplit(url)
        # Only origin: even URL paths can embed credentials.
        result['url'] = urlunsplit((parts.scheme, parts.hostname or '', '', '', ''))
        if result['transport'] in ('unknown', 'remote', 'streamableHttp'):
            result['transport'] = 'http'
    result['credential_ref'] = sorted(set(k for field in ('env', 'headers', 'staticHeaders')
                                         for k in (config.get(field) or {}) if isinstance(config.get(field), dict)))
    return result


def collect(source, agent_id, label=None):
    source = Path(source)
    document = json.loads(source.read_text())
    findings = document.get('findings', {})
    identity = findings.get('identity', {}).get('value') or {}
    assets = findings.get('assets', {})
    now = time.time()
    report = {'schema_version': 'asg-soc-preview-v1', 'agent_id': agent_id,
              'name': label or identity.get('name') or agent_id, 'collected_at': now,
              'identity': {key: identity[key] for key in ('name','package_name','bundle_id','version') if key in identity},
              'source_file': str(source), 'source_target': document.get('target', {}),
              'source_time': source.stat().st_mtime, 'ownership': 'learned_investigation',
              'model_calls': 0, 'categories': {}}
    for category, key in (('skill', 'skills'), ('mcp_server', 'mcp')):
        record = assets.get(key) or assets.get('registered_tools_and_mcp' if key == 'mcp' else key) or {}
        value = structured(record)
        items = value.get('items', [])
        paths = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            path = item.get('path') if category == 'skill' else (item.get('source') or item.get('config_path') or item.get('path'))
            if absolute(path):
                paths[path] = item
        scopes = []
        for raw_path, original in sorted(paths.items()):
            path = Path(raw_path)
            if category == 'skill' and path.name != 'SKILL.md':
                path = path / 'SKILL.md'
            scope = {'scope_key': category + ':' + str(path), 'items': [], 'status': 'success'}
            try:
                data = read_bytes(path)
                if category == 'skill':
                    info = manifest(data, path.parent.name)
                    scope['items'].append({**info, 'installation_key': str(path), 'path': str(path),
                        'fingerprint': digest(data), 'fingerprint_version': 'skill-md-v1',
                        'enabled': original.get('enabled'), 'source_kind': original.get('source_kind', 'unknown'),
                        'evidence_refs': original.get('evidence_refs') or record.get('evidence_refs', [])})
                else:
                    if path.suffix == '.toml':
                        import tomlkit
                        config = tomlkit.parse(data.decode())
                    elif path.suffix in ('.yaml', '.yml'):
                        import yaml
                        config = yaml.safe_load(data)
                    else:
                        import json5
                        config = json5.loads(data.decode())
                    for name, definition in mcp_definitions(config):
                        safe = safe_definition(definition)
                        scope['items'].append({**safe, 'name': name, 'path': str(path),
                            'installation_key': str(path) + '#' + name,
                            'fingerprint': digest(definition), 'fingerprint_version': 'mcp-local-hash-v1',
                            'description': '', 'declared_version': None,
                            'evidence_refs': record.get('evidence_refs', [])})
            except FileNotFoundError:
                # An explicitly learned file now absent is a complete empty scope.
                scope['removed'] = True
            except Exception as exc:
                scope['status'] = 'failed'
                scope['error'] = type(exc).__name__
            scopes.append(scope)
        report['categories'][category] = {'status': 'partial' if scopes else 'not_collected',
            'scope_note': '仅复查调查已定位的资源文件；不表示全部资源或当前会话已加载', 'scopes': scopes}
    return report
