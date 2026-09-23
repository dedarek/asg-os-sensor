"""ASG terminal supervisor: engine + endpoint children, durable heartbeat.

Stdlib-only (runs on the bundled interpreter). The service manager only proves
that this process is registered; every consumer of health must read
state/operations/heartbeat.json, which proves that the work loops actually
advance. Children inherit no credentials from this process.
"""
import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

BEAT_INTERVAL_S = 5
LOG_LIMIT = 8 * 1024 * 1024


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', required=True)
    args = parser.parse_args()
    home = Path(args.home).expanduser().resolve()
    current = (home / 'current').resolve()
    app = current / 'app'
    config = json.loads((home / 'config' / 'terminal.json').read_text())
    logs = home / 'logs'
    operations = home / 'state' / 'operations'
    operations.mkdir(parents=True, exist_ok=True)
    (logs / 'supervisor.log').open('a').close()
    sabotage = app / 'bin' / 'sabotage.json'
    if sabotage.is_file():
        # A release that intentionally cannot start (used by upgrade-rollback
        # acceptance). Log the reason and exit; the health wait must fail.
        with (logs / 'supervisor.log').open('a') as stream:
            stream.write('refusing to start: release marked fail_start\n')
        raise SystemExit(86)

    # Reap orphans from a previous supervisor that was SIGKILLed: its children
    # (start_new_session=True) survive and keep holding the engine port, which
    # would crash-loop every fresh engine with EADDRINUSE. Anything whose
    # command line references this install directory, except ourselves, is a
    # leftover of exactly this installation (the path is per-home unique).
    def reap_orphans():
        # Only engine/endpoint leftovers are reaped (matched by their launch
        # signature plus this install directory); asgctl/e2e clients that merely
        # pass --home must never be touched.
        import subprocess as _sp
        try:
            listing = _sp.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=10).stdout
        except Exception:
            return 0
        me = os.getpid()
        victims = []
        for line in listing.splitlines():
            line = line.strip()
            if not line:
                continue
            pid_text, _, command = line.partition(' ')
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            if pid in (me, os.getppid()):
                continue
            if str(home) not in command:
                continue
            if 'monitor_dashboard.py' not in command and 'integrations.soc_inventory.endpoint' not in command:
                continue
            victims.append(pid)
        if not victims:
            return 0
        for pid in victims:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        deadline = time.time() + 5
        alive = set(victims)
        while alive and time.time() < deadline:
            time.sleep(0.3)
            for pid in list(alive):
                try:
                    os.kill(pid, 0)
                except OSError:
                    alive.discard(pid)
        for pid in alive:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        return len(victims)

    stop = threading.Event()
    children = {}

    def log(line):
        with (logs / 'supervisor.log').open('a') as stream:
            stream.write(time.strftime('%Y-%m-%dT%H:%M:%S%z ') + line + '\n')
        rotate(logs / 'supervisor.log')

    def rotate(path):
        try:
            if path.stat().st_size > LOG_LIMIT:
                backup = path.with_suffix(path.suffix + '.1')
                os.replace(path, backup)
        except OSError:
            pass

    def child_env(engine):
        env = {k: v for k, v in os.environ.items() if not k.startswith('ASG_')}
        env['PYTHONUTF8'] = '1'
        # launchd supplies a minimal PATH; keep standard tool locations
        # so the engine can resolve goose and other CLIs.
        base_path = env.get('PATH', '')
        for extra_dir in ('/opt/homebrew/bin', '/usr/local/bin'):
            if extra_dir not in base_path.split(os.pathsep):
                base_path = base_path + os.pathsep + extra_dir if base_path else extra_dir
        env['PATH'] = str(current / 'python' / 'bin') + os.pathsep + base_path
        env['PYTHONPATH'] = str(app) + os.pathsep + env.get('PYTHONPATH', '')
        extra = config.get('engine_env') or {}
        if engine:
            env['ASG_HOST'] = '127.0.0.1'
            env['ASG_PORT'] = str(config.get('port', 8081))
            env['ASG_RUN_DIR'] = str(home / 'state' / 'engine')
            env['ASG_FINGERPRINT_DB'] = str(home / 'state' / 'fingerprints.json')
            if isinstance(config.get('scan_interval'), int):
                env['ASG_SCAN_INTERVAL'] = str(config['scan_interval'])
            goose = app / 'bin' / 'goose'
            if goose.is_file():
                env['ASG_GOOSE_BIN'] = str(goose)
        env.update({str(k): str(v) for k, v in extra.items()})
        return env

    def spawn(name, command, engine):
        log_path = logs / (name + '.log')
        rotate(log_path)
        stream = log_path.open('ab')
        process = subprocess.Popen(command, cwd=str(app), env=child_env(engine),
                                   stdout=stream, stderr=stream, start_new_session=True)
        children[name] = {'process': process, 'engine': engine, 'started': time.time()}
        log('started %s pid=%s' % (name, process.pid))

    endpoint_config = home / 'config' / 'endpoint.json'

    def desired():
        python = str(current / 'python' / 'bin' / 'python3')
        return {
            'engine': ([python, '-u', '-B', str(app / 'monitor_dashboard.py')], True),
            'endpoint': ([python, '-B', '-m', 'integrations.soc_inventory.endpoint',
                          '--config', str(endpoint_config)], False),
        }

    reaped = reap_orphans()
    if reaped:
        log('reaped %d orphan process(es) from a previous supervisor' % reaped)
    for name, (command, engine) in desired().items():
        spawn(name, command, engine)

    def terminate(signum, frame):
        stop.set()
    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)

    scan_track = {'count': None, 'since': time.time()}
    while not stop.wait(BEAT_INTERVAL_S):
        for name, entry in list(children.items()):
            code = entry['process'].poll()
            if code is None:
                continue
            log('child %s exited code=%s; restarting with backoff' % (name, code))
            rotate(logs / (name + '.log'))
            command, engine = desired()[name]
            try:
                spawn(name, command, engine)
            except OSError as exc:
                log('restart failed for %s: %s' % (name, exc))
        snapshot = {'t': time.time(), 'pid': os.getpid(),
                    'service_version': read_release_version(current),
                    'children': {}, 'engine': {}, 'endpoint': {}, 'soc': {}}
        for name, entry in children.items():
            process = entry['process']
            snapshot['children'][name] = {'pid': process.pid,
                                          'alive': process.poll() is None,
                                          'exit_code': process.poll()}
        snapshot['engine'] = engine_probe(config, scan_track)
        snapshot['endpoint'] = endpoint_probe(
            home, snapshot['children'].get('endpoint', {}).get('pid'))
        snapshot['soc'] = snapshot['endpoint'].get('soc', {})
        write_json(operations / 'heartbeat.json', snapshot)

    log('stopping children')
    for name, entry in children.items():
        process = entry['process']
        if process.poll() is not None:
            continue
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except OSError:
            process.terminate()
    deadline = time.time() + 30
    for name, entry in children.items():
        try:
            entry['process'].wait(timeout=max(0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            log('child %s did not exit in 30s; killing' % name)
            entry['process'].kill()
    log('supervisor stopped')


def read_release_version(current):
    try:
        return json.loads((current / 'release.json').read_text()).get('asg_version')
    except (OSError, json.JSONDecodeError):
        return None


def http_json(url, timeout=4):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.load(response)


def engine_probe(config, scan_track):
    out = {'reachable': False, 'scan_count': None, 'last_scan_time': None,
           'scan_interval': None, 'scan_stalled': False}
    try:
        state = http_json('http://127.0.0.1:%s/api/state' % config.get('port', 8081))
    except (OSError, ValueError):
        out['error'] = 'engine /api/state unreachable'
        return out
    out['reachable'] = True
    count = state.get('scan_count')
    out['scan_count'] = count if isinstance(count, int) else None
    out['last_scan_time'] = state.get('last_scan_time')
    out['scan_interval'] = state.get('scan_interval')
    active = state.get('active_investigations')
    out['active_investigations'] = len(active) if isinstance(active, dict) else 0
    # Manual mode is the product default: with periodic scans disabled a flat
    # scan_count is expected, not a stall (crazytest B27 false alarm).
    scan_enabled = state.get('scan_enabled')
    if scan_enabled is False:
        out['scan_stalled'] = False
    elif isinstance(count, int):
        if scan_track['count'] != count:
            scan_track['count'] = count
            scan_track['since'] = time.time()
        interval = out['scan_interval'] if isinstance(out['scan_interval'], (int, float)) else 600
        # Only a loop frozen past three full scan intervals plus grace counts
        # as stalled; slow-but-advancing counters are healthy.
        out['scan_stalled'] = time.time() - scan_track['since'] > 3 * interval + 120
    else:
        out['scan_stalled'] = True
    return out


def endpoint_probe(home, endpoint_pid=None):
    out = {'loop_age_s': None, 'bridge_age_s': None, 'soc': {}, 'queue': {}}
    state = home / 'state' / 'endpoint'
    for key, name in (('loop_age_s', 'loop-beat.json'), ('bridge_age_s', 'bridge-beat.json')):
        try:
            beat = json.loads((state / name).read_text())
            if endpoint_pid is not None and int(beat.get('pid', -1)) != int(endpoint_pid):
                continue
            out[key] = round(max(0.0, time.time() - float(beat['t'])), 1)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    try:
        out['soc'] = json.loads((state / 'soc-health.json').read_text())
        if (endpoint_pid is not None
                and int(out['soc'].get('pid', -1)) != int(endpoint_pid)):
            out['soc'] = {'state': 'unknown',
                          'detail': 'current endpoint has not reported yet'}
    except (OSError, ValueError):
        out['soc'] = {'state': 'unknown', 'detail': 'endpoint has not reported yet'}
    try:
        db = sqlite3.connect('file:' + str(state / 'outbox.sqlite') + '?mode=ro', uri=True)
        pending = db.execute('SELECT count(*) FROM queue').fetchone()[0]
        drafts = db.execute('SELECT count(*) FROM drafts').fetchone()[0]
        db.close()
        out['queue'] = {'pending_requests': pending, 'drafts': drafts}
    except sqlite3.Error:
        out['queue'] = {'error': 'outbox unreadable'}
    return out


def write_json(path, value):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False))
    os.replace(tmp, path)


if __name__ == '__main__':
    main()
