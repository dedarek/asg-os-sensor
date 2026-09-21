"""Try a reusable command Hook dialect before spending a Goose investigation.

This is a reversible compatibility attempt, not a product-name adapter. Only
process-related, parsed settings with concrete command-Hook structures enter.
Missing channels and failed activation return to the existing Goose pipeline.
"""
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

import psutil

from runtime import autonomous_pipeline as pipeline, learned_install, onboarding
from runtime.integration_protocol import detect


def install(config_path, target, root, control_client):
    """Transactional install with exact file preconditions and deployment scope."""
    if Path(config_path).is_symlink(): raise ValueError('symlink configuration is not a direct protocol target')
    config_path, root = Path(config_path).resolve(), Path(root).resolve()
    workspace = config_path.parent
    auth = onboarding.authorization_from_environment(str(workspace))
    if not auth.get('approved') or auth.get('scope') != 'project':
        return {'status': 'pending_authorization', 'reason': '协议配置目录不在部署授权范围内'}
    onboarding._target_is_live(target)
    if config_path.is_symlink() or config_path.stat().st_size > 262144:
        raise ValueError('configuration outside bounded JSON scope')
    original = config_path.read_bytes()
    from runtime.protocol_discovery import read_config, serialize_config
    settings = read_config(config_path)
    candidates = detect(settings)
    if not any(c['family'] == 'command_hooks' and c['pointer'] == '/hooks' for c in candidates):
        raise ValueError('no top-level compatible command Hook structure')
    hooks = settings['hooks']
    directory = workspace / '.asg-command-protocol'
    runtime_config = directory / 'client.json'
    if runtime_config.exists():
        old = json.loads(runtime_config.read_text())
        targets = old.get('targets', [])
    else: targets = []
    # Exact identities only. Do not grow the file indefinitely with old PIDs.
    live = []
    for t in targets:
        try:
            if abs(psutil.Process(t['pid']).create_time() - t['create_time']) < .001: live.append(t)
        except (psutil.Error, KeyError, TypeError): pass
    if target not in live: live.append(target)
    runner = directory / 'hook.py'
    runner_source = Path(__file__).with_name('command_protocol_hook.py').read_text()
    if runner.exists() and runner.read_text() != runner_source:
        raise ValueError('existing protocol runtime differs; preserve it and investigate compatibility')
    command_args = [sys.executable, str(runner), str(runtime_config)]
    command = subprocess.list2cmdline(command_args) if os.name == 'nt' else shlex.join(command_args)
    from runtime.command_protocol_hook import EVENTS
    supported = []
    for event in EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list) or not entries: continue
        # Never mix incompatible group/direct forms in one event array.
        grouped = all(isinstance(v, dict) and isinstance(v.get('hooks'), list) for v in entries)
        direct = all(isinstance(v, dict) and v.get('type') == 'command' for v in entries)
        if not grouped and not direct: continue
        commands = [c for v in entries for c in (v['hooks'] if grouped else [v]) if isinstance(c, dict)]
        if not any(c.get('command') == command for c in commands):
            entry = {'type': 'command', 'command': command}
            entries.append({'matcher': '.*', 'hooks': [entry]} if grouped else entry)
        supported.append(event)
    if not supported: raise ValueError('no supported command event layout')
    contents = {config_path: serialize_config(settings, config_path),
                runner: runner_source,
                runtime_config: json.dumps({'targets': live, 'log_path': str(directory / 'events.jsonl'),
                                           'control_client': control_client})}
    files = []
    for file, content in contents.items():
        before = file.read_bytes() if file.exists() else None
        files.append({'path': file.relative_to(workspace).as_posix(), 'content': content,
                      'expected_sha256': hashlib.sha256(before).hexdigest() if before is not None else None})
    plan = {'version': 1, 'files': files}
    iid = onboarding.make_instance_id(target['pid'], target['create_time'])
    state_dir = root / 'protocol-fastpath' / iid.replace(':', '_')
    state_dir.mkdir(parents=True, exist_ok=True)
    result = learned_install.install(plan, workspace, state_dir, approved_workspace=workspace,
                                    approved_digest=learned_install.plan_digest(plan))
    binding = state_dir / 'observation.json'
    learned_install._atomic(binding, json.dumps({'target': target, 'log_path': str(directory / 'events.jsonl'),
        'fields': {'event': 'event', 'pid': 'pid', 'timestamp': 'timestamp', 'tool': 'tool', 'call_id': 'call_id'}}).encode())
    from runtime.observation_registry import Registry
    Registry(root).register(binding, target)
    return {**result, 'family': 'command_hooks', 'source': 'protocol_fastpath',
            'observe_config': str(binding), 'supported_native_events': supported,
            'missing_capabilities': ['model.request', 'model.response', 'io.turn.end', 'independent_control_effect'],
            'workspace': str(workspace), 'state_dir': str(state_dir), 'installed_at': time.time(),
            'reason': '协议模板已安装；等待当前目标真正执行回调，未继承模板测试结果'}


