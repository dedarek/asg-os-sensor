"""Task-5 local check: the standalone runtime runs from a closure-only install.

It copies only the resident dependency closure into a fresh install directory
(no discovery/investigation files), points it at a run directory that contains
only the observation bindings, and proves the service starts and serves the
bound instances. This is the local form of "no required dependency on the
discovery source tree"; a machine reboot remains an external condition.

Usage: python3 e2e/verify_portable_install.py
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime.learned_install import _atomic  # noqa: E402

RUN = ROOT / 'artifacts/autonomous-service'
DISCOVERY_FILES = ('runtime/analyzer.py', 'runtime/matcher.py', 'runtime/onboarding.py',
                   'runtime/autonomous_pipeline.py', 'runtime/investigation_findings.py',
                   'runtime/analyst_tools.py', 'find_agents.py', 'find_agents_behavior.py',
                   'monitor_dashboard.py')


def closure_files():
    """Follow runtime-local imports from the service entry point."""
    import ast
    seed = ['hook_runtime.py', 'runtime/hook_service.py', 'runtime/hook_control_client.py']
    seen, stack, files = set(), list(seed), []
    while stack:
        rel = stack.pop()
        if rel in seen or not (ROOT / rel).is_file():
            continue
        seen.add(rel)
        files.append(rel)
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Import):
                candidates = ['runtime/' + a.name.split('.')[1] + '.py'
                              for a in node.names if a.name.startswith('runtime.')]
            elif isinstance(node, ast.ImportFrom) and node.module:
                parts = node.module.split('.')
                if node.module == 'runtime':
                    candidates = ['runtime/' + a.name + '.py' for a in node.names]
                elif len(parts) > 1 and parts[0] == 'runtime':
                    candidates = ['runtime/' + parts[1] + '.py']
            for candidate in candidates:
                if (ROOT / candidate).is_file():
                    stack.append(candidate)
    return files


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def get(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.load(response)


def main():
    report = {}
    files = list(dict.fromkeys(closure_files()))
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp).resolve()
        install = base / 'asg-runtime'
        run_dir = base / 'run'
        (install / 'runtime').mkdir(parents=True)
        run_dir.mkdir(parents=True)
        copied = []
        for rel in files:
            source = ROOT / rel
            if not source.is_file():
                continue
            target = install / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            copied.append(rel)
        # run dir gets only the bindings (logs are referenced by absolute path)
        shutil.copy2(RUN / 'observations.json', run_dir / 'observations.json')
        if (RUN / 'observation-bindings').is_dir():
            shutil.copytree(RUN / 'observation-bindings', run_dir / 'observation-bindings')
        port = free_port()
        config = run_dir / 'hook-runtime.json'
        _atomic(config, json.dumps({'ASG_RUN_DIR': str(run_dir), 'ASG_HOOK_HOST': '127.0.0.1',
                                    'ASG_HOOK_PORT': str(port), 'ASG_HOOK_REMOTE': '0'}).encode())
        report['copied'] = copied
        report['discovery_files_present_in_install'] = [
            rel for rel in DISCOVERY_FILES if (install / rel).exists()]
        env = {k: v for k, v in os.environ.items() if not k.startswith('ASG_')}
        started = subprocess.run(['python3', 'hook_runtime.py', 'start', '--config', str(config)],
                                 cwd=str(install), env=env, capture_output=True, text=True, timeout=30)
        report['start_stdout'] = started.stdout.strip()
        health = None
        for _ in range(20):
            try:
                health = get('http://127.0.0.1:%d/health' % port)
                break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        report['health'] = health
        report['instances_served'] = (health or {}).get('instances')
        subprocess.run(['python3', 'hook_runtime.py', 'stop', '--config', str(config)],
                       cwd=str(install), env=env, capture_output=True, text=True, timeout=30)

    gates = {
        'install_has_no_discovery_files': report['discovery_files_present_in_install'] == [],
        'closure_only_install_starts_and_serves': (report.get('health') or {}).get('status') == 'ok',
        'bound_instances_served_from_isolated_run_dir': (report.get('instances_served') or 0) > 0,
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    report['external_condition'] = '机器重启后 60 秒恢复仍需专用测试机器'
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'portable-install.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed'],
                      'instances': report['instances_served'],
                      'discovery_files_present': report['discovery_files_present_in_install']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
