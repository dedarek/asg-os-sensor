"""Create a portable direct-SOC package from proven command-Hook structure."""
import hashlib
import json
import platform
from pathlib import Path
import shlex
from datetime import datetime, timezone

from runtime.compatibility import observe
from runtime.recipe_bundle import digest, PLACEHOLDER_WORKSPACE
from runtime.protocol_discovery import discover, read_config, serialize_config


def prepare(process):
    """Return a bundle only when the live process itself opened the JSON config.

    Directory/name matches are investigation leads.  They never authorize a
    generated package.  The first implementation intentionally accepts plain
    JSON only so comments and TOML formatting cannot be destroyed by a rewrite.
    """
    surface = discover(process)
    candidates = [item for item in surface.get('candidates', [])
                  if item.get('family') == 'command_hooks'
                  and item.get('pointer') == '/hooks'
                  and item.get('loaded_by_process') is True]
    if not candidates:
        return None
    candidate = candidates[0]
    config_path = Path(candidate['source'])
    workspace = Path(process.cwd()).resolve()
    if config_path.suffix.lower() != '.json' or config_path.is_symlink():
        return None
    try:
        relative = config_path.resolve().relative_to(workspace)
    except ValueError:
        return None
    original = config_path.read_bytes()
    if len(original) > 262144:
        return None
    settings = read_config(config_path)
    hooks = settings.get('hooks')
    if not isinstance(hooks, dict):
        return None
    runner_rel = Path('.asg-soc-hook/command_hook.py')
    command = shlex.join(['python3', str(Path(PLACEHOLDER_WORKSPACE) / runner_rel)])
    supported = []
    for event in candidate.get('events', []):
        entries = hooks.get(event)
        if not isinstance(entries, list) or not entries:
            continue
        grouped = all(isinstance(item, dict) and isinstance(item.get('hooks'), list)
                      for item in entries)
        direct = all(isinstance(item, dict) and item.get('type') == 'command'
                     for item in entries)
        if not grouped and not direct:
            continue
        existing = [hook for item in entries
                    for hook in (item['hooks'] if grouped else [item])
                    if isinstance(hook, dict)]
        if not any(item.get('command') == command for item in existing):
            entry = {'type': 'command', 'command': command}
            entries.append({'matcher': '.*', 'hooks': [entry]} if grouped else entry)
        supported.append(event)
    if not supported:
        return None
    compatibility = observe(process.exe(), process.cmdline(), process.cwd())
    if not compatibility:
        return None
    runner = Path(__file__).with_name('soc_command_protocol_hook.py').read_text()
    recipe = {'agent_identity_name': 'protocol-discovered-agent',
              'integration': {'family': 'command_hooks', 'evidence_refs': [],
                              'missing_capabilities': ['fresh_instance_activation',
                                                       'real_execution_effect']},
              'hook': {'method': 'loaded command-Hook configuration',
                       'restart_required': 'unknown', 'capabilities': supported,
                       'verification': 'fresh hook.loaded plus chat/tool reconciliation',
                       'rollback': 'transaction manifest rollback',
                       'limitations': ['package is generated before activation verification']},
              'install_plan': {'version': 1, 'files': [
                  {'path': relative.as_posix(), 'content': serialize_config(settings, config_path),
                   'expected_sha256': hashlib.sha256(original).hexdigest()},
                  {'path': runner_rel.as_posix(), 'content': runner,
                   'expected_sha256': None}]},
              'observation_source': {'log_path': '.asg-soc-hook/events.jsonl',
                  'fields': {'event': 'event', 'pid': 'pid', 'timestamp': 'timestamp',
                             'tool': 'tool', 'call_id': 'call_id'}},
              'fallback': 'investigate if package installation or fresh callback fails'}
    identity = hashlib.sha256(json.dumps({'events': sorted(supported),
                              'compatibility': compatibility}, sort_keys=True).encode()).hexdigest()[:20]
    bundle = {'schema': 'asg-recipe-bundle.v1',
              'created_at': datetime.now(timezone.utc).isoformat(),
              'created_by': {'tool': 'asg-protocol-package', 'platform': platform.system(),
                             'architecture': platform.machine()},
              'fingerprint': {'id': 'protocol-command-hooks-' + identity,
                              'name': 'command_hooks', 'revision': 1},
              'recipe': recipe,
              'constraints': {'revision': 1, 'compatibility': compatibility,
                              'covers': ['executable content', 'entry content',
                                         'runtime', 'platform', 'loaded config structure'],
                              'not_covers': ['fresh activation', 'execution effect']},
              'verification': {'investigation_verified': True, 'hook_verified': False,
                               'recipe_source': 'deterministic_protocol',
                               'local_scope': 'receiver must independently verify activation'},
              'limitations': ['loaded configuration proves a loader surface, not callback success']}
    bundle['integrity'] = {'algorithm': 'sha256', 'digest': digest(bundle)}
    return bundle
