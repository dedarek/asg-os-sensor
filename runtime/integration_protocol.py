"""Protocol-family selection from observed structures, never product names.

Detection is a candidate, not proof that a host loads a configuration. No
discovered command is executed here. Existing installer and activation gates
remain responsible for installing and verifying the learned binding.
"""
import json

EVENTS = ('UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop', 'SessionStart')
# Structured alias spellings used by hosts that document them as equivalent
# to the names above; detection stays structural, never product-name based.
EVENT_ALIASES = {'BeforeAgent': 'UserPromptSubmit', 'BeforeTool': 'PreToolUse',
                 'AfterTool': 'PostToolUse', 'AfterAgent': 'Stop'}
FAMILIES = ('command_hooks', 'acp', 'plugin', 'unsupported')


def contract():
    return {'version': 1, 'priority': ['command_hooks', 'acp', 'plugin'],
            'families': {
                'command_hooks': 'JSON hooks[event] command entries, optionally nested in matcher groups; verify host input/output and exit semantics',
                'acp': 'JSON-RPC initialize protocolVersion/agentCapabilities; session/prompt, session/update and optional session/request_permission',
                'plugin': 'Use evidence-backed plugin entry points only for capabilities missing from existing channels'},
            'requirements': ['Inspect target-related configuration/source before selecting a channel',
                             'ACP needs an actual client/transport attachment; an executable name or acp flag is not a connected channel',
                             'MCP only covers routed tool calls; telemetry is observation, not execution control',
                             'Declare each missing capability; loading is not full IO or blocking proof'],
            'recipe_field': 'integration: {family, evidence_refs, missing_capabilities, fallback_reason}; cite inspect_integration_protocols evidence'}


def detect(value):
    """Read bounded structured evidence (including a JSON config file body).

    Skip contracts, prior recipes and prose to avoid detecting our own advice.
    Return only locations and event names, never command text or credentials.
    """
    found = []
    budget = [10000]
    def visit(node, pointer='', depth=0):
        budget[0] -= 1
        if depth > 14 or budget[0] < 0:
            return
        if isinstance(node, str):
            if len(node) <= 262144 and node.lstrip().startswith(('{', '[')):
                try: visit(json.loads(node), pointer + '/json', depth + 1)
                except ValueError: pass
            return
        if isinstance(node, list):
            for i, child in enumerate(node[:200]): visit(child, pointer + '/' + str(i), depth + 1)
        if not isinstance(node, dict): return
        hooks = node.get('hooks')
        if isinstance(hooks, dict):
            events = []
            for event in (*EVENTS, *EVENT_ALIASES):
                entries = hooks.get(event)
                if not isinstance(entries, list): continue
                for entry in entries:
                    if not isinstance(entry, dict): continue
                    commands = entry.get('hooks', [entry])
                    if isinstance(commands, list) and any(isinstance(c, dict) and c.get('type') == 'command' and isinstance(c.get('command'), str) and c['command'].strip() for c in commands):
                        events.append(event)
                        break
            if events: found.append({'family': 'command_hooks', 'pointer': pointer + '/hooks', 'events': events, 'status': 'candidate'})
        if type(node.get('protocolVersion')) is int and isinstance(node.get('agentCapabilities'), dict):
            found.append({'family': 'acp', 'pointer': pointer, 'protocol_version': node['protocolVersion'], 'status': 'candidate'})
        for key, child in node.items():
            if key in ('prior_memory', 'prior_experience', 'prior_investigation', 'control_contract', 'io_capture_contract', 'integration_protocol', 'integration', 'recipe', 'requirements'):
                continue
            visit(child, pointer + '/' + str(key).replace('~', '~0').replace('/', '~1'), depth + 1)
    visit(value)
    return found


