"""Local identity evidence and process ownership, independent of LLM recipes."""
import re
import json
import plistlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
import psutil


def load_catalog():
    data = yaml.safe_load((Path(__file__).parents[1] / 'identities.yaml').read_text())
    if os.environ.get('ASG_IDENTITY_HINTS', '1') == '0':
        data['agents'] = []
    return data


def entrypoints(info):
    argv = info.get('cmdline') or []
    values = [info.get('exe') or '', info.get('name') or '', argv[0] if argv else '']
    # Only the executable and actual interpreter entrypoint count as identity.
    # Prompts, config values, and arbitrary later arguments never count.
    exe = Path(values[-1]).name.lower().removesuffix('.exe')
    if exe in {'node', 'bun', 'python', 'python3', 'python3.9', 'python3.11', 'python3.12'}:
        for i, arg in enumerate(argv[1:], 1):
            if arg == '-m' and i + 1 < len(argv):
                values.append(argv[i + 1])
                break
            if arg in {'-c', '-e', '--eval'}:
                break
            if not arg.startswith('-'):
                values.append(arg)
                break
    return [v.replace('\\', '/') for v in values if v]


def discovery_probe_allowed(info):
    """Limit expensive probes to plausible user-installed software.

    A full process table includes hundreds of OS daemons. Reading package
    manifests, open files, children, and sockets for all of them means one slow
    filesystem path can stall the complete inventory. This gate uses paths and
    generic protocol flags only; it contains no Agent product names.
    """
    argv = info.get('cmdline') or []
    flags = {str(arg).split('=', 1)[0] for arg in argv[1:]
             if isinstance(arg, str) and arg.startswith('-')}
    if flags & {
        '--model', '--model-provider', '--provider', '--api-base', '--base-url',
        '--prompt', '--system-prompt', '--task', '--instructions',
        '--allowed-tools', '--disallowed-tools', '--permission-mode',
        '--approval-mode', '--max-turns', '--mcp-config', '--jsonl',
    }:
        return True

    values = entrypoints(info)
    if not values:
        return True
    home = str(Path.home()).replace('\\', '/').rstrip('/') + '/'
    user_roots = (
        home, '/Applications/', '/opt/', '/usr/local/', '/private/var/folders/',
        '/var/folders/',
    )
    windows_markers = ('/users/', '/program files/', '/appdata/', '/local programs/')
    for value in values:
        normalized = str(value).replace('\\', '/')
        lower = normalized.lower()
        if normalized.startswith(user_roots) or any(marker in lower for marker in windows_markers):
            return True
    return False


def identify(info, catalog):
    for rule in catalog.get('agents', []):
        for value in entrypoints(info):
            if any(re.search(pattern, value, re.I) for pattern in rule['patterns']):
                return {'id': rule['id'], 'name': rule['name'],
                        'source': 'local-entrypoint', 'evidence': value}
    return {}


PROTOCOL_DEPENDENCY_TOKENS = {'mcp', 'acp', 'modelcontextprotocol'}


def declares_protocol_ecosystem(dependency_names):
    """Dependency names that carry MCP/ACP as a whole token.

    Membership of the open MCP/ACP protocol ecosystem is capability evidence
    for any harness that embeds these client stacks, without naming vendors.
    Names are token-split on separators, so lookalikes like 'mcpkit' never
    match, and no prompt or config value is ever consulted.
    """
    hits = []
    for name in dependency_names:
        tokens = set(re.split(r'[-./@_]+', str(name).lower()))
        if PROTOCOL_DEPENDENCY_TOKENS & tokens:
            hits.append(str(name))
    return hits


