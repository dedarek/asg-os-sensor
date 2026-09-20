"""Terminal lifecycle acceptance: install/start/doctor/upgrade/uninstall.

Runs the full delivery-package acceptance against a real SOC gateway without
touching real agents or the developer's running services:

  python3 e2e/verify_terminal_lifecycle.py --package /path/asg-terminal \
      [--gateway http://127.0.0.1:8095] [--enrollment-key /path/app.key]

Every step records name, timings, package digest, expected/actual and evidence
paths into a JSON report; exit 0 only when every step passed.
"""
import argparse
import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def free_port():
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def http(method, url, body=None, key=None, timeout=15):
    data = json.dumps(body).encode() if body is not None else None
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['Authorization'] = 'Bearer ' + key
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    with opener.open(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode())


class Runner:
    def __init__(self, package, gateway, enrollment_key, evidence_dir):
        self.package = Path(package).resolve()
        self.gateway = gateway.rstrip('/')
        self.enrollment_key = Path(enrollment_key).expanduser().read_text().strip()
        self.evidence = Path(evidence_dir)
        self.steps = []
        self.home = None

    def asgctl(self, package, *args, home=None):
        home = home or self.home
        command = [str(Path(package or self.package) / 'asgctl'), '--home', str(home),
                   '--json'] + list(args)
        started = time.time()
        result = subprocess.run(command, capture_output=True, text=True, timeout=300)
        payload = None
        for line in reversed((result.stdout or '').splitlines()):
            line = line.strip()
            if line.startswith('{'):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    pass
        return result.returncode, payload, (result.stdout or '') + (result.stderr or ''), time.time() - started

    def step(self, name, fn, expected=''):
        started = time.time()
        entry = {'step': name, 'expected': expected,
                 'package_sha256': sha256_file(self.package / 'release.json'),
                 'started_at': started}
        try:
            detail = fn() or {}
            entry.update({'pass': True, 'detail': detail})
        except AssertionError as exc:
            entry.update({'pass': False, 'error': str(exc)})
        except Exception as exc:
            entry.update({'pass': False, 'error': '%s: %s' % (type(exc).__name__, exc)})
        entry['elapsed_s'] = round(time.time() - started, 1)
        self.steps.append(entry)
        print('%-38s %s (%.1fs)' % (name, 'PASS' if entry['pass'] else 'FAIL', entry['elapsed_s']),
              flush=True)
        if not entry['pass']:
            print('    ' + str(entry.get('error', ''))[:400], flush=True)
        return entry['pass']

    def heartbeat(self):
        path = Path(self.home) / 'state' / 'operations' / 'heartbeat.json'
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def queue_count(self):
        db = sqlite3.connect('file:%s?mode=ro' % (Path(self.home) / 'state' / 'endpoint' / 'outbox.sqlite'), uri=True)
        try:
            return db.execute('SELECT count(*) FROM queue').fetchone()[0]
        finally:
            db.close()

    def service_pids(self):
        label = 'com.asg.terminal' if sys.platform == 'darwin' else 'asg-terminal'
        if sys.platform == 'darwin':
            probe = subprocess.run(['launchctl', 'print', 'gui/%d/%s' % (os.getuid(), label)],
                                   capture_output=True, text=True)
            if probe.returncode != 0:
                return []
            return [line.split('=', 1)[1].strip().strip('();')
                    for line in probe.stdout.splitlines() if 'pid =' in line]
        probe = subprocess.run(['systemctl', '--user', 'show', label, '-p', 'MainPID'],
                               capture_output=True, text=True)
        pid = dict(line.split('=', 1) for line in probe.stdout.splitlines() if '=' in line).get('MainPID')
        return [pid] if pid and pid != '0' else []


