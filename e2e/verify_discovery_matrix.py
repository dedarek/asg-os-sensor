"""Discovery acceptance matrix: fixed real agents found, ordinary programs not admitted.

Runs the same per-process decision path as the dashboard scan
(agent_score + runtime_discovery_candidate) over every live process, then:
  A. asserts each fixed real-agent pid (resolved live, never hardcoded pids)
     is admitted as a candidate;
  B. asserts an adversarial set of ordinary/edge programs is NOT admitted as
     an independent candidate (helper processes, shells, MCP tool servers,
     plain CLIs, plain desktop helpers).
Fixture negatives are spawned as short-lived child processes by this script
only, and killed before exit. Writes a JSON report; exit 0 only when all pass.

  python3 e2e/verify_discovery_matrix.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from asg_os_sensor import Sensor, load_policies  # noqa: E402
from runtime.identity import metadata_identity, runtime_discovery_candidate  # noqa: E402


def live_candidates():
    sensor = Sensor(load_policies())
    threshold = int(load_policies().get('agent_score_threshold', 50))
    admitted = {}
    for proc in psutil.process_iter(['pid', 'ppid', 'exe', 'name', 'cmdline', 'create_time']):
        try:
            pinfo = proc.info
            pid = pinfo.get('pid')
            cmdline = pinfo.get('cmdline') or []
            if not cmdline or pid == os.getpid():
                continue
            score, reasons = sensor.agent_score(sensor.wrap_pid(pid))
            discovery = runtime_discovery_candidate(pinfo, proc) if 0 <= score < threshold else {}
            if score >= threshold or discovery:
                admitted[pid] = {'score': score,
                                 'signals': [s['source'] for s in (discovery or {}).get('signals', [])],
                                 'name': pinfo.get('name')}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return admitted


def find_agent_pid(matcher, label):
    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            info = proc.info
            info['cmdline'] = info.get('cmdline') or []
            if matcher(info):
                return proc.info['pid']
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    raise SystemExit('fixed real agent not running: ' + label)


def main():
    # Fixed real agents on this host, resolved by launch shape at run time
    # (this file is the acceptance list; production code never sees it).
    agents = [
        ('opencode-demo', lambda i: (i.get('name') or '').startswith('opencode')
            and any('serve' in a for a in i.get('cmdline', []))),
        ('desktop-codex', lambda i: (i.get('name') or '').lower() == 'codex'
            and any('app-server' in a or 'codex' in a for a in i.get('cmdline', []))),
        ('dsh-web', lambda i: (i.get('name') or '') == 'node'
            and any(a.endswith('/.bin/dsh') or '/@deepseek-ai/dsh/' in a
                    for a in i.get('cmdline', []))),
        ('zcode-main', lambda i: (i.get('name') or '') == 'ZCode'),
        ('opencodex-gateway', lambda i: (i.get('name') or '') == 'bun.exe'
            and any('opencodex' in a.lower() for a in i.get('cmdline', []))),
    ]
    fixtures = []
    tempdirs = []
    try:
        # Adversarial negatives spawned as real live processes.
        def spawn(argv, keep_dir):
            proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            fixtures.append(proc)
            return proc.pid

        def fixture_node(deps, args):
            tmp = tempfile.mkdtemp(prefix='asg-discovery-neg-')
            tempdirs.append(tmp)
            root = Path(tmp)
            entry = root / 'bin' / 'tool.js'
            entry.parent.mkdir()
            entry.write_text('setTimeout(()=>{},600000)')
            (root / 'package.json').write_text(json.dumps(
                {'name': 'neg-' + uuid.uuid4().hex, 'bin': {'tool': 'bin/tool.js'},
                 'dependencies': deps}))
            return spawn(['node', str(entry)] + args, tmp)

        plain_cli = fixture_node({'commander': '*'}, ['serve', '--port', '4'])
        mcp_like = fixture_node({'mcpkit': '*', 'acpx-tools': '*'}, ['web'])
        sleep_pid = spawn(['/bin/sleep', '600'], None)
        shell = subprocess.Popen(['/bin/zsh', '-c', 'sleep 600'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        fixtures.append(shell)
        mcp_server = spawn(['node', '-e', '// fake mcp-server entry setTimeout(()=>{},600000)'], None)

        # Desktop-app helper processes (never the main executable) must not be
        # admitted independently.
        helper_negatives = []
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                name = proc.info.get('name') or ''
                if name.endswith(' Helper') or '--type=renderer' in ' '.join(proc.info.get('cmdline') or []):
                    helper_negatives.append((proc.info['pid'], name))
                    if len(helper_negatives) >= 5:
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        time.sleep(1.2)
        admitted = live_candidates()

        report = {'agent_checks': [], 'negative_checks': [], 'failed': 0}
        for label, matcher in agents:
            pid = find_agent_pid(matcher, label)
            hit = pid in admitted
            report['agent_checks'].append(
                {'agent': label, 'pid': pid, 'admitted': hit,
                 'detail': admitted.get(pid, {})})
            if not hit:
                report['failed'] += 1
        for label, pid in [('plain-cli-with-bin', plain_cli),
                           ('mcp-lookalike-deps', mcp_like),
                           ('sleep-binary', sleep_pid),
                           ('zsh-wrapper', shell.pid),
                           ('fake-mcp-server', mcp_server)]:
            hit = pid in admitted
            report['negative_checks'].append({'case': label, 'pid': pid, 'admitted': hit})
            if hit:
                report['failed'] += 1
        for pid, name in helper_negatives:
            hit = pid in admitted
            report['negative_checks'].append({'case': 'desktop-helper:' + name, 'pid': pid,
                                              'admitted': hit})
            if hit:
                report['failed'] += 1
    finally:
        for proc in fixtures:
            try:
                proc.kill()
            except OSError:
                pass
        for tmp in tempdirs:
            subprocess.run(['/bin/rm', '-rf', tmp])

    out = Path(tempfile.gettempdir()) / 'asg-discovery-matrix-report.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    for row in report['agent_checks']:
        print('%-22s pid=%-7s %s' % (row['agent'], row['pid'],
                                     'FOUND' if row['admitted'] else 'MISS'))
    for row in report['negative_checks']:
        print('%-28s pid=%-7s %s' % (row['case'], row['pid'],
                                     'FALSE-POSITIVE' if row['admitted'] else 'clean'))
    print('report: %s' % out)
    return 1 if report['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