def metadata_identity(info):
    """Read bounded metadata beside the real entry, never prompts or user configs.

    A display name alone says nothing about whether the program is an Agent.
    model_sdk indicates declared capability, not proof of an active model call.
    """
    script_suffixes = {'.cjs', '.js', '.mjs', '.py', '.pyw', '.rb', '.sh', '.ts', '.tsx'}

    def resolved(value):
        try:
            return Path(value).resolve(strict=False)
        except (OSError, ValueError):
            return Path(value)

    def bin_targets(data, parent):
        value = data.get('bin')
        if isinstance(value, str):
            return [parent / value]
        if isinstance(value, dict):
            return [parent / target for target in value.values() if isinstance(target, str)]
        return []

    for value in reversed(entrypoints(info)):
        path = Path(value)
        if not path.is_absolute():
            continue
        real_path = resolved(path)
        for parent in list(real_path.parents)[:10]:
            package = parent / 'package.json'
            try:
                if package.is_file() and package.stat().st_size < 262144:
                    data = json.loads(package.read_text())
                    if isinstance(data.get('name'), str):
                        mapped = any(resolved(target) == real_path for target in bin_targets(data, parent))
                        script_entry = real_path.suffix.lower() in script_suffixes
                        try:
                            script_under_package = real_path.is_relative_to(parent)
                        except AttributeError:
                            script_under_package = str(real_path).startswith(str(parent) + os.sep)
                        if not mapped and not (script_entry and script_under_package):
                            # An outer toolchain manifest is not ownership proof for
                            # a native child executable.  Continue towards a nearer
                            # owning package or bundle.
                            continue
                        deps = data.get('dependencies') or {}
                        sdk = any(k in deps for k in ('openai', '@anthropic-ai/sdk', '@ai-sdk/openai', 'litellm', '@langchain/core'))
                        protocol_hits = declares_protocol_ecosystem(deps.keys())
                        return {'id': 'package:' + str(parent), 'name': data.get('productName') or data['name'],
                                'source': 'package-metadata', 'evidence': str(package),
                                'version': data.get('version', 'unknown'), 'model_sdk': sdk,
                                'protocol_dependencies': protocol_hits,
                                'entrypoint': str(path), 'resolved_entrypoint': str(real_path),
                                'ownership': 'bin-mapping' if mapped else 'script-under-package'}
            except (OSError, ValueError, TypeError):
                pass
        # Outermost bundle identifies the host, not each Electron helper bundle.
        bundle = next((p for p in reversed(real_path.parents) if p.suffix == '.app'), None)
        if bundle:
            manifest = bundle / 'Contents' / 'Info.plist'
            try:
                if manifest.stat().st_size > 262144:
                    continue
                data = plistlib.loads(manifest.read_bytes())
                name = data.get('CFBundleDisplayName') or data.get('CFBundleName')
                if name:
                    return {'id': 'bundle:' + str(bundle), 'name': name,
                            'source': 'bundle-metadata', 'evidence': str(manifest),
                            'version': data.get('CFBundleShortVersionString', 'unknown'),
                            'entrypoint': str(path), 'resolved_entrypoint': str(real_path),
                            'ownership': 'app-bundle'}
            except (OSError, ValueError, plistlib.InvalidFileException):
                pass
    return {}


_METADATA_CACHE = {}
_METADATA_CACHE_LOCK = threading.Lock()
_METADATA_CACHE_TTL = 60.0


def metadata_identity_bounded(info, timeout=1.0):
    """Read package ownership in a killable helper process.

    macOS can leave a regular-file ``open`` blocked indefinitely while a
    package tree is being changed or inspected by another filesystem filter.
    A Python thread cannot cancel that syscall.  Keeping the read in a helper
    process gives the inventory a real deadline without weakening unknown
    Agent discovery or hard-coding package names.
    """
    safe_info = {
        'pid': info.get('pid'),
        'name': str(info.get('name') or '')[:1024],
        'exe': str(info.get('exe') or '')[:4096],
        'cmdline': [str(value)[:4096] for value in (info.get('cmdline') or [])[:16]],
    }
    key = tuple(entrypoints(safe_info))
    now = time.monotonic()
    with _METADATA_CACHE_LOCK:
        cached = _METADATA_CACHE.get(key)
        if cached and now - cached[0] <= _METADATA_CACHE_TTL:
            return dict(cached[1])
    code = (
        'import json,sys; from runtime.identity import metadata_identity; '
        'print(json.dumps(metadata_identity(json.load(sys.stdin))))'
    )
    try:
        completed = subprocess.run(
            [sys.executable, '-c', code],
            input=json.dumps(safe_info), text=True, capture_output=True,
            timeout=max(0.05, float(timeout)), cwd=str(Path(__file__).parents[1]),
            check=True,
        )
        result = json.loads(completed.stdout)
        if not isinstance(result, dict):
            result = {}
    except (OSError, ValueError, TypeError, subprocess.SubprocessError, json.JSONDecodeError):
        result = {}
    with _METADATA_CACHE_LOCK:
        if len(_METADATA_CACHE) >= 512:
            oldest = min(_METADATA_CACHE, key=lambda item: _METADATA_CACHE[item][0])
            _METADATA_CACHE.pop(oldest, None)
        _METADATA_CACHE[key] = (now, dict(result))
    return result


