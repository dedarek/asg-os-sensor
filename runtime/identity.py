"""Local identity evidence and process ownership, independent of LLM recipes."""
import re
import json
import plistlib
import os
from pathlib import Path

import yaml


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


def identify(info, catalog):
    for rule in catalog.get('agents', []):
        for value in entrypoints(info):
            if any(re.search(pattern, value, re.I) for pattern in rule['patterns']):
                return {'id': rule['id'], 'name': rule['name'],
                        'source': 'local-entrypoint', 'evidence': value}
    return {}


def metadata_identity(info):
    """Read bounded metadata beside the real entry, never prompts or user configs.

    A display name alone says nothing about whether the program is an Agent.
    model_sdk indicates declared capability, not proof of an active model call.
    """
    for value in reversed(entrypoints(info)):
        path = Path(value)
        if not path.is_absolute():
            continue
        for parent in list(path.parents)[:6]:
            package = parent / 'package.json'
            try:
                if package.is_file() and package.stat().st_size < 262144:
                    data = json.loads(package.read_text())
                    if isinstance(data.get('name'), str):
                        deps = data.get('dependencies') or {}
                        sdk = any(k in deps for k in ('openai', '@anthropic-ai/sdk', '@ai-sdk/openai', 'litellm', '@langchain/core'))
                        return {'id': 'package:' + str(parent), 'name': data.get('productName') or data['name'],
                                'source': 'package-metadata', 'evidence': str(package), 'model_sdk': sdk}
            except (OSError, ValueError, TypeError):
                pass
        # Outermost bundle identifies the host, not each Electron helper bundle.
        bundle = next((p for p in reversed(path.parents) if p.suffix == '.app'), None)
        if bundle:
            manifest = bundle / 'Contents' / 'Info.plist'
            try:
                if manifest.stat().st_size > 262144:
                    continue
                data = plistlib.loads(manifest.read_bytes())
                name = data.get('CFBundleDisplayName') or data.get('CFBundleName')
                if name:
                    return {'id': 'bundle:' + str(bundle), 'name': name,
                            'source': 'bundle-metadata', 'evidence': str(manifest)}
            except (OSError, ValueError, plistlib.InvalidFileException):
                pass
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