def make_variant(base, out, version=None, sabotage=False):
    """Copy the delivery package and rewrite release.json (version bump keeps
    every file digest; the sabotage build adds one marker file and rehashes)."""
    out = Path(out)
    shutil.copytree(base, out, symlinks=True)
    manifest = json.loads((out / 'release.json').read_text())
    if sabotage:
        marker = out / 'app' / 'bin' / 'sabotage.json'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"reason": "acceptance rollback probe"}\n')
        manifest['files']['app/bin/sabotage.json'] = {'sha256': sha256_file(marker)}
    if version:
        manifest['asg_version'] = version
    (out / 'release.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    parser.add_argument('--gateway', default='http://127.0.0.1:8095')
    parser.add_argument('--enrollment-key',
                        default='/Users/mac/Documents/ChatGPT/asg/soc-review/.local/enrollment-application.key')
    parser.add_argument('--psql', default='127.0.0.1:55432/asg_soc_poc/mac')
    parser.add_argument('--keep', action='store_true')
    args = parser.parse_args()

    run_dir = Path(tempfile.mkdtemp(prefix='asg-lifecycle-acceptance-'))
    runner = Runner(args.package, args.gateway, args.enrollment_key, run_dir)
    runner.home = run_dir / 'home'
    psqphost, remainder = args.psql.split(':', 1)
    psqpport, psqpdb, psqpuser = remainder.split('/', 2)
    cred = run_dir / 'credential'
    shutil.copyfile(args.enrollment_key, cred)
    os.chmod(cred, 0o600)
    port = free_port()
    dead_port = free_port()
    install_extra = ['--port', str(port), '--collection-mode', 'auto',
                     '--collect-interval', '60', '--no-discovery',
                     '--skip-soc-installation', '--skip-skill-upload',
                     '--engine-env', 'ASG_AUTONOMOUS_ANALYSIS=0']

    test_agent = 'e2e-terminal-' + uuid.uuid4().hex[:12]
    soc_agent = {'id': None}  # gateway-minted agent id from /api/asg/enroll

    def sql(query):
        result = subprocess.run(['psql', '-h', psqphost, '-p', psqpport, '-U', psqpuser,
                                 '-d', psqpdb, '-tAc', query], capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError('psql failed: ' + result.stderr.strip()[:200])
        return result.stdout.strip()

    results = {}

    def step_install_once():
        rc, payload, output, waited = runner.asgctl(None, 'install', '--soc-url', runner.gateway,
                                                    '--credential-file', str(cred),
                                                    '--package', str(runner.package), *install_extra)
        assert rc == 0, 'rc=%s %s' % (rc, output[-300:])
        assert payload and payload.get('status') == 'installed', payload
        assert payload.get('soc', {}).get('auth') == 'ok', payload.get('soc')
        assert waited <= 60, 'health wait exceeded 60s: %.1f' % waited
        return {'healthy_in_s': round(waited, 1)}

    def step_package_survives_source_removal():
        stash = run_dir / 'package-stash'
        os.rename(runner.package, stash)
        try:
            time.sleep(12)
            hb = runner.heartbeat()
            assert hb and time.time() - float(hb.get('t', 0)) < 30, 'heartbeat stalled after source removal'
            assert all(child.get('alive') for child in hb['children'].values()), hb['children']
        finally:
            os.rename(stash, runner.package)

    def step_repeat_install_idempotent():
        for _ in range(2):
            rc, payload, output, _ = runner.asgctl(None, 'install', '--soc-url', runner.gateway,
                                                   '--credential-file', str(cred),
                                                   '--package', str(runner.package), *install_extra)
            assert rc == 0 and payload and payload.get('status') == 'already_installed', (rc, payload)
        assert len(runner.service_pids()) == 1, runner.service_pids()

    def step_first_collection_reports():
        agent = soc_agent['id'] or ''
        deadline = time.time() + 150
        rows = '0'
        while time.time() < deadline:
            rows = sql("SELECT count(*) FROM inventory_streams WHERE agent_id='%s'" % agent)
            if rows == '1':
                break
            time.sleep(5)
        assert rows == '1', 'collector stream not registered in SOC: agent=%s rows=%s' % (agent, rows)
        # the snapshot itself must land too, and the queue must drain afterwards
        while time.time() < deadline:
            snaps = sql("SELECT count(*) FROM inventory_snapshots WHERE agent_id='%s'" % agent)
            if int(snaps) >= 1 and runner.queue_count() == 0:
                return {'snapshots': int(snaps)}
            time.sleep(5)
        raise AssertionError('first snapshot never reached SOC or queue stuck')

    def step_register_test_agent():
        workspace = run_dir / 'agent-workspace'
        skill = workspace / 'skills' / 'e2e-demo'
        skill.mkdir(parents=True)
        (skill / 'SKILL.md').write_text('# e2e demo skill\n')
        status, result = http('POST', runner.gateway + '/api/asg/enroll',
                              {'host_id': 'e2e-lifecycle-host', 'instance_id': test_agent,
                               'name': 'E2E lifecycle agent', 'platform': 'e2e-platform'},
                              key=runner.enrollment_key)
        assert status == 200, result
        soc_agent['id'] = result['agent_id']
        key_file = run_dir / (test_agent + '.key')
        key_file.write_text(result['api_key'])
        os.chmod(key_file, 0o600)
        endpoint_config = json.loads((Path(runner.home) / 'config' / 'endpoint.json').read_text())
        endpoint_config['agents'] = [{'agent_id': result['agent_id'], 'platform': 'e2e-platform',
                                      'workspace': str(workspace), 'key_file': str(key_file),
                                      'learned_skill_roots': [str(skill.parent)]}]
        (Path(runner.home) / 'config' / 'endpoint.json').write_text(json.dumps(endpoint_config))
        rc, _, output, _ = runner.asgctl(None, 'stop')
        assert rc == 0, output
        rc, _, output, _ = runner.asgctl(None, 'start')
        assert rc == 0, output
        return {'agent_id': result['agent_id']}

    def step_disconnect_queues_then_reconnect_drains():
        rc, _, _, _ = runner.asgctl(None, 'stop')
        assert rc == 0
        config_path = Path(runner.home) / 'config' / 'endpoint.json'
        original = config_path.read_text()
        offline = json.loads(original)
        offline['backend_url'] = 'http://127.0.0.1:%d' % dead_port
        config_path.write_text(json.dumps(offline))
        rc, _, output, _ = runner.asgctl(None, 'start')
        assert rc == 0, output
        grew = False
        baseline = 0
        deadline = time.time() + 160
        while time.time() < deadline:
            try:
                count = runner.queue_count()
            except sqlite3.Error:
                count = 0
            if count > baseline:
                grew = True
                break
            time.sleep(5)
        assert grew, 'queue did not grow while SOC unreachable'
        config_path.write_text(original)
        rc, _, output, _ = runner.asgctl(None, 'stop')
        assert rc == 0, output
        rc, _, output, _ = runner.asgctl(None, 'start')
        assert rc == 0, output
        # The queue must drain and the endpoint's own SOC marker must turn ok.
        # Poll the durable marker directly: the supervisor heartbeat snapshots
        # it at 5s intervals and races the flush thread.
        deadline = time.time() + 240
        state = {}
        while time.time() < deadline:
            try:
                queue_now = runner.queue_count()
                marker = json.loads((Path(runner.home) / 'state' / 'endpoint' /
                                     'soc-health.json').read_text())
            except (sqlite3.Error, OSError, json.JSONDecodeError):
                queue_now, marker = -1, {}
            if queue_now == 0 and marker.get('state') == 'ok':
                return {'soc': marker.get('state')}
            state = {'queue': queue_now, 'soc': marker.get('state')}
            time.sleep(4)
        raise AssertionError('queue not drained or SOC still not ok: %s' % state)

    def step_no_duplicate_snapshots():
        row = sql("SELECT count(*), count(DISTINCT revision) FROM inventory_snapshots WHERE agent_id='%s'" % (soc_agent['id'] or ''))
        total, distinct = row.split('|')
        assert int(total) >= 1 and int(total) == int(distinct), 'duplicate snapshot revisions: %s' % row
        return {'snapshots': int(total)}

    def step_start_stop_cycles():
        # Spec: 10 consecutive starts keep exactly one process; after stop the
        # service manager must not auto-restart for at least 30 seconds.
        for index in range(10):
            rc, payload, output, _ = runner.asgctl(None, 'start')
            assert rc == 0, 'start %d: %s' % (index, output[-200:])
            pids = runner.service_pids()
            assert len(pids) == 1, 'start %d left %d processes' % (index, len(pids))
            rc, _, output, _ = runner.asgctl(None, 'stop')
            assert rc == 0, 'stop %d: %s' % (index, output[-200:])
            time.sleep(2)
            assert not runner.service_pids(), 'stop %d: service still loaded' % index
        time.sleep(30)
        assert not runner.service_pids(), 'service auto-restarted within 30s of stop'
        rc, _, output, _ = runner.asgctl(None, 'start')
        assert rc == 0, output
        return {'cycles': 10, 'no_autorestart_30s': True}

    def step_bad_credential_rc5():
        bad = run_dir / 'bad-credential'
        bad.write_text('asg_' + '0' * 64)
        alt_home = run_dir / 'home-badcred'
        rc, payload, output, _ = runner.asgctl(None, 'install', '--soc-url', runner.gateway,
                                               '--credential-file', str(bad),
                                               '--package', str(runner.package),
                                               '--port', str(free_port()), '--no-discovery',
                                               '--skip-soc-installation', '--skip-skill-upload',
                                               '--engine-env', 'ASG_AUTONOMOUS_ANALYSIS=0',
                                               '--service-label', 'com.asg.terminal.e2e-a',
                                               home=alt_home)
        assert rc == 5, 'expected rc5 auth-failed path, got rc=%s %s' % (rc, str(payload)[:200])
        assert payload and payload.get('healthy') is True and payload.get('soc', {}).get('auth') == 'rejected', payload
        # fixing the credential and re-running install recovers without reinstall
        rc, payload, output, _ = runner.asgctl(None, 'install', '--soc-url', runner.gateway,
                                               '--credential-file', str(cred),
                                               '--package', str(runner.package),
                                               '--port', str(json.loads((alt_home / 'config/terminal.json').read_text())['port']),
                                               '--no-discovery', '--skip-soc-installation',
                                               '--skip-skill-upload',
                                               '--engine-env', 'ASG_AUTONOMOUS_ANALYSIS=0',
                                               '--service-label', 'com.asg.terminal.e2e-a',
                                               home=alt_home)
        assert rc == 0 and payload.get('soc', {}).get('auth') == 'ok', payload
        subprocess.run([str(runner.package / 'asgctl'), '--home', str(alt_home), 'uninstall'],
                       capture_output=True, timeout=120)
        return {}

    def step_negative_installs():
        missing = run_dir / 'pkg-missing'
        shutil.copytree(runner.package, missing, symlinks=True)
        (missing / 'app' / 'identities.yaml').unlink()
        alt_home = run_dir / 'home-missing'
        rc, payload, output, _ = runner.asgctl(None, 'install', '--soc-url', runner.gateway,
                                               '--credential-file', str(cred), '--package', str(missing),
                                               '--port', str(free_port()), home=alt_home)
        assert rc == 2 and '文件缺失' in output, (rc, output[-200:])
        assert not (alt_home / 'config' / 'terminal.json').exists()

        tampered = run_dir / 'pkg-tampered'
        shutil.copytree(runner.package, tampered, symlinks=True)
        target = tampered / 'app' / 'asg_os_sensor.py'
        target.write_text(target.read_text() + '\n# tampered\n')
        alt_home = run_dir / 'home-tampered'
        rc, payload, output, _ = runner.asgctl(None, 'install', '--soc-url', runner.gateway,
                                               '--credential-file', str(cred), '--package', str(tampered),
                                               '--port', str(free_port()), home=alt_home)
        assert rc == 2 and '摘要' in output, (rc, output[-200:])
        assert not (alt_home / 'config' / 'terminal.json').exists()

        offline_home = run_dir / 'home-offline-soc'
        rc, payload, output, _ = runner.asgctl(None, 'install',
                                               '--soc-url', 'http://127.0.0.1:%d' % dead_port,
                                               '--credential-file', str(cred),
                                               '--package', str(runner.package),
                                               '--port', str(free_port()), '--no-discovery',
                                               '--skip-soc-installation', '--skip-skill-upload',
                                               '--engine-env', 'ASG_AUTONOMOUS_ANALYSIS=0',
                                               '--service-label', 'com.asg.terminal.e2e-b',
                                               home=offline_home)
        assert rc == 0 and payload.get('status') == 'installed', (rc, payload)
        assert payload.get('soc', {}).get('network') == 'unreachable', payload
        subprocess.run([str(runner.package / 'asgctl'), '--home', str(offline_home), 'uninstall'],
                       capture_output=True, timeout=120)
        return {}

    def step_doctor_fault_injection():
        # bad credential while service runs -> doctor separates 网络/鉴权
        credentials = Path(runner.home) / 'config' / 'credentials.json'
        original = credentials.read_text()
        credentials.write_text('asg_' + 'f' * 64)
        rc, payload, _, _ = runner.asgctl(None, 'doctor')
        checks = {item['name']: item for item in payload['checks']}
        assert checks['SOC 网络']['ok'] is True, checks['SOC 网络']
        assert checks['SOC 鉴权']['ok'] is False and '鉴权' in json.dumps(checks['SOC 鉴权'], ensure_ascii=False), checks['SOC 鉴权']
        credentials.write_text(original)
        # missing program file -> integrity names the file
        victim = Path(runner.home) / 'current/app/identities.yaml'
        backup = victim.with_suffix('.yaml.acceptance-bak')
        victim.rename(backup)
        rc, payload, _, _ = runner.asgctl(None, 'doctor')
        integrity = next(item for item in payload['checks'] if item['name'] == '程序完整性')
        assert integrity['ok'] is False and 'identities.yaml' in integrity['detail'], integrity
        backup.rename(victim)
        rc, payload, _, _ = runner.asgctl(None, 'doctor')
        integrity = next(item for item in payload['checks'] if item['name'] == '程序完整性')
        assert integrity['ok'] is True, integrity
        # SOC network down: use a TEST-NET address (RFC 5737, not routable) so
        # the failure is genuinely network-layer. A refused loopback port is a
        # service failure, not a network failure, and must not be conflated.
        terminal_cfg = Path(runner.home) / 'config' / 'terminal.json'
        terminal_original = terminal_cfg.read_text()
        offline_cfg = json.loads(terminal_original)
        offline_cfg['soc_url'] = 'http://192.0.2.1:80'
        terminal_cfg.write_text(json.dumps(offline_cfg))
        try:
            started = time.time()
            rc, payload, _, _ = runner.asgctl(None, 'doctor')
            assert time.time() - started < 30, 'doctor hung on unreachable SOC'
            checks = {item['name']: item for item in payload['checks']}
            assert checks['SOC 网络']['ok'] is False, checks['SOC 网络']
            assert checks['SOC 鉴权']['ok'] is False and '网络不可达' in checks['SOC 鉴权']['detail'], checks['SOC 鉴权']
        finally:
            terminal_cfg.write_text(terminal_original)
        # Refused loopback port: still a failed SOC check, and auth stays
        # unverified rather than being reported as a credential problem.
        offline_cfg['soc_url'] = 'http://127.0.0.1:%d' % dead_port
        terminal_cfg.write_text(json.dumps(offline_cfg))
        try:
            rc, payload, _, _ = runner.asgctl(None, 'doctor')
            checks = {item['name']: item for item in payload['checks']}
            assert checks['SOC 网络']['ok'] is False, checks['SOC 网络']
            assert checks['SOC 鉴权']['ok'] is False and '凭据' not in checks['SOC 鉴权']['detail'], checks['SOC 鉴权']
        finally:
            terminal_cfg.write_text(terminal_original)
        # Work-loop stall: the engine process stays alive (SIGSTOP), only its
        # loop stops answering. doctor and status must both report the stall.
        hb = runner.heartbeat()
        engine_pid = int((hb.get('children') or {}).get('engine', {}).get('pid', 0))
        assert engine_pid > 0, hb
        os.kill(engine_pid, signal.SIGSTOP)
        try:
            stalled = False
            deadline = time.time() + 45
            while time.time() < deadline:
                rc, payload, _, _ = runner.asgctl(None, 'doctor')
                checks = {item['name']: item for item in payload['checks']}
                if checks['工作循环']['ok'] is False and '引擎' in checks['工作循环']['detail']:
                    stalled = True
                    break
                time.sleep(4)
            assert stalled, 'frozen engine loop never reported by doctor'
            rc, status, _, _ = runner.asgctl(None, 'status')
            assert rc == 3 and any('引擎' in reason for reason in (status or {}).get('health_reasons', [])), (rc, status)
        finally:
            os.kill(engine_pid, signal.SIGCONT)
        recovered = False
        deadline = time.time() + 60
        while time.time() < deadline:
            rc, payload, _, _ = runner.asgctl(None, 'doctor')
            checks = {item['name']: item for item in payload['checks']}
            if checks['工作循环']['ok'] is True:
                recovered = True
                break
            time.sleep(4)
        assert recovered, 'work loop still reported stalled after resume'
        return {}

    def step_user_config_key_preserved():
        terminal = Path(runner.home) / 'config' / 'terminal.json'
        data = json.loads(terminal.read_text())
        data['acceptance_user_key'] = 'keep-me'
        terminal.write_text(json.dumps(data))
        return {}

    def make_b():
        return make_variant(runner.package, run_dir / 'package-B', version='9.9.2')

    def make_c():
        return make_variant(runner.package, run_dir / 'package-C', version='9.9.3', sabotage=True)

    def step_upgrade_a_to_b():
        package_b = make_b()
        rc, payload, output, waited = runner.asgctl(None, 'upgrade', '--package', str(package_b))
        assert rc == 0 and payload and payload.get('status') == 'upgraded', (rc, str(payload)[:250])
        assert payload.get('version') == '9.9.2' and payload.get('from') != '9.9.2', payload
        rc, status, _, _ = runner.asgctl(None, 'status')
        assert rc == 0 and status.get('version') == '9.9.2' and status.get('healthy'), status
        terminal = json.loads((Path(runner.home) / 'config' / 'terminal.json').read_text())
        assert terminal.get('acceptance_user_key') == 'keep-me', 'user config key lost on upgrade'
        db = sqlite3.connect('file:%s?mode=ro' % (Path(runner.home) / 'state' / 'endpoint' / 'outbox.sqlite'), uri=True)
        streams = db.execute('SELECT count(*) FROM streams').fetchone()[0]
        db.close()
        assert streams >= 1, 'stream state lost on upgrade'
        return {'upgraded_in_s': round(waited, 1)}

    def step_upgrade_failure_rolls_back():
        # Seed a queue item so the restore path must preserve it.
        db = sqlite3.connect(Path(runner.home) / 'state' / 'endpoint' / 'outbox.sqlite')
        db.execute("INSERT INTO queue(agent,path,method,body) VALUES('acceptance-probe','/api/health','POST','{}')")
        db.commit()
        db.close()
        package_c = make_c()
        started = time.time()
        rc, payload, output, _ = runner.asgctl(None, 'upgrade', '--package', str(package_c))
        elapsed = time.time() - started
        assert rc == 1 and payload and payload.get('status') == 'failed_rolled_back', (rc, str(payload)[:250])
        assert payload.get('old_healthy') is True, payload
        assert elapsed <= 90 + 75, 'rollback exceeded budget: %.0fs' % elapsed
        rc, status, _, _ = runner.asgctl(None, 'status')
        assert status.get('version') == '9.9.2' and status.get('healthy'), status
        db = sqlite3.connect('file:%s?mode=ro' % (Path(runner.home) / 'state' / 'endpoint' / 'outbox.sqlite'), uri=True)
        seeded = db.execute("SELECT count(*) FROM queue WHERE agent='acceptance-probe'").fetchone()[0]
        db.close()
        assert seeded == 1, 'queued records lost across rollback'
        # same broken package again: fails cleanly again, old version stays healthy
        rc2, payload2, _, _ = runner.asgctl(None, 'upgrade', '--package', str(package_c))
        assert rc2 == 1 and payload2.get('status') == 'failed_rolled_back', (rc2, str(payload2)[:200])
        rc3, status3, _, _ = runner.asgctl(None, 'status')
        assert status3.get('healthy') and status3.get('version') == '9.9.2', status3
        # clear the seeded probe row so later steps can assert an empty queue
        db = sqlite3.connect(Path(runner.home) / 'state' / 'endpoint' / 'outbox.sqlite')
        db.execute("DELETE FROM queue WHERE agent='acceptance-probe'")
        db.commit()
        db.close()
        return {'rollback_total_s': round(elapsed, 1)}

    def step_uninstall_keeps_data_and_hooks():
        rc, payload, output, _ = runner.asgctl(None, 'uninstall')
        assert rc == 0 and payload.get('service_gone') is True, payload
        home = Path(runner.home)
        assert (home / 'config' / 'terminal.json').is_file() and (home / 'state').is_dir()
        assert not (home / 'current').exists()
        assert not runner.service_pids()
        hook_dir = Path.home() / 'Library/Application Support/asg-hooks'
        assert hook_dir.is_dir(), 'hook run dir must survive scanner uninstall'
        rc, payload, output, _ = runner.asgctl(None, 'uninstall')
        assert rc == 0 and payload.get('status') == 'not_installed', (rc, payload)
        return {}

    def install_one_hook_seed(ws_name, user_edit_after):
        # Create a learned (soc-direct-v1) install exactly the way the real
        # onboarding path does: the release's own learned_install writes the
        # transaction manifest, a package receipt sits in the derived state
        # directory, and the endpoint DB rows mirror a completed onboarding.
        python = Path(runner.home) / 'current' / 'python' / 'bin' / 'python3'
        app = Path(runner.home) / 'current' / 'app'
        ws = (run_dir / ws_name).resolve()
        ws.mkdir(parents=True, exist_ok=True)
        (ws / 'user-notes.md').write_text('unrelated user file\n')
        script = ('import json,sys,hashlib;sys.path.insert(0,%r);'
                  'from runtime import learned_install;from pathlib import Path;'
                  'ws=Path(%r);'
                  'state=ws.parent/(".asg-install-"+hashlib.sha256(str(ws).encode()).hexdigest()[:16]);'
                  'state.mkdir(parents=True,exist_ok=True);'
                  'plan={"version":1,"files":[{"path":"hooks/a.js","content":"module.exports={asg:true};"},'
                  '{"path":"settings.js","content":"hooks=[];"}]};'
                  'pid=learned_install.plan_digest(plan);'
                  'r=learned_install.install(plan,ws,state,approved_workspace=ws,approved_digest=pid);'
                  '(state/"package-receipt.json").write_text(json.dumps({"plan_digest":pid,"status":r["status"]}));'
                  'print(json.dumps({"digest":pid,"state":str(state)}))') % (str(app), str(ws))
        result = subprocess.run([str(python), '-B', '-c', script], capture_output=True, text=True, timeout=120)
        assert result.returncode == 0, (result.stdout + result.stderr)[-300:]
        body = json.loads(result.stdout.strip().splitlines()[-1])
        instance = ws_name + ':1000.0'
        db_path = Path(runner.home) / 'state' / 'endpoint' / 'outbox.sqlite'
        for _ in range(20):
            try:
                con = sqlite3.connect(db_path, timeout=5)
                with con:
                    con.execute('CREATE TABLE IF NOT EXISTS enrolled(instance TEXT PRIMARY KEY,configuration TEXT NOT NULL)')
                    con.execute('CREATE TABLE IF NOT EXISTS soc_onboarding(instance TEXT PRIMARY KEY,result TEXT)')
                    con.execute('INSERT OR REPLACE INTO enrolled VALUES(?,?)', (instance, json.dumps(
                        {'asg_instance_id': instance, 'workspace': str(ws),
                         'agent_id': soc_agent['id'], 'platform': 'e2e-platform',
                         'key_file': str(run_dir / (test_agent + '.key'))})))
                    con.execute('INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)', (instance, json.dumps(
                        {'status': 'installed', 'transport': 'soc-direct-v1'})))
                con.close()
                break
            except sqlite3.OperationalError:
                time.sleep(1)
        else:
            raise AssertionError('endpoint DB busy seeding hook receipts')
        if user_edit_after:
            (ws / 'hooks' / 'a.js').write_text('// asg hook edited by user later\n')
        return {'instance': instance, 'workspace': str(ws), 'state': body['state']}

    def step_reinstall_seed_hooks():
        # Re-install the same release after the default uninstall (proving the
        # uninstall really removed the release), then seed two learned hook
        # installs: one untouched, one user-modified after installation.
        rc, payload, output, _ = runner.asgctl(None, 'install', '--soc-url', runner.gateway,
                                               '--credential-file', str(cred),
                                               '--package', str(runner.package), *install_extra)
        assert rc == 0 and payload and payload.get('status') == 'installed', (rc, str(payload)[:200])
        clean = install_one_hook_seed('hook-ws-clean', False)
        modified = install_one_hook_seed('hook-ws-modified', True)
        results['hooks'] = {'clean': clean, 'modified': modified}
        # Doctor must flag exactly the user-modified install as drift, name the
        # file, keep the untouched install clean, and never treat missing
        # events as a broken hook.
        rc, payload, output, _ = runner.asgctl(None, 'doctor')
        checks = {item['name']: item for item in payload['checks']}
        detail = checks['Hook 安装']['detail']
        assert checks['Hook 安装']['ok'] is False, checks['Hook 安装']
        assert 'hook-ws-modified' in detail and 'a.js' in detail, detail
        assert 'hook-ws-clean' not in detail, detail
        assert checks['Hook 活动']['ok'] is True, checks['Hook 活动']
        return {'instances': [clean['instance'], modified['instance']]}

    def step_uninstall_remove_hooks_scoped():
        seed = results['hooks']
        terminal_cfg = Path(runner.home) / 'config' / 'terminal.json'
        data = json.loads(terminal_cfg.read_text())
        data['hook_test_user_key'] = 'keep2'
        terminal_cfg.write_text(json.dumps(data))
        rc, payload, output, _ = runner.asgctl(None, 'uninstall', '--remove-hooks')
        assert rc == 0, (rc, output[-300:])
        assert 'hook-ws-clean:1000.0' in (payload.get('hooks_removed') or []), payload
        conflicts = json.dumps(payload.get('hook_conflicts') or [], ensure_ascii=False)
        assert 'hook-ws-modified:1000.0' in conflicts, payload
        clean_ws = Path(seed['clean']['workspace'])
        assert not (clean_ws / 'hooks' / 'a.js').exists(), 'untouched install file not rolled back'
        assert not (clean_ws / 'settings.js').exists(), 'created settings not removed'
        assert (clean_ws / 'user-notes.md').read_text() == 'unrelated user file\n'
        mod_ws = Path(seed['modified']['workspace'])
        assert 'edited by user later' in (mod_ws / 'hooks' / 'a.js').read_text(), 'user edit destroyed'
        assert (mod_ws / 'user-notes.md').is_file()
        final_cfg = json.loads(terminal_cfg.read_text())
        assert final_cfg.get('acceptance_user_key') == 'keep-me'
        assert final_cfg.get('hook_test_user_key') == 'keep2'
        assert not runner.service_pids()
        rc, payload, _, _ = runner.asgctl(None, 'uninstall')
        assert rc == 0 and payload.get('status') == 'not_installed', (rc, payload)
        return {'removed': payload.get('hooks_removed'), 'modified_kept_as_conflict': True}

    order = [
        ('install_healthy_within_60s', step_install_once,
         '干净目录安装：60 秒内心跳健康，SOC 鉴权成功，rc0'),
        ('service_survives_package_removal', step_package_survives_source_removal,
         '删除/改名解压目录后服务心跳与子进程仍正常'),
        ('repeat_install_idempotent', step_repeat_install_idempotent,
         '重复安装 3 次：只有一个服务、一个运行实例，rc0 already_installed'),
        ('register_test_agent', step_register_test_agent,
         '隔离测试 Agent 经网关注册成功并写入采集配置'),
        ('first_collection_reaches_soc', step_first_collection_reports,
         '首轮清点快照到达 SOC，队列清空'),
        ('disconnect_queues_reconnect_drains', step_disconnect_queues_then_reconnect_drains,
         'SOC 不可达时队列增长；恢复后自动补报清空且无需重装'),
        ('soc_snapshots_no_duplicates', step_no_duplicate_snapshots,
         'SOC 快照数等于去重后的修订数（无逻辑重复）'),
        ('start_stop_cycles', step_start_stop_cycles,
         '10 轮 start 只有一个进程；stop 后 30 秒不被服务管理器拉起'),
        ('bad_credential_rc5_and_repair', step_bad_credential_rc5,
         '错误凭据：install rc5 报鉴权失败；更正凭据重装修复，不出现已连接误报'),
        ('negative_installs', step_negative_installs,
         '缺文件 rc2 点名缺失；篡改摘要 rc2 拒绝；SOC 离线仍可装成并明示离线'),
        ('doctor_fault_injection', step_doctor_fault_injection,
         '四类故障各自给出正确原因：坏凭据/网络断/缺文件/工作循环停滞；恢复后 OK'),
        ('seed_user_config_key', step_user_config_key_preserved,
         '写入用户自定义配置键，供升级后核验保留'),
        ('upgrade_a_to_b', step_upgrade_a_to_b,
         '升级后版本变新，用户配置键与流水线状态保留'),
        ('upgrade_failure_rolls_back_within_90s', step_upgrade_failure_rolls_back,
         '坏版本升级失败自动回滚：旧版本健康、队列记录不丢、可重复重试'),
        ('uninstall_keeps_data_and_hooks', step_uninstall_keeps_data_and_hooks,
         '默认卸载移除服务与程序但保留配置/状态/Hook 运行目录；二次运行 not_installed'),
        ('reinstall_seed_hooks', step_reinstall_seed_hooks,
         '重装同一发布包成功；种入两项学习 Hook 安装后，doctor 点名被用户修改的那项漂移、未修改项不误报'),
        ('uninstall_remove_hooks_scoped', step_uninstall_remove_hooks_scoped,
         '--remove-hooks 只回滚未修改的 ASG 写入文件；被用户修改的保留并报冲突；无关文件与用户配置键不动；再次卸载 not_installed'),
    ]
    failures = 0
    for name, fn, expected in order:
        if not runner.step(name, fn, expected):
            failures += 1

    report = run_dir / 'report.json'
    report.write_text(json.dumps({'package': str(runner.package),
                                  'package_manifest_sha256': sha256_file(runner.package / 'release.json'),
                                  'gateway': runner.gateway, 'steps': runner.steps,
                                  'failed': failures, 'home': str(runner.home)},
                                 ensure_ascii=False, indent=1))
    print('\nreport: %s' % report)
    if failures and not args.keep:
        pass  # keep evidence on failure either way; cleanup below only on success
    if not failures and not args.keep:
        subprocess.run([str(runner.package / 'asgctl'), '--home', str(runner.home), 'uninstall'],
                       capture_output=True, timeout=120)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