def runtime_discovery_candidate(info, process):
    """Independent evidence families admit investigation, never assert a role."""
    signals = []
    collection = {}

    def add(kind, evidence, reason):
        if not any(s['source'] == kind for s in signals):
            signals.append({'source': kind, 'evidence': evidence, 'reason': reason})

    metadata = metadata_identity_bounded(info)
    if metadata.get('ownership') == 'bin-mapping':
        add('package-bin-mapping', metadata['evidence'], '入口与安装包 bin 声明一致')
    if metadata.get('model_sdk'):
        add('declared-model-sdk', metadata['evidence'], '入口包声明模型 SDK')
    if metadata.get('protocol_dependencies'):
        # Embedding an MCP/ACP client stack is protocol-ecosystem capability
        # evidence (open standards, no vendor names): admit for role
        # investigation only, never assert Agent.
        add('protocol-ecosystem-dependency', metadata['protocol_dependencies'][:4],
            '安装包声明 MCP/ACP 协议栈依赖')
    argv = info.get('cmdline') or []
    flags = {arg.split('=', 1)[0] for arg in argv[1:] if isinstance(arg, str) and arg.startswith('--')}
    families = {
        'model-options': ({'--model', '--model-provider', '--provider', '--api-base', '--base-url'}, '模型或服务配置参数'),
        'task-options': ({'--prompt', '--system-prompt', '--task', '--instructions'}, '任务或指令参数'),
        'tool-control-options': ({'--allowed-tools', '--disallowed-tools', '--permission-mode', '--approval-mode', '--max-turns', '--mcp-config'}, '工具、MCP 或执行治理参数'),
    }
    for kind, (names, label) in families.items():
        matched = sorted(flags & names)
        if matched:
            add(kind, matched, label)
    try:
        opened_files = process.open_files()
        collection['open_files'] = {'status': 'collected', 'truncated': len(opened_files) > 80}
        for opened in opened_files[:80]:
            path = Path(opened.path)
            if path.name == 'SKILL.md':
                add('opened-standard-asset', str(path), '实际打开标准 Skill 文档')
            elif path.name in {'AGENTS.md', 'AGENTS.override.md'}:
                add('opened-agent-instructions', str(path), '实际打开 Agent 指令文档')
            elif path.name in {'.mcp.json', 'mcp.json', 'mcp_config.json'}:
                add('opened-mcp-configuration', str(path), '实际打开 MCP 配置候选文件')
    except (OSError, AttributeError, TypeError, psutil.Error) as exc:
        collection['open_files'] = {'status': 'unavailable', 'reason': type(exc).__name__}
    try:
        children = process.children(recursive=False)
        collection['children'] = {'status': 'collected', 'truncated': len(children) > 40}
        for child in children[:40]:
            try:
                args = child.cmdline()
                # Protocol/launcher structure only; never inspect prompt values.
                entries = entrypoints({'cmdline': args, 'name': child.name()})
                if any('mcp-server' in Path(e).name or '@modelcontextprotocol/' in e for e in entries):
                    add('mcp-child', {'pid': child.pid}, '派生 MCP 工具服务进程')
            except (OSError, psutil.Error):
                continue
    except (OSError, AttributeError, TypeError, psutil.Error) as exc:
        collection['children'] = {'status': 'unavailable', 'reason': type(exc).__name__}
    desktop = desktop_discovery_candidate(info)
    if desktop:
        add(desktop['source'], desktop['evidence'], '桌面主程序归属已核实')
    # Established outbound transport is a capability signal only when the
    # entry is owned by a resolvable script package (bin mapping / in-package
    # script). Native .app bundles and opaque system binaries keep no such
    # claim, so browsers and desktop apps never gain points from mere traffic.
    if metadata.get('ownership') in ('bin-mapping', 'script-under-package'):
        try:
            conns = process.net_connections(kind='inet')
            remote = sorted({f'{c.raddr.ip}:{c.raddr.port}' for c in conns
                             if c.status == 'ESTABLISHED' and c.raddr})
            collection['net'] = {'status': 'collected', 'established': len(remote)}
            if remote:
                add('model-transport-connection', remote[:4],
                    '安装包归属进程保持远端服务连接')
        except (AttributeError, OSError, TypeError, psutil.Error) as exc:
            collection['net'] = {'status': 'unavailable', 'reason': type(exc).__name__}
    # Ownership is locating evidence, never Agent capability evidence. A plain
    # desktop app or any npm CLI must not enter automatic investigation alone.
    capability_signals = [s for s in signals if s['source'] not in
                          {'package-bin-mapping', 'desktop-main-executable'}]
    if not capability_signals:
        return {}
    return {**signals[0], 'signals': signals, 'collection': collection,
            'reason': '；'.join(s['reason'] for s in signals) + '；待调查角色（非 Agent 确认）'}


