"""Cross-platform, idempotent ASG setup/start/status/stop/doctor entry point."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import venv

ROOT = Path(__file__).resolve().parent


def python_path():
    return ROOT / '.venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def provision(offline=False):
    if not python_path().exists(): venv.EnvBuilder(with_pip=True).create(ROOT / '.venv')
    args = [str(python_path()), '-m', 'pip', 'install', '--disable-pip-version-check', '-r', str(ROOT / 'requirements.txt')]
    if offline: args += ['--no-index', '--find-links', str(ROOT / 'vendor/wheels')]
    subprocess.run(args, check=True)


def provision_goose(offline=False):
    """Use an existing CLI or install the official pinned release locally."""
    import shutil, platform, hashlib, tarfile, zipfile
    existing = os.environ.get('ASG_GOOSE_BIN') or shutil.which('goose')
    if existing and Path(existing).is_file():
        subprocess.run([existing, '--version'], check=True, timeout=30, capture_output=True)
        return str(Path(existing).resolve())
    folder = ROOT / '.tools/goose'
    executable = folder / ('goose.exe' if os.name == 'nt' else 'goose')
    if executable.exists(): return str(executable)
    if offline: raise ValueError('Offline setup needs Goose on PATH or ASG_GOOSE_BIN; use --skip-goose for protocol-only setup')
    arch = {'arm64': 'aarch64', 'aarch64': 'aarch64', 'x86_64': 'x86_64', 'AMD64': 'x86_64'}.get(platform.machine())
    suffix = {'darwin': 'apple-darwin.tar.gz', 'linux': 'unknown-linux-gnu.tar.gz', 'win32': 'pc-windows-msvc.zip'}.get(sys.platform)
    if not arch or not suffix: raise ValueError('No bundled Goose build for this platform; set ASG_GOOSE_BIN or --skip-goose')
    name = 'goose-' + arch + '-' + suffix
    release = json.load(urllib.request.urlopen('https://api.github.com/repos/block/goose/releases/tags/v1.50.1', timeout=30))
    asset = next((a for a in release['assets'] if a['name'] == name), None)
    if not asset or not str(asset.get('digest', '')).startswith('sha256:'): raise ValueError('Official Goose asset or SHA256 digest unavailable')
    folder.mkdir(parents=True, exist_ok=True)
    archive = folder / name
    try:
        with urllib.request.urlopen(asset['browser_download_url'], timeout=60) as response, archive.open('wb') as out:
            shutil.copyfileobj(response, out)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        if digest != asset['digest'].split(':', 1)[1]: raise ValueError('Goose download checksum mismatch')
        if name.endswith('.zip'):
            with zipfile.ZipFile(archive) as z:
                for item in z.infolist():
                    dest = (folder / item.filename).resolve()
                    if folder.resolve() not in dest.parents: raise ValueError('Unsafe Goose archive path')
                z.extractall(folder)
        else:
            with tarfile.open(archive) as t:
                for item in t.getmembers():
                    dest = (folder / item.name).resolve()
                    if folder.resolve() not in dest.parents or not (item.isfile() or item.isdir()): raise ValueError('Unsafe Goose archive entry')
                t.extractall(folder)
        if not executable.exists():
            matches = list(folder.rglob(executable.name))
            if len(matches) != 1: raise ValueError('Goose executable not found in official archive')
            executable = matches[0]
        executable.chmod(executable.stat().st_mode | 0o700)
        subprocess.run([str(executable), '--version'], check=True, timeout=30, capture_output=True)
        return str(executable)
    finally: archive.unlink(missing_ok=True)


def config_path(): return ROOT / 'deployment.local.json'


def load():
    if not config_path().exists(): raise ValueError('Run deploy.py setup first')
    return json.loads(config_path().read_text(encoding='utf-8'))


def own_process(record):
    import psutil
    if not record: return None
    try:
        p = psutil.Process(record['pid'])
        if abs(p.create_time() - record['create_time']) < .001 and str(ROOT / 'monitor_dashboard.py') in p.cmdline(): return p
    except psutil.Error: pass
    return None


def request(url):
    # Local health checks must not use an inherited HTTP proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=3) as response: return response.read()


def doctor(config):
    import importlib.util
    import shutil
    report = {'python': sys.version.split()[0], 'platform': sys.platform,
              'dependencies': {m: importlib.util.find_spec(m) is not None for m in ('psutil', 'yaml', 'requests', 'dotenv', 'tomlkit', 'json5', 'opentelemetry.proto')},
              'goose_available': bool(shutil.which('goose') or Path(config.get('ASG_GOOSE_BIN', '/nonexistent')).is_file()),
              'protocols': ['command_hooks: JSON/JSONC/TOML', 'ACP v1: managed stdio bridge', 'OTLP/HTTP: JSON/protobuf logs/traces'],
              'model_config': 'Configure and verify in the dashboard',
              'url': 'http://127.0.0.1:' + config['ASG_PORT']}
    try:
        report['http_ready'] = b'<html' in request(report['url'])
    except OSError: report['http_ready'] = False
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['setup', 'start', 'stop', 'status', 'doctor'], nargs='?', default='setup')
    parser.add_argument('--port', type=int, default=8081)
    parser.add_argument('--lan', action='store_true', help='Listen on LAN; dashboard is intended for trusted networks')
    parser.add_argument('--allow-root', action='append', default=[], help='Explicit directory where automated Hook installation is authorized')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--skip-goose', action='store_true', help='Deploy protocol channels without Goose fallback')
    parser.add_argument('--offline', action='store_true', help='Install dependencies from vendor/wheels')
    args = parser.parse_args()
    if sys.version_info < (3, 10): parser.error('Python 3.10+ required')
    if not 1 <= args.port <= 65535: parser.error('port must be 1..65535')
    if args.action == 'setup':
        provision(args.offline)
        goose = None if args.skip_goose else provision_goose(args.offline)
        if not config_path().exists():
            roots = [str(Path(p).expanduser().resolve()) for p in args.allow_root]
            for p in roots:
                if not Path(p).is_dir(): raise ValueError('Authorized root must exist: ' + p)
            config = {'ASG_HOST': '0.0.0.0' if args.lan else '127.0.0.1', 'ASG_PORT': str(args.port),
                      'ASG_RUN_DIR': str(ROOT / 'data'), 'ASG_FINGERPRINT_DB': str(ROOT / 'data/fingerprints.json'),
                      'ASG_PIPELINE': '1', 'ASG_AUTONOMOUS_ANALYSIS': '1', 'ASG_SCAN_INTERVAL': '60',
                      'ASG_ONBOARDING_AUTO_INSTALL': '1' if roots else '0',
                      'ASG_ONBOARDING_AUTHORIZED': '1' if roots else '0',
                      'ASG_ONBOARDING_SCOPE': 'project', 'ASG_ONBOARDING_WORKSPACE_ROOTS': os.pathsep.join(roots)}
            if goose: config['ASG_GOOSE_BIN'] = goose
            config_path().write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
            try: config_path().chmod(0o600)
            except OSError: pass
        # New deployments never inherit the development machine's fingerprints.
        run = Path(load()['ASG_RUN_DIR']); run.mkdir(parents=True, exist_ok=True)
        db = Path(load()['ASG_FINGERPRINT_DB'])
        if not db.exists(): db.write_text('{"version":1,"fingerprints":[]}', encoding='utf-8')
        if args.no_start:
            print('Setup complete; run deploy.py start'); return
        action = 'start'
    else: action = args.action
    # Re-exec under the managed environment; no global pip modifications.
    if Path(sys.executable).absolute() != python_path().absolute():
        command = [str(python_path()), str(ROOT / 'deploy.py'), action]
        if not python_path().exists(): raise ValueError('Run setup first')
        sys.exit(subprocess.call(command, cwd=ROOT))
    config = load(); run = Path(config['ASG_RUN_DIR']); run.mkdir(parents=True, exist_ok=True)
    record_path = run / 'deployment-process.json'
    record = json.loads(record_path.read_text()) if record_path.exists() else None
    process = own_process(record)
    if action == 'doctor':
        print(json.dumps(doctor(config), ensure_ascii=False, indent=2)); return
    if action == 'status':
        print(json.dumps({'running': bool(process), 'pid': process.pid if process else None, **doctor(config)}, ensure_ascii=False, indent=2)); return
    if action == 'stop':
        if process: process.terminate(); process.wait(timeout=15)
        record_path.unlink(missing_ok=True); print('Stopped'); return
    if process:
        if not doctor(config)['http_ready']: raise RuntimeError('Managed service exists but HTTP is unhealthy; inspect data/dashboard.log')
        print('Already running: http://127.0.0.1:' + config['ASG_PORT']); return
    import socket
    with socket.socket() as probe:
        if os.name != 'nt': probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((config['ASG_HOST'], int(config['ASG_PORT'])))
    from dotenv import dotenv_values
    env = {k: v for k, v in os.environ.items() if not k.startswith('ASG_')}
    env.update({k: v for k, v in dotenv_values(ROOT / '.env').items() if v is not None})
    env.update(config)
    env['PYTHONUTF8'] = '1'
    with (run / 'dashboard.log').open('ab') as log:
        child = subprocess.Popen([sys.executable, '-u', '-B', str(ROOT / 'monitor_dashboard.py')], cwd=ROOT,
                                 env=env, stdout=log, stderr=log, start_new_session=os.name != 'nt',
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
    import psutil
    record_path.write_text(json.dumps({'pid': child.pid, 'create_time': psutil.Process(child.pid).create_time()}))
    url = 'http://127.0.0.1:' + config['ASG_PORT']
    for _ in range(40):
        if child.poll() is not None: raise RuntimeError('Service exited; see data/dashboard.log')
        try:
            if b'<html' in request(url):
                print('Ready: ' + url); print('Configure the Goose model in the dashboard.'); return
        except OSError: pass
        time.sleep(.25)
    child.terminate(); child.wait(timeout=10); record_path.unlink(missing_ok=True)
    raise RuntimeError('Health check failed; see data/dashboard.log')


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print('Deployment failed: ' + str(exc), file=sys.stderr); sys.exit(1)
