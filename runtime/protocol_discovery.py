"""Process-related protocol surfaces. No product-name inference or execution.

Parse JSON/JSONC/TOML and follow concrete config references. Only declarations
and field names leave this module; credentials and command bodies stay local.
"""
import json
import re
import psutil
from pathlib import Path
from runtime.integration_protocol import detect

CONFIG_NAMES = ('settings.json', 'settings.local.json', 'hooks.json', 'config.json',
                'config.jsonc', 'config.toml', 'settings.toml', 'acp.json', 'agents.json')


def read_config(path):
    path = Path(path)
    if path.stat().st_size > 512 * 1024: raise ValueError('configuration exceeds 512 KiB')
    text = path.read_text(encoding='utf-8-sig')
    if path.suffix == '.toml':
        import tomlkit
        return tomlkit.parse(text)
    if path.suffix == '.jsonc':
        import json5
        return json5.loads(text)
    return json.loads(text)


def serialize_config(value, path):
    if Path(path).suffix == '.toml':
        import tomlkit
        return tomlkit.dumps(value)
    return json.dumps(value, ensure_ascii=False, indent=2) + '\n'


def inspect_paths(paths, *, limit=96):
    pending = [(Path(p), 'process_related') for p in paths]
    seen, findings, errors = set(), [], []
    while pending and len(seen) < limit:
        path, relation = pending.pop(0)
        path = path.expanduser().resolve()
        if path in seen or not path.is_file(): continue
        if any(part in ('.ssh', '.gnupg', '.aws') for part in path.parts) or re.search(r'credential|secret|token|password|auth\.', path.name, re.I): continue
        seen.add(path)
        if path.suffix not in ('.json', '.jsonc', '.toml'): continue
        try:
            data = read_config(path)
            if not isinstance(data, dict): continue
            findings.extend({**v, 'source': str(path), 'relation': relation} for v in detect(data))
            # Configured OTel is evidence of a transport option, not GenAI content.
            if isinstance(data.get('otel'), dict) or isinstance(data.get('opentelemetry'), dict):
                findings.append({'family': 'otel', 'status': 'configured', 'source': str(path),
                                 'genai_verified': False, 'control_supported': False})
            if isinstance(data.get('agent_servers'), dict) or isinstance(data.get('acp'), dict):
                findings.append({'family': 'acp', 'status': 'configured', 'source': str(path),
                                 'handshake_verified': False})
            # Follow explicit hook config file references, not arbitrary values.
            hooks = data.get('hooks')
            if isinstance(hooks, dict):
                for key in ('files', 'config_files'):
                    for ref in hooks.get(key, []) if isinstance(hooks.get(key), list) else []:
                        if isinstance(ref, str):
                            linked = Path(ref).expanduser()
                            if not linked.is_absolute(): linked = path.parent / linked
                            pending.append((linked, str(path) + ':hooks.' + key))
        except (OSError, ValueError, ImportError) as exc:
            errors.append({'path': str(path), 'error': type(exc).__name__})
    return {'candidates': findings, 'checked_paths': [str(p) for p in sorted(seen)],
            'errors': errors, 'truncated': bool(pending), 'scope': 'bounded_process_related_configs'}


def discover(process):
    from runtime.analyst_evidence import entry_surface
    surface = entry_surface(process)
    paths = []
    for item in surface.get('opened_files', []):
        if item.get('resolved'): paths.append(Path(item['resolved']))
    roots = [Path(r['path']) for r in surface.get('related_roots', []) if r.get('path')]
    # Environment-declared app homes/config paths tie home locations to target.
    environment_error = None
    try:
        for key, value in process.environ().items():
            if key in ('HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'PATH'): continue
            if key.endswith(('_HOME', '_CONFIG_DIR', '_CONFIG_PATH')) and value:
                path = Path(value).expanduser()
                if path.is_absolute():
                    if path.is_file(): paths.append(path)
                    elif path.is_dir(): roots.append(path)
    except (OSError, psutil.Error) as exc:
        # psutil access denial must not discard other evidence.
        environment_error = type(exc).__name__
    for root in dict.fromkeys(roots):
        if root == Path.home() or root == Path(root.anchor): continue
        paths.extend(root / name for name in CONFIG_NAMES)
        # One level of concrete hidden project configuration directories.
        try:
            children = sorted(root.iterdir())[:100]
            for child in children:
                if child.name.startswith('.') and child.is_dir() and not child.is_symlink() and child.name not in ('.git', '.venv', '.ssh'):
                    paths.extend(child / name for name in CONFIG_NAMES)
        except OSError: pass
    result = inspect_paths(paths)
    if environment_error: result['errors'].append({'scope': 'process.environment', 'error': environment_error})
    result['target'] = {'pid': process.pid, 'create_time': process.create_time()}
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Inspect bounded process-related protocol declarations without invoking Goose or executing discovered commands')
    parser.add_argument('--pid', type=int, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(discover(psutil.Process(args.pid)), ensure_ascii=False, indent=2))
    except (psutil.Error, OSError, ValueError) as exc:
        parser.exit(1, 'Protocol discovery failed: ' + type(exc).__name__ + '\n')