def desktop_discovery_candidate(info):
    """Admit a verified non-system desktop main executable for role investigation.

    No AI identity or behavioral score is inferred from bundle ownership.
    Helpers and background services are not independent desktop candidates.
    """
    try:
        executable = Path(info.get('exe') or '').resolve()
        if not executable.is_absolute() or str(executable).startswith('/System/'):
            return {}
        # Framework-hosted interpreter executables are runtime containers, not
        # independently launched desktop applications (e.g. Python.framework).
        if any(parent.suffix == '.framework' for parent in executable.parents):
            return {}
        bundle = next((p for p in reversed(executable.parents) if p.suffix == '.app'), None)
        if bundle is None:
            return {}
        manifest = bundle / 'Contents' / 'Info.plist'
        if manifest.stat().st_size > 262144:
            return {}
        data = plistlib.loads(manifest.read_bytes())
        entry = data.get('CFBundleExecutable')
        if not isinstance(entry, str) or Path(entry).name != entry:
            return {}
        if data.get('LSBackgroundOnly') or data.get('LSUIElement'):
            return {}
        if executable != (bundle / 'Contents' / 'MacOS' / entry).resolve():
            return {}
        return {'source': 'desktop-main-executable', 'evidence': str(manifest),
                'reason': '桌面主程序归属已核实；行为证据不足，待调查角色（非 Agent 确认）'}
    except (OSError, ValueError, TypeError, plistlib.InvalidFileException):
        return {}


def structural_score(info, children, metadata):
    """Independent capability signals for previously unseen orchestration hosts."""
    reasons = []
    points = 0
    if metadata.get('model_sdk'):
        points += 30
        reasons.append('入口包声明模型 SDK (+30)')
    # Generic CLI subprocess role: no vendor/product name is inspected.
    cli_children = [c for c in children if any(re.search(r'(^|/)[^/\s]+-cli(?:\.exe)?$', e) for e in entrypoints(c))]
    if cli_children:
        points += 35
        reasons.append('派生独立 CLI 执行器 (+35)')
    if points and children:
        points += 15
        reasons.append('运行时子进程编排 (+15)')
    if points and metadata:
        points += 5
        reasons.append('入口包/应用元数据可追溯 (+5)')
    return points, reasons


def ownership(snapshot, identities, candidates):
    """Nearest candidate owns helpers; different named agents remain separate."""
    roots = {}
    for pid in candidates:
        root, current, seen = pid, snapshot[pid].get('ppid'), {pid}
        while current in snapshot and current not in seen:
            seen.add(current)
            if current in candidates:
                own = identities.get(pid, {}).get('id')
                parent = identities.get(current, {}).get('id')
                if own != parent:
                    break
                root = current
            current = snapshot[current].get('ppid')
        roots[pid] = root
    members = {root: [] for root in set(roots.values())}
    for pid in snapshot:
        current, seen = pid, set()
        while current in snapshot and current not in seen:
            seen.add(current)
            if current in roots:
                members[roots[current]].append(pid)
                break
            current = snapshot[current].get('ppid')
    return members