def install_acp(config_path, target, root, control_config):
    """Wrap declared ACP client launch entries; handshake still belongs to child."""
    from runtime.protocol_discovery import read_config, serialize_config
    config_path = Path(config_path)
    if config_path.is_symlink(): raise ValueError('symlink ACP configuration')
    config_path = config_path.resolve(); workspace = config_path.parent
    auth = onboarding.authorization_from_environment(str(workspace))
    if not auth.get('approved') or auth.get('scope') != 'project':
        return {'status': 'pending_authorization', 'reason': 'ACP 配置目录不在部署授权范围内'}
    onboarding._target_is_live(target)
    original = config_path.read_bytes(); settings = read_config(config_path)
    entries = settings.get('agent_servers')
    if not isinstance(entries, dict) or not entries: raise ValueError('no ACP agent_servers declarations')
    bridge = str(Path(__file__).with_name('acp_bridge.py').resolve())
    wrapped = []
    for name, entry in entries.items():
        if not isinstance(entry, dict): continue
        command, args = entry.get('command'), entry.get('args', [])
        if not isinstance(command, str) or not command or not isinstance(args, list) or not all(isinstance(v, str) for v in args): continue
        if bridge in args: continue
        entry['command'] = sys.executable
        entry['args'] = [bridge, '--run-dir', str(Path(root).resolve()), '--control-config', str(control_config), '--', command, *args]
        wrapped.append(name)
    if not wrapped: return {'status': 'fallback', 'reason': 'ACP 声明不支持直接包装，或已经包装'}
    plan = {'version': 1, 'files': [{'path': config_path.name, 'content': serialize_config(settings, config_path),
                                  'expected_sha256': hashlib.sha256(original).hexdigest()}]}
    iid = onboarding.make_instance_id(target['pid'], target['create_time'])
    state_dir = Path(root) / 'protocol-fastpath' / iid.replace(':', '_') / 'acp'
    state_dir.mkdir(parents=True, exist_ok=True)
    result = learned_install.install(plan, workspace, state_dir, approved_workspace=workspace,
                                    approved_digest=learned_install.plan_digest(plan))
    return {**result, 'family': 'acp', 'source': 'protocol_fastpath', 'workspace': str(workspace),
            'state_dir': str(state_dir), 'wrapped_entries': wrapped, 'installed_at': time.time(),
            'verified': False, 'reason': 'ACP 启动配置已包装；下次启动子 Agent 后独立握手，既有桌面会话不继承验证',
            'missing_capabilities': ['actual_child_handshake', 'io_reconciliation', 'independent_control_effect']}


def attempt(target, *, force=False):
    """Discover protocol candidates; installation belongs to the SOC package flow.

    A matching JSON shape is not loader or dialect compatibility evidence.
    Legacy local runtimes must never suppress investigation or claim SOC direct
    readiness, even when they have produced a real local callback.
    """
    iid = onboarding.make_instance_id(target['pid'], target['create_time'])
    prior = pipeline.read(iid).get('protocol_fastpath') or {}
    try:
        from runtime.protocol_discovery import discover
        process = psutil.Process(target['pid'])
        if abs(process.create_time() - target['create_time']) >= .001:
            raise ValueError('target changed')
        surface = discover(process)
        result = {'status': 'fallback', 'handled': False, 'verified': False,
                  'route': 'soc_package_pipeline',
                  'reason': '协议线索交给调查与 SOC 制品流程；确认加载位置和兼容性后生成直连安装包',
                  'protocol_discovery': surface,
                  'candidate_count': len(surface.get('candidates', []))}
        if prior.get('status') in ('installed', 'already_installed'):
            result['legacy_local_install'] = {k: prior[k] for k in
                ('state_dir', 'observe_config', 'installed_at', 'workspace') if k in prior}
            result['missing_checks'] = ['soc_direct_transport', 'soc_package_registration',
                                        'fresh_instance_callback']
        elif prior.get('legacy_local_install'):
            result['legacy_local_install'] = prior['legacy_local_install']
            result['missing_checks'] = prior.get('missing_checks', [])
    except (OSError, ValueError, KeyError, psutil.Error) as exc:
        result = {'status': 'fallback', 'handled': False, 'verified': False, 'reason': str(exc)}
    pipeline.save(iid, 'protocol_fastpath', '', **result)
    return result
