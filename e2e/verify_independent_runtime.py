"""Task-5 acceptance: the Hook runtime keeps working with discovery stopped.

Phases: dependency manifest + static discovery-dependency check; stop the
scanner/dashboard and Goose and prove they are gone; repoint the Hook's decision
client at the standalone service; run real chat (>=30 rounds, >=50 tool calls)
and real allow/deny (10/10) through the standalone service; stop the service for
60s while real events keep being produced, then restart and prove catch-up
(missing 0, duplicates 0) within 30s. A soak of N minutes can be added.

Usage: python3 e2e/verify_independent_runtime.py [--duration-min 60] [...]
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import secrets
import signal
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts/autonomous-demo'
RUN = ROOT / 'artifacts/autonomous-service'
DASH = 'http://127.0.0.1:8081'
RT = 'http://127.0.0.1:8099'
CLIENT_CFG = RUN / 'hook-control-client.json'
SERVICE_CFG = RUN / 'hook-runtime.json'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
DISCOVERY_DEP_HINTS = ('analyzer', 'onboard', 'investigat', 'goose', 'autonomous_pipeline',
                       'find_agents', 'matcher', 'llm_proxy', 'scan')


def request(base, path, body=None, timeout=120, method='POST'):
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'}, method=method)
    try:
        return json.load(OPENER.open(req, timeout=timeout))
    except urllib.error.HTTPError as exc:
        return {'__http__': exc.code}
    except urllib.error.URLError as exc:
        return {'__error__': type(exc).__name__ + ': ' + str(exc.reason)}


def wait_service(deadline=30):
    end = time.time() + deadline
    while time.time() < end:
        if request(RT, '/health', method='GET').get('status') == 'ok':
            return True
        time.sleep(1)
    return False


def live_pids(pattern):
    """PIDs whose command matches the pattern and whose cwd is inside this repo.

    Scoping by cwd avoids stopping an unrelated checkout's service on the same
    host (for example the stage1 project's own dashboard).
    """
    import psutil
    hits = []
    for proc in psutil.process_iter(['pid', 'cmdline', 'cwd']):
        try:
            cmd = ' '.join(proc.info['cmdline'] or [])
            if pattern not in cmd:
                continue
            if (proc.info['cwd'] or '') == str(ROOT):
                hits.append(proc.info['pid'])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return hits


def dependency_manifest():
    """Compute the runtime-local import closure from the service entry point."""
    present, stack, names = [], ['runtime/hook_service.py', 'runtime/hook_control_client.py',
                                 'hook_runtime.py'], set()
    seen = set()
    while stack:
        rel = stack.pop()
        if rel in seen or not (ROOT / rel).is_file():
            continue
        seen.add(rel)
        present.append(rel)
        tree = ast.parse((ROOT / rel).read_text())
        for node in ast.walk(tree):
            candidates = []
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
                candidates = ['runtime/' + a.name.split('.')[1] + '.py'
                              for a in node.names if a.name.startswith('runtime.')]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
                parts = node.module.split('.')
                if node.module == 'runtime':
                    candidates = ['runtime/' + a.name + '.py' for a in node.names]
                elif len(parts) > 1 and parts[0] == 'runtime':
                    candidates = ['runtime/' + parts[1] + '.py']
            stack.extend(c for c in candidates if (ROOT / c).is_file())
    discovery = sorted(n for n in names if any(h in n.lower() for h in DISCOVERY_DEP_HINTS))
    return {'files': present, 'interpreter': subprocess.run(
        ['python3', '-c', 'import sys;print(sys.executable)'], capture_output=True, text=True).stdout.strip(),
        'discovery_dependencies': discovery}


def start_dashboard(executable="python3"):
    subprocess.run([executable, 'service.py', 'start', '--config', str(RUN / 'settings.json')],
                   cwd=str(ROOT), capture_output=True, text=True, timeout=30)
    for _ in range(40):
        try:
            urllib.request.urlopen(DASH + '/api/state', timeout=3)
            return True
        except Exception:  # noqa: BLE001
            time.sleep(0.5)
    return False


def chat_rounds(target, gateway, n_rounds, tools_per_round):
    """Real chat rounds with distinct tool calls (never repeated identically,
    which would trip opencode's doom_loop permission ask and hang the session)."""
    canaries = []
    for i in range(n_rounds):
        sid = request(target, '/session', {'title': 'independent',
            'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]}, timeout=60)['id']
        canary = 'ASG-IND-' + secrets.token_hex(6)
        text = ('请依次用工具完成三件事：1) 用 read 读取 demo.txt；2) 用 bash 执行 ls -1；'
                '3) 用 read 读取 package.json。三件事各做一次，然后简短确认 %s。' % canary)
        if tools_per_round == 0:
            text = '不要调用任何工具。请用约 300 字说明事件暂存，并原样包含编号 ' + canary
        try:
            response = request(target, '/session/' + sid + '/message',
                    {'model': {'providerID': 'demo', 'modelID': gateway['model']},
                     'parts': [{'type': 'text', 'text': text}]}, timeout=90)
            text_out = ''.join(p.get('text', '') for p in response.get('parts', []) if p.get('type') == 'text')
            canaries.append(canary if canary in text_out else canary + ' ERR:missing_reply')
        except Exception as exc:  # noqa: BLE001
            canaries.append(canary + ' ERR:' + type(exc).__name__)
    return sid, canaries


def control_rounds(target, gateway, pid, decision, n):
    ok = 0
    for _ in range(n):
        started = time.time()
        marker = Path(json.loads((DEMO / 'target.json').read_text())['workspace']) / (
            'ind-' + decision + '-' + secrets.token_hex(6) + '.txt')
        content = secrets.token_hex(16)
        request(RT, '/api/hook-control/policy',
                {'default': 'allow', 'rules': [{'tool': t, 'decision': decision, 'pid': pid}
                                               for t in ('write', 'edit', 'bash')]})
        sid = request(target, '/session', {'title': 'ind ' + decision,
            'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]}, timeout=60)['id']
        try:
            request(target, '/session/' + sid + '/message',
                    {'model': {'providerID': 'demo', 'modelID': gateway['model']},
                     'parts': [{'type': 'text', 'text': ('Use the write tool exactly once to create %s '
                       'containing %s. If denied stop.' % (marker, content))}]}, timeout=300)
        except Exception:  # noqa: BLE001
            pass
        good = (not marker.exists()) if decision == 'deny' else (
            marker.exists() and marker.read_text().strip() == content)
        status = request(RT, '/api/hook-control/status', method='GET')
        delivered = any(e.get('event') == 'decision.returned' and e.get('pid') == pid
                        and e.get('decision') == decision and e.get('timestamp', 0) >= started
                        and marker.name in json.dumps(e.get('input'))
                        for e in status.get('events', []))
        ok += 1 if good and delivered else 0
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=30)
    ap.add_argument('--tools', type=int, default=3)
    ap.add_argument('--allow', type=int, default=10)
    ap.add_argument('--deny', type=int, default=10)
    ap.add_argument('--outage-s', type=int, default=60)
    ap.add_argument('--soak-min', type=int, default=0)
    args = ap.parse_args()

    gateway = json.loads((DEMO / 'gateway.json').read_text())
    report = {'manifest': dependency_manifest(), 'started_at': time.time()}
    original_client = CLIENT_CFG.read_text()
    original_policy = request(RT, '/api/hook-control/status', method='GET')['policy']
    dashboard_pids = live_pids('monitor_dashboard.py')
    import psutil
    dashboard_executable = psutil.Process(dashboard_pids[0]).exe() if dashboard_pids else 'python3'
    goose_pids = live_pids('goose run')
    report['before'] = {'dashboard_pids': dashboard_pids, 'goose_pids': goose_pids,
                        'service_health': request(RT, '/health', method='GET')}
    try:
        # 1) stop discovery + Goose; the standalone service must stay up
        for pid in dashboard_pids + goose_pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        time.sleep(3)
        report['discovery_stopped'] = {'dashboard_alive': live_pids('monitor_dashboard.py'),
                                       'goose_alive': live_pids('goose run')}
        report['service_after_stop'] = request(RT, '/health', method='GET')
        # 2) point the Hook's decision client at the standalone service
        cfg = json.loads(original_client)
        cfg['base_url'] = RT + '/api/hook-control'
        CLIENT_CFG.write_text(json.dumps(cfg))
        # 3) workload + control through the standalone service
        m = json.loads((DEMO / 'target.json').read_text())
        target = 'http://127.0.0.1:' + str(m['port'])
        iid = '%s:%s' % (m['pid'], m['create_time'])
        request(RT, '/api/hook-control/policy', {'default': 'allow', 'rules': []})
        phase_start = time.time()
        sid, canaries = chat_rounds(target, gateway, args.rounds, args.tools)
        time.sleep(2)
        data = request(RT, '/api/hook-data?' + urllib.parse.urlencode(
            {'instance_id': iid, 'limit': 4000}), method='GET')
        recs = data.get('records') or []
        report['workload'] = {
            'chat_rounds': len([c for c in canaries if 'ERR' not in c]),
            'tool_calls': sum(1 for r in recs if r.get('event_type') == 'tool.execute.before' and r.get('timestamp', 0) >= phase_start),
            'instance': iid,
        }
        report['control'] = {
            'allow_ok': control_rounds(target, gateway, m['pid'], 'allow', args.allow),
            'deny_ok': control_rounds(target, gateway, m['pid'], 'deny', args.deny),
        }
        # 4) outage: stop the service, keep producing real events, restart, catch up
        outage_canaries = []
        subprocess.run(['python3', 'hook_runtime.py', 'stop', '--config', str(SERVICE_CFG)],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        outage_start = time.time()
        outage_sid, outage_canaries = chat_rounds(target, gateway, max(10, args.rounds // 3), 0)
        while time.time() - outage_start < args.outage_s:
            time.sleep(2)
        subprocess.run(['python3', 'hook_runtime.py', 'start', '--config', str(SERVICE_CFG)],
                       cwd=str(ROOT), capture_output=True, text=True, timeout=30)
        restarted = time.time()
        wait_service(30)
        # catch-up: data queryable within 30s
        caught_up_at = None
        for _ in range(30):
            d = request(RT, '/api/hook-data?' + urllib.parse.urlencode(
                {'instance_id': iid, 'limit': 4000}), method='GET')
            cur = d.get('records') or []
            captured = json.dumps(cur, ensure_ascii=False)
            expected = [c for c in outage_canaries if ' ERR:' not in c]
            if expected and all(c in captured for c in expected):
                caught_up_at = time.time() - restarted
                break
            time.sleep(1)
        all_recs = (request(RT, '/api/hook-data?' + urllib.parse.urlencode(
            {'instance_id': iid, 'limit': 8000}), method='GET').get('records') or [])
        window = [r for r in all_recs if r.get('timestamp', 0) >= outage_start]
        lines = [r.get('line') for r in window if r.get('line') is not None]
        blob = json.dumps(window, ensure_ascii=False)
        report['outage'] = {
            'events_in_window': len(window),
            'duration_s': restarted - outage_start,
            'catch_up_seconds': caught_up_at,
            'missing_canaries': [c for c in outage_canaries if c not in blob],
            'duplicate_records': len(lines) - len(set(lines)),
        }
        # 5) optional soak
        soak_samples = []
        end = time.time() + args.soak_min * 60
        while time.time() < end:
            soak_samples.append(request(RT, '/health', method='GET').get('status'))
            time.sleep(30)
        report['soak'] = {'minutes': args.soak_min, 'samples': len(soak_samples),
                          'all_ok': all(s == 'ok' for s in soak_samples)}
    finally:
        subprocess.run(['python3', 'hook_runtime.py', 'start', '--config', str(SERVICE_CFG)],
                       cwd=str(ROOT), capture_output=True, timeout=30)
        request(RT, '/api/hook-control/policy', original_policy)
        CLIENT_CFG.write_text(original_client)
        report['dashboard_restored'] = start_dashboard(dashboard_executable)
    report['finished_at'] = time.time()
    gates = {
        'no_discovery_dependency': not report['manifest']['discovery_dependencies'],
        'discovery_and_goose_stopped': not report['discovery_stopped']['dashboard_alive']
                                       and not report['discovery_stopped']['goose_alive'],
        'service_survives_discovery_stop': report['service_after_stop'].get('status') == 'ok',
        'chat_30_rounds': report['workload']['chat_rounds'] >= 30,
        'tool_calls_50': report['workload']['tool_calls'] >= 50,
        'allow_10_of_10': report['control']['allow_ok'] >= 10,
        'deny_10_of_10': report['control']['deny_ok'] >= 10,
        'outage_100_events': report['outage']['events_in_window'] >= 100,
        'outage_at_least_60s': report['outage']['duration_s'] >= 60,
        'outage_catch_up_30s': report['outage']['catch_up_seconds'] is not None and report['outage']['catch_up_seconds'] <= 30,
        'outage_missing_0': not report['outage']['missing_canaries'],
        'outage_duplicates_0': report['outage']['duplicate_records'] == 0,
    }
    gates['soak_60min_ok'] = args.soak_min >= 60 and report['soak']['all_ok'] and report['soak']['samples'] >= 100
    report['gates'] = gates
    report['passed'] = all(gates.values())
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'independent-runtime.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed']}, ensure_ascii=False))
    for key in ('manifest', 'workload', 'control', 'outage'):
        print(key, json.dumps(report.get(key), ensure_ascii=False))


if __name__ == '__main__':
    main()