def inspect(evidence_dir, target, refs):
    from pathlib import Path
    import re
    sources, candidates, skipped = [], [], []
    allowed = {'get_target_context', 'inspect_config_surface', 'read_related_file', 'inspect_loader_surface', 'inspect_entry_surface', 'read_evidence'}
    if refs is None or refs == []:
        # Select real observations in this investigation; never borrow another run.
        refs = []
        for file in sorted(Path(evidence_dir).glob('ev-*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                item = json.loads(file.read_text())
            except (OSError, ValueError):
                continue
            if item.get('target') == target and item.get('tool') in allowed and not item.get('error'):
                refs.append(file.stem)
            if len(refs) == 32:
                break
    if not isinstance(refs, list) or not 1 <= len(refs) <= 32:
        raise ValueError('No usable evidence. Call get_target_context, then inspect_integration_protocols with {}. Allowed tools: ' + ', '.join(sorted(allowed)))
    for ref in refs:
        if not isinstance(ref, str) or not re.fullmatch(r'ev-[0-9]+-[a-f0-9]{10}', ref):
            skipped.append({'evidence_id': str(ref)[:100], 'reason': 'invalid_id'})
            continue
        try:
            item = json.loads((Path(evidence_dir) / (ref + '.json')).read_text())
        except (OSError, ValueError):
            skipped.append({'evidence_id': ref, 'reason': 'missing_or_unreadable'})
            continue
        if item.get('target') != target:
            raise ValueError('Evidence ' + ref + ' belongs to another target instance; cannot use it')
        if item.get('error') or item.get('tool') not in allowed:
            skipped.append({'evidence_id': ref, 'tool': item.get('tool'), 'reason': 'failed_observation' if item.get('error') else 'not_observation_tool'})
            continue
        sources.append(ref)
        candidates.extend({**candidate, 'evidence_id': ref} for candidate in detect(item.get('result')))
        result = item.get('result') or {}
        collected = result if item['tool'] == 'inspect_config_surface' else result.get('local_evidence', {}) if item['tool'] == 'get_target_context' else {}
        if isinstance(collected, dict):
            candidates.extend({**c, 'evidence_id': ref} for c in collected.get('protocol_observations', [])
                              if isinstance(c, dict) and c.get('family') in FAMILIES[:2])
        if item['tool'] == 'get_target_context' and isinstance(result, dict):
            candidates.extend({**c, 'evidence_id': ref} for c in result.get('protocol_discovery', {}).get('candidates', [])
                              if c.get('family') in FAMILIES[:2] and c.get('status') == 'candidate')
    if not sources:
        raise ValueError('No usable observation evidence; call inspect_integration_protocols with {} to select current-run evidence automatically. Rejected: ' + json.dumps(skipped) + '; allowed_tools: ' + ', '.join(sorted(allowed)))
    candidates.sort(key=lambda c: FAMILIES.index(c['family']))
    return {'version': 1, 'target': target, 'status': 'candidates' if candidates else 'not_detected_in_scope',
            'candidates': candidates, 'sources': sources, 'skipped_evidence': skipped, 'allowed_tools': sorted(allowed),
            'next_step': 'Verify existing channel semantics and attach it first; investigate plugin extensions for missing capabilities.',
            'limitations': ['Bounded structured evidence only; absence is not proof of no protocol support', 'Configuration or handshake does not prove runtime activation or control']}


def validate_selection(recipe, evidence_dir, target, required=False):
    from pathlib import Path
    selection = recipe.get('integration')
    if selection is None and not required: return  # legacy stored recipes
    if not isinstance(selection, dict) or selection.get('family') not in FAMILIES:
        raise ValueError('integration.family must be command_hooks, acp, plugin or unsupported')
    refs = selection.get('evidence_refs')
    if not isinstance(refs, list) or not refs or any(r not in recipe.get('evidence_refs', []) for r in refs):
        raise ValueError('integration.evidence_refs must cite recipe evidence_refs')
    candidates = []
    inspected = 0
    for ref in refs:
        item = json.loads((Path(evidence_dir) / (ref + '.json')).read_text())
        # integration.evidence_refs may also cite the source/config evidence that
        # explains the selection. recipe_validation already verifies that every
        # cited ref is a successful observation from this exact target. Only the
        # protocol inspection result contributes detected protocol candidates.
        if item.get('tool') != 'inspect_integration_protocols':
            continue
        if item.get('target') != target or item.get('error'):
            raise ValueError('Run inspect_integration_protocols on current target before proposing integration')
        inspected += 1
        candidates.extend(item.get('result', {}).get('candidates', []))
    if not inspected:
        raise ValueError('Run inspect_integration_protocols on current target before proposing integration')
    family = selection['family']
    if family in ('command_hooks', 'acp') and not any(c['family'] == family for c in candidates):
        raise ValueError('Selected protocol has no observed structured evidence; inspect configuration/handshake first')
    missing = selection.get('missing_capabilities')
    if not isinstance(missing, list) or not all(isinstance(v, str) and v for v in missing):
        raise ValueError('integration.missing_capabilities must explicitly list gaps (or [])')
    if family in ('plugin', 'unsupported') or (family == 'acp' and any(c['family'] == 'command_hooks' for c in candidates)):
        if not isinstance(selection.get('fallback_reason'), str) or not selection['fallback_reason'].strip():
            raise ValueError('Explain why prior channels cannot satisfy the missing capabilities in fallback_reason')
    return selection
