"""ASG terminal lifecycle entry point: install/start/stop/status/doctor/upgrade/uninstall.

Stdlib-only so it runs on the system python3 before any release is installed.
Every machine-readable decision comes from structured files (release.json,
installation.json, heartbeat.json), never from parsing human text.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

CONFIG_VERSION = 1
DB_SCHEMA_VERSION = 1
HEALTH_WAIT_S = 60


def default_home():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/ASG'
    if sys.platform.startswith('linux'):
        base = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local/share')
        return Path(base) / 'asg'
    raise SystemExit('unsupported platform: ' + sys.platform)


def default_label():
    return 'com.asg.terminal' if sys.platform == 'darwin' else 'asg-terminal'


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex[:6])
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def emit(args, payload, human, error=False):
    if getattr(args, 'json', False):
        print(json.dumps(payload, ensure_ascii=False))
    elif human is not None:
        print(human, file=sys.stderr if error else sys.stdout)


def verify_package(pkg):
    """Validate release.json plus every bundled file; raise ValueError with the
    first concrete reason (missing file, digest mismatch, platform mismatch)."""
    pkg = Path(pkg).expanduser().resolve()
    manifest = load_json(pkg / 'release.json')
    if not isinstance(manifest, dict):
        raise ValueError('安装包缺少 release.json')
    for key in ('asg_version', 'git_commit', 'platform', 'architecture', 'files',
                'config_version', 'db_schema_version'):
        if key not in manifest:
            raise ValueError('release.json 缺少字段: ' + key)
    if manifest['platform'] != platform.system() or manifest['architecture'] != platform.machine():
        raise ValueError('安装包平台不匹配: 包为 %s/%s，本机为 %s/%s' % (
            manifest['platform'], manifest['architecture'], platform.system(), platform.machine()))
    if int(manifest['config_version']) > CONFIG_VERSION:
        raise ValueError('安装包配置格式版本 %s 超出本 asgctl 支持 %s' % (
            manifest['config_version'], CONFIG_VERSION))
    if int(manifest['db_schema_version']) > DB_SCHEMA_VERSION:
        raise ValueError('安装包数据库版本 %s 超出本 asgctl 支持 %s' % (
            manifest['db_schema_version'], DB_SCHEMA_VERSION))
    for name in sorted(manifest['files']):
        entry = manifest['files'][name]
        target = pkg / name
        if entry.get('link') is not None:
            if not target.is_symlink() or os.readlink(target) != entry['link']:
                raise ValueError('符号链接缺失或被修改: ' + name)
            continue
        if target.is_symlink() or not target.is_file():
            raise ValueError('文件缺失或形态改变: ' + name)
        if sha256_file(target) != entry['sha256']:
            raise ValueError('文件摘要不一致: ' + name)
    return manifest


def purge_staging(parent):
    for stale in Path(parent).glob('*.staging-*'):
        shutil.rmtree(stale, ignore_errors=True)


def copy_release(pkg, destination):
    """Copy the bundle to a temp directory, verify digests post-copy, then rename."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    purge_staging(destination.parent)
    displaced = None
    if destination.is_dir():
        # Replacing an existing version directory (repair or re-install) must
        # not rename-into-a-non-empty-directory; move the old one aside first.
        displaced = destination.parent / (destination.name + '.displaced-' + uuid.uuid4().hex[:8])
        os.replace(destination, displaced)
    staging = destination.parent / (destination.name + '.staging-' + uuid.uuid4().hex[:8])
    staging.mkdir(parents=True)
    manifest = load_json(Path(pkg) / 'release.json')
    try:
        # release.json itself is not listed in files (self-referential digest);
        # copy it explicitly so current/release.json exists for doctor, status,
        # and the supervisor version probe.
        shutil.copy2(Path(pkg) / 'release.json', staging / 'release.json')
        for name in sorted(manifest['files']):
            entry = manifest['files'][name]
            source = Path(pkg) / name
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry.get('link') is not None:
                os.symlink(entry['link'], target)
                continue
            shutil.copy2(source, target)
            if entry.get('executable'):
                os.chmod(target, os.stat(target).st_mode | 0o755)
        for name in sorted(manifest['files']):
            entry = manifest['files'][name]
            if entry.get('link') is not None:
                continue
            target = staging / name
            if not target.is_file() or sha256_file(target) != entry['sha256']:
                raise ValueError('复制后摘要校验失败: ' + name)
        os.replace(staging, destination)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        if displaced is not None and not destination.exists():
            os.replace(displaced, destination)
            displaced = None
        raise
    if displaced is not None:
        shutil.rmtree(displaced, ignore_errors=True)
    return manifest


# ---------- platform service registration ----------

def launchd_domain():
    return 'gui/%d' % os.getuid()


def plist_path(label):
    return Path.home() / 'Library/LaunchAgents' / (label + '.plist')


def unit_path(label):
    return Path.home() / '.config/systemd/user' / (label + '.service')


def register_service(home, label):
    home = Path(home)
    program = [str(home / 'current/python/bin/python3'),
               str(home / 'current/app/service_main.py'), '--home', str(home)]
    _retire_legacy_collector_service()
    if sys.platform == 'darwin':
        import plistlib
        path = plist_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {'Label': label, 'ProgramArguments': program,
                   'WorkingDirectory': str(home / 'current/app'),
                   'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
                   'StandardOutPath': str(home / 'logs/service.log'),
                   'StandardErrorPath': str(home / 'logs/service.log')}
        path.write_bytes(plistlib.dumps(payload))
        subprocess.run(['launchctl', 'bootout', launchd_domain(), str(path)], capture_output=True)
        result = subprocess.run(['launchctl', 'bootstrap', launchd_domain(), str(path)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise ValueError('launchctl bootstrap 失败: ' + (result.stderr or '').strip())
    else:
        path = unit_path(label)
        path.parent.mkdir(parents=True, exist_ok=True)
        quoted = ' '.join('"%s"' % item.replace('\\', '\\\\').replace('"', '\\"') for item in program)
        path.write_text('[Unit]\nDescription=ASG terminal service\nAfter=default.target\n'
                        '[Service]\nType=simple\nWorkingDirectory=%s\nExecStart=%s\n'
                        'Restart=always\nRestartSec=10\n[Install]\nWantedBy=default.target\n'
                        % (str(home / 'current/app'), quoted))
        run_check(['systemctl', '--user', 'daemon-reload'])
        run_check(['systemctl', '--user', 'enable', '--now', label])


def unregister_service(label):
    if sys.platform == 'darwin':
        subprocess.run(['launchctl', 'bootout', launchd_domain(), str(plist_path(label))],
                       capture_output=True)
        plist_path(label).unlink(missing_ok=True)
    else:
        subprocess.run(['systemctl', '--user', 'disable', '--now', label], capture_output=True)
        unit_path(label).unlink(missing_ok=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
    _retire_legacy_collector_service()


LEGACY_COLLECTOR_LABEL = 'com.asg.soc-collector'


def _retire_legacy_collector_service():
    """Remove the pre-supervisor standalone collector service left behind by
    older installs.  The endpoint now runs under service_main, so a leftover
    definition would double-report to SOC on the next login."""
    try:
        if sys.platform == 'darwin':
            path = plist_path(LEGACY_COLLECTOR_LABEL)
            if path.exists():
                subprocess.run(['launchctl', 'bootout', launchd_domain(), str(path)],
                               capture_output=True)
                path.unlink(missing_ok=True)
        elif os.name == 'nt':
            # tools/soc_collector_service.py registered the legacy variant as a
            # scheduled task on Windows; deleting an absent task is a no-op error.
            subprocess.run(['schtasks', '/Delete', '/TN', LEGACY_COLLECTOR_LABEL, '/F'],
                           capture_output=True)
        else:
            unit = Path.home() / '.config/systemd/user' / (LEGACY_COLLECTOR_LABEL + '.service')
            if unit.exists():
                subprocess.run(['systemctl', '--user', 'disable', '--now', LEGACY_COLLECTOR_LABEL],
                               capture_output=True)
                unit.unlink(missing_ok=True)
                subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
    except OSError:
        pass


def service_loaded(label):
    if sys.platform == 'darwin':
        probe = subprocess.run(['launchctl', 'print', '%s/%s' % (launchd_domain(), label)],
                               capture_output=True, text=True)
        pid = next((line.split('=', 1)[1].strip().strip('();')
                    for line in probe.stdout.splitlines() if 'pid =' in line), None)
        return probe.returncode == 0, pid
    probe = subprocess.run(['systemctl', '--user', 'show', label,
                            '-p', 'ActiveState', '-p', 'MainPID'],
                           capture_output=True, text=True)
    fields = dict(line.split('=', 1) for line in probe.stdout.splitlines() if '=' in line)
    return fields.get('ActiveState') == 'active', fields.get('MainPID') or None


def run_check(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise ValueError('命令失败 %s: %s' % (' '.join(command), (result.stderr or '').strip()[:300]))
    return result


# ---------- health ----------

def read_heartbeat(home):
    return load_json(Path(home) / 'state' / 'operations' / 'heartbeat.json')


def evaluate_health(home, expect_version=None, max_age=25):
    hb = read_heartbeat(home)
    reasons = []
    if not isinstance(hb, dict):
        return {'healthy': False, 'reasons': ['没有心跳文件（服务从未启动）'], 'heartbeat': None}
    age = time.time() - float(hb.get('t', 0))
    if age > max_age:
        reasons.append('心跳已停止推进 %d 秒' % int(age))
    children = hb.get('children') or {}
    for name in ('engine', 'endpoint'):
        if not (children.get(name) or {}).get('alive'):
            reasons.append('子进程 %s 未运行' % name)
    engine = hb.get('engine') or {}
    if not engine.get('reachable'):
        reasons.append('发现/调查引擎无响应: ' + str(engine.get('error') or 'unreachable'))
    elif engine.get('scan_stalled'):
        reasons.append('发现循环停滞（超过 3 个扫描周期没有新的扫描）')
    endpoint = hb.get('endpoint') or {}
    loop_age = endpoint.get('loop_age_s')
    if loop_age is None:
        reasons.append('上报服务尚未写入循环心跳')
    elif loop_age > 120:
        reasons.append('上报服务循环 %d 秒未推进' % int(loop_age))
    bridge_age = endpoint.get('bridge_age_s')
    if bridge_age is not None and bridge_age > 180:
        reasons.append('桥接循环 %d 秒未推进' % int(bridge_age))
    if expect_version is not None and hb.get('service_version') != expect_version:
        reasons.append('运行版本 %s 与预期 %s 不一致' % (hb.get('service_version'), expect_version))
    return {'healthy': not reasons, 'reasons': reasons, 'heartbeat': hb}


def wait_healthy(home, expect_version=None, timeout=HEALTH_WAIT_S):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = evaluate_health(home, expect_version)
        if last['healthy']:
            return last
        time.sleep(2)
    return last or evaluate_health(home, expect_version)


# ---------- SOC probes ----------

def soc_probe(home):
    home = Path(home)
    terminal = load_json(home / 'config' / 'terminal.json') or {}
    out = {'network': 'unknown', 'auth': 'unknown', 'detail': ''}
    base = terminal.get('soc_url', '').rstrip('/')
    if not base:
        out['detail'] = '未配置 SOC 地址'
        return out
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(base + '/api/health', timeout=5) as response:
            out['network'] = 'ok' if response.status < 500 else 'error'
    except urllib.error.HTTPError as exc:
        out['network'] = 'reachable_error'
        out['detail'] = 'HTTP %s' % exc.code
    except Exception as exc:
        out['network'] = 'unreachable'
        out['detail'] = type(exc).__name__
        return out
    try:
        credential = (home / 'config' / 'credentials.json').read_text().strip()
    except OSError:
        out['detail'] = '凭据文件缺失'
        return out
    request = urllib.request.Request(base + '/api/asg/self',
                                     headers={'Authorization': 'Bearer ' + credential})
    try:
        with opener.open(request, timeout=5) as response:
            out['auth'] = 'ok' if response.status < 400 else 'error'
    except urllib.error.HTTPError as exc:
        out['auth'] = 'rejected' if exc.code in (401, 403) else 'error'
        out['detail'] = 'HTTP %s' % exc.code
    except Exception as exc:
        out['auth'] = 'error'
        out['detail'] = type(exc).__name__
    return out


def require_installed(home):
    home = Path(home)
    if not (home / 'config' / 'terminal.json').is_file() or not (home / 'current').is_symlink():
        raise SystemExit('未找到已安装的 ASG（%s）。请先执行 asgctl install。' % home)
    return load_json(home / 'config' / 'terminal.json')


def hook_run_dir(home):
    # Hooks live outside the ASG home so that uninstalling the scanner can
    # never remove the files a running Agent's Hook depends on.
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/asg-hooks'
    base = os.environ.get('XDG_DATA_HOME') or str(Path.home() / '.local/share')
    return Path(base) / 'asg-hooks'


# ---------- install ----------

def write_configs(home, args, version, label):
    home = Path(home)
    terminal = {'soc_url': (args.soc_url or '').rstrip('/'), 'port': args.port,
                'asg_version': version, 'service_label': label,
                'config_version': CONFIG_VERSION, 'db_schema_version': DB_SCHEMA_VERSION,
                'scan_interval': args.scan_interval,
                'collection_mode': args.collection_mode,
                'collect_interval': args.collect_interval}
    engine_env = dict(prior_env := (load_json(home / 'config' / 'terminal.json') or {}).get('engine_env') or {})
    for pair in getattr(args, 'engine_env', None) or []:
        if '=' not in pair:
            raise ValueError('--engine-env 需要 KEY=VALUE 形式: ' + pair)
        key, value = pair.split('=', 1)
        engine_env[key] = value
    if engine_env:
        terminal['engine_env'] = engine_env
    prior = load_json(home / 'config' / 'terminal.json')
    if isinstance(prior, dict):
        # Upgrades preserve keys asgctl does not own (user edits, deployment
        # notes); only the managed keys above are refreshed.
        merged = dict(prior)
        merged.update(terminal)
        terminal = merged
    endpoint_config = {'backend_url': args.soc_url.rstrip('/'),
                       'state_dir': str(home / 'state' / 'endpoint'),
                       'agents': [], 'interval_seconds': args.collect_interval,
                       'runtime_bridge': True, 'asg_url': 'http://127.0.0.1:%d' % args.port,
                       'collection_mode': args.collection_mode}
    prior_endpoint = load_json(home / 'config' / 'endpoint.json')
    if isinstance(prior_endpoint, dict):
        # An upgrade must never re-enable isolation opt-outs the operator chose
        # at install time (discovery, hook auto-install, skill upload) nor drop
        # configured agents; only managed connection fields are refreshed.
        for key in ('agents', 'discovery', 'soc_installation', 'upload_skill_content'):
            if key in prior_endpoint:
                endpoint_config[key] = prior_endpoint[key]
    else:
        endpoint_config['upload_skill_content'] = not getattr(args, 'skip_skill_upload', False)
        # --skip-soc-installation keeps an acceptance instance from writing
        # Hooks into real agents; production installs keep onboarding enabled.
        endpoint_config['soc_installation'] = not getattr(args, 'skip_soc_installation', False)
        if not getattr(args, 'no_discovery', False):
            # Lifecycle discovery registers real local agents with SOC.
            # Acceptance runs share the gateway, so they can opt out and report
            # only configured test agents instead.
            endpoint_config['discovery'] = {
                'application_key_file': str(home / 'config' / 'credentials.json')}
    write_json(home / 'config' / 'terminal.json', terminal, mode=0o600)
    write_json(home / 'config' / 'endpoint.json', endpoint_config, mode=0o600)


def save_credential(home, credential_file):
    text = Path(credential_file).expanduser().read_text().strip()
    if not text:
        raise ValueError('凭据文件为空: ' + str(credential_file))
    path = Path(home) / 'config' / 'credentials.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write(text)


def self_check(release_dir):
    python = release_dir / 'python/bin/python3'
    if not python.is_file():
        return '捆绑解释器缺失: ' + str(python)
    probe = subprocess.run([str(python), '-c',
                            'import sys; sys.path.insert(0, %r);'
                            'import integrations.soc_inventory.endpoint, integrations.soc_inventory.discovery,'
                            'runtime.matcher, runtime.hook_data, runtime.learned_install; import psutil;'
                            'print("self-check ok")' % str(release_dir / 'app')],
                           capture_output=True, text=True, timeout=60)
    if probe.returncode != 0:
        return '依赖或模块加载失败: ' + (probe.stderr or '')[-400:]
    try:
        import sqlite3
        probe_db = Path(release_dir).parent / '.sqlite-probe.sqlite'
        connection = sqlite3.connect(probe_db)
        connection.execute('CREATE TABLE IF NOT EXISTS probe(v INTEGER)')
        connection.execute('INSERT INTO probe VALUES(1)')
        connection.commit()
        connection.close()
        probe_db.unlink()
    except Exception as exc:
        return '本地 SQLite 不可用: %s' % exc
    return None


def seed_fingerprint_db(home, release_dir):
    """Merge shipped verified recipes into the writable runtime database.

    Locally learned families remain intact. A shipped id is authoritative for
    that id so an upgraded release can repair a previously published recipe;
    match counters and last-seen timestamps remain local runtime state.
    """
    source = load_json(Path(release_dir) / 'app/runtime/fingerprints.json')
    if not isinstance(source, dict) or not isinstance(source.get('fingerprints'), list):
        raise ValueError('交付包指纹库缺失或损坏')
    target = Path(home) / 'state' / 'fingerprints.json'
    if target.exists():
        current = load_json(target)
        if not isinstance(current, dict) or not isinstance(current.get('fingerprints'), list):
            raise ValueError('运行指纹库损坏，拒绝覆盖')
    else:
        current = {'version': source.get('version', 1), 'fingerprints': []}
    existing = {item.get('id'): item for item in current['fingerprints']
                if isinstance(item, dict) and item.get('id')}
    for shipped in source['fingerprints']:
        if not isinstance(shipped, dict) or not shipped.get('id'):
            raise ValueError('交付包包含无 id 的指纹')
        previous = existing.get(shipped['id'])
        replacement = json.loads(json.dumps(shipped))
        if previous:
            for key in ('match_count', 'last_seen'):
                if key in previous:
                    replacement[key] = previous[key]
            index = current['fingerprints'].index(previous)
            current['fingerprints'][index] = replacement
        else:
            current['fingerprints'].append(replacement)
        existing[shipped['id']] = replacement
    write_json(target, current, mode=0o600)
    return {'path': str(target), 'count': len(current['fingerprints']),
            'shipped': len(source['fingerprints'])}


def cmd_install(home, args):
    home = Path(home)
    label = args.service_label or default_label()
    pkg = Path(args.package).expanduser().resolve() if args.package else \
        Path(__file__).resolve().parents[1]
    if not (pkg / 'release.json').is_file():
        emit(args, {'command': 'install', 'status': 'failed',
                    'error': 'release.json missing in ' + str(pkg)},
             '目录 %s 不是交付包（缺少 release.json）。' % pkg, error=True)
        return 2
    try:
        manifest = verify_package(pkg)
    except ValueError as exc:
        emit(args, {'command': 'install', 'status': 'failed', 'error': str(exc)},
             '安装包校验失败: %s（未注册任何服务）' % exc, error=True)
        return 2
    version = manifest['asg_version']

    # Refresh the stored credential before short-circuiting on an existing
    # install: fixing a rejected credential and re-running install must recover
    # the connection without a reinstall (the endpoint re-reads this file per
    # request, so a restart is not even required).
    try:
        save_credential(home, args.credential_file)
    except (OSError, ValueError) as exc:
        emit(args, {'command': 'install', 'status': 'failed', 'error': str(exc)},
             '凭据不可用: %s（未注册任何服务）' % exc, error=True)
        return 2

    existing = load_json(home / 'state' / 'installation.json')
    if existing and (home / 'current').is_symlink():
        current_manifest = load_json(home / 'current/release.json') or {}
        if current_manifest.get('asg_version') == version:
            terminal = load_json(home / 'config' / 'terminal.json') or {}
            same = (terminal.get('soc_url') == args.soc_url.rstrip('/')
                    and terminal.get('port') == args.port)
            loaded, _ = service_loaded(terminal.get('service_label', label))
            health = evaluate_health(home, version)
            if same and health['healthy'] and loaded:
                try:
                    seed_fingerprint_db(home, home / 'current')
                except (OSError, ValueError) as exc:
                    emit(args, {'command': 'install', 'status': 'failed', 'error': str(exc)},
                         '服务健康，但配方库同步失败: %s' % exc, error=True)
                    return 1
                # Probe SOC even on the short-circuit path: re-running install
                # with a corrected credential must surface the new connection
                # state immediately (the endpoint re-reads the key per request).
                soc = soc_probe(home)
                result = {'command': 'install', 'status': 'already_installed',
                          'version': version, 'healthy': True, 'soc': soc}
                if soc['auth'] == 'rejected':
                    emit(args, result, '已安装版本 %s，服务健康；但 SOC 鉴权失败，'
                         '请更正凭据文件。' % version, error=True)
                    return 5
                emit(args, result, '已安装版本 %s，服务健康，无需重复安装。' % version)
                return 0
            # Residual same-version install: repair below (re-copy, re-register).
        else:
            emit(args, {'command': 'install', 'status': 'installed_other_version',
                        'version': current_manifest.get('asg_version')},
                 '已安装版本 %s；安装新版本请使用 asgctl upgrade --package。' %
                 current_manifest.get('asg_version'), error=True)
            return 2

    (home / 'releases').mkdir(parents=True, exist_ok=True)
    (home / 'config').mkdir(parents=True, exist_ok=True)
    (home / 'state' / 'operations').mkdir(parents=True, exist_ok=True)
    (home / 'logs').mkdir(parents=True, exist_ok=True)
    hook_run_dir(home).mkdir(parents=True, exist_ok=True)

    release_dir = home / 'releases' / version
    try:
        copy_release(pkg, release_dir)
    except (OSError, ValueError) as exc:
        emit(args, {'command': 'install', 'status': 'failed', 'error': str(exc)},
             '复制程序文件失败: %s（未注册任何服务）' % exc, error=True)
        return 1

    failure = self_check(release_dir)
    if failure:
        emit(args, {'command': 'install', 'status': 'failed', 'error': failure},
             '本地自检失败: %s（未注册任何服务）' % failure, error=True)
        return 1

    try:
        seed_fingerprint_db(home, release_dir)
    except (OSError, ValueError) as exc:
        emit(args, {'command': 'install', 'status': 'failed', 'error': str(exc)},
             '配方库初始化失败: %s（未注册任何服务）' % exc, error=True)
        return 1

    write_configs(home, args, version, label)

    previous_target = os.readlink(home / 'current') if (home / 'current').is_symlink() else None
    link = home / ('.current-link-' + uuid.uuid4().hex[:6])
    os.symlink(str(release_dir), link)
    os.replace(link, home / 'current')

    try:
        register_service(home, label)
    except ValueError as exc:
        if previous_target is not None:
            restore = home / ('.current-link-' + uuid.uuid4().hex[:6])
            os.symlink(previous_target, restore)
            os.replace(restore, home / 'current')
        emit(args, {'command': 'install', 'status': 'failed', 'error': str(exc)},
             '服务注册失败，未启动任何服务: %s' % exc, error=True)
        return 1

    health = wait_healthy(home, version)
    record = {'version': version, 'release_dir': str(release_dir), 'service_label': label,
              'installed_at': time.time(), 'updated_at': time.time(),
              'release_manifest_sha256': sha256_file(pkg / 'release.json'),
              'config_digest': sha256_file(home / 'config' / 'terminal.json'),
              'soc_url': args.soc_url.rstrip('/'), 'port': args.port, 'home': str(home)}
    write_json(home / 'state' / 'installation.json', record, mode=0o644)
    if not health['healthy']:
        emit(args, {'command': 'install', 'status': 'installed_unhealthy',
                    'version': version, 'reasons': health['reasons'],
                    'logs': str(home / 'logs')},
             '服务已注册但 60 秒内未通过健康检查: %s（诊断: %s/logs）'
             % ('; '.join(health['reasons']), home), error=True)
        return 1
    soc = soc_probe(home)
    result = {'command': 'install', 'status': 'installed', 'version': version,
              'healthy': True, 'soc': soc, 'home': str(home)}
    if soc['auth'] == 'rejected':
        emit(args, result, '服务已安装且运行正常；SOC 鉴权失败（凭据被拒绝）。'
             '请更正凭据文件后重启服务即可，无需重装。', error=True)
        return 5
    if soc['network'] not in ('ok', 'reachable_error'):
        emit(args, result, '安装成功；SOC 当前不可达（%s），发现与清点照常运行，'
             '数据暂存本机，恢复联网后自动补报。' % (soc.get('detail') or 'unreachable'))
        return 0
    emit(args, result, '安装成功，已连接 SOC。版本 %s。' % version)
    return 0


# ---------- start / stop / status ----------

def cmd_start(home, args):
    home = Path(home)
    terminal = require_installed(home)
    label = terminal.get('service_label', default_label())
    loaded, _ = service_loaded(label)
    health = evaluate_health(home, terminal.get('asg_version'))
    if loaded and health['healthy']:
        return report_status(home, args, 'start', note='正在运行')
    try:
        register_service(home, label)
    except ValueError as exc:
        emit(args, {'command': 'start', 'status': 'failed', 'error': str(exc)},
             '启动失败: %s' % exc, error=True)
        return 1
    health = wait_healthy(home, terminal.get('asg_version'))
    if not health['healthy']:
        emit(args, {'command': 'start', 'status': 'failed', 'reasons': health['reasons']},
             '60 秒内未恢复健康: %s' % '; '.join(health['reasons']), error=True)
        return 1
    return report_status(home, args, 'start')


def cmd_stop(home, args):
    home = Path(home)
    terminal = require_installed(home)
    label = terminal.get('service_label', default_label())
    loaded, pid = service_loaded(label)
    if not loaded:
        emit(args, {'command': 'stop', 'status': 'stopped'}, '服务本就未运行。')
        return 0
    unregister_service(label)
    deadline = time.time() + 30
    while time.time() < deadline:
        loaded, current = service_loaded(label)
        if not loaded:
            break
        time.sleep(1)
    loaded, current = service_loaded(label)
    if loaded:
        emit(args, {'command': 'stop', 'status': 'failed', 'pid': current},
             '30 秒后服务进程仍未退出（pid=%s）。' % current, error=True)
        return 1
    emit(args, {'command': 'stop', 'status': 'stopped', 'previous_pid': pid},
         '服务已停止，不会被自动拉起；待发送记录保留在本机。')
    return 0


def report_status(home, args, command, note=None):
    home = Path(home)
    terminal = load_json(home / 'config' / 'terminal.json') or {}
    health = evaluate_health(home, terminal.get('asg_version'))
    loaded, pid = service_loaded(terminal.get('service_label', default_label()))
    hb = health['heartbeat'] or {}
    endpoint = hb.get('endpoint') or {}
    engine = hb.get('engine') or {}
    payload = {'command': command, 'installed': True, 'version': terminal.get('asg_version'),
               'service': {'loaded': loaded, 'pid': pid}, 'healthy': health['healthy'],
               'health_reasons': health['reasons'], 'engine': engine, 'endpoint': endpoint,
               'soc': endpoint.get('soc') or {}, 'queue': endpoint.get('queue') or {},
               'beat_age_s': round(time.time() - float(hb.get('t', 0)), 1) if hb else None,
               'home': str(home)}
    if args.json:
        emit(args, payload, None)
        return 0 if health['healthy'] else 3
    soc = payload['soc']
    soc_line = {'ok': '正常', 'rejected': '鉴权失败', 'error': '请求失败',
                'unknown': '尚未上报'}.get(soc.get('state'), str(soc.get('state')))
    lines = [note or ('ASG 服务：%s' % ('运行中' if loaded else '未运行')),
             '版本：%s' % terminal.get('asg_version'),
             '健康：%s%s' % ('正常' if health['healthy'] else '异常',
                            '' if health['healthy'] else '（%s）' % '; '.join(health['reasons'])),
             'SOC 连接：%s（%s）' % (soc_line, soc.get('detail') or '-'),
             '最近上报检查：%s' % ('%d 秒前' % int(time.time() - float(soc['checked_at']))
                                if soc.get('checked_at') else '未知'),
             '待发送记录：%s' % payload['queue'].get('pending_requests', '未知'),
             '发现循环：%s（扫描计数 %s，间隔 %s 秒）' % (
                 '正常' if engine.get('reachable') and not engine.get('scan_stalled') else '异常',
                 engine.get('scan_count'), engine.get('scan_interval')),
             '上报循环心跳：%s' % ('%s 秒前' % endpoint.get('loop_age_s')
                              if endpoint.get('loop_age_s') is not None else '无'),
             '深度清点：%s' % terminal.get('collection_mode', '未知'),
             '数据目录：%s' % home]
    print('\n'.join(lines))
    return 0 if health['healthy'] else 3


def cmd_status(home, args):
    home = Path(home)
    if not (home / 'config' / 'terminal.json').is_file() or not (home / 'current').is_symlink():
        if args.json:
            print(json.dumps({'command': 'status', 'installed': False}, ensure_ascii=False))
        else:
            print('未安装。')
        return 4
    return report_status(home, args, 'status')


# ---------- doctor ----------

def load_json_str(value):
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def hook_install_records(home):
    # Learned installs keep a per-workspace receipt at
    # <workspace-parent>/.asg-install-<hash>/package-receipt.json; the endpoint
    # database lists enrolled instances and their onboarding receipts. Doctor
    # verifies exactly what ASG recorded it wrote, never a whole-disk scan.
    records = []
    db_path = Path(home) / 'state' / 'endpoint' / 'outbox.sqlite'
    if not db_path.is_file():
        return records
    try:
        import sqlite3
        connection = sqlite3.connect('file:' + str(db_path) + '?mode=ro', uri=True)
        rows = connection.execute('SELECT configuration FROM enrolled').fetchall()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        onboarding = dict(connection.execute('SELECT instance,result FROM soc_onboarding').fetchall()) \
            if 'soc_onboarding' in tables else {}
        connection.close()
    except Exception:
        return records
    for row in rows:
        try:
            agent = json.loads(row[0])
        except json.JSONDecodeError:
            continue
        prior = load_json_str(onboarding.get(agent.get('asg_instance_id')))
        if not prior or prior.get('status') not in (
                'installed', 'already_installed', 'installed_waiting_activation',
                'activation_verified'):
            continue
        if prior.get('transport') != 'soc-direct-v1':
            records.append({'instance': agent.get('asg_instance_id'), 'kind': 'native',
                            'status': prior.get('status')})
            continue
        # The Hook may be installed in a profile/config root different from the
        # process cwd. Doctor must resolve the same target the installer used.
        workspace = agent.get('hook_workspace') or agent.get('workspace')
        if not workspace:
            continue
        state = Path(workspace).expanduser().resolve().parent / ('.asg-install-' + hashlib.sha256(
            str(Path(workspace).expanduser().resolve()).encode()).hexdigest()[:16])
        records.append({'instance': agent.get('asg_instance_id'), 'kind': 'learned',
                        'workspace': str(Path(workspace).expanduser().resolve()),
                        'state_dir': str(state), 'status': prior.get('status')})
    return records


def cmd_doctor(home, args):
    home = Path(home)
    checks = []

    def check(name, ok, detail):
        checks.append({'name': name, 'ok': bool(ok), 'detail': detail})

    terminal = load_json(home / 'config' / 'terminal.json')
    record = load_json(home / 'state' / 'installation.json')
    # 1 program integrity against the current release manifest
    if record and (home / 'current/release.json').is_file():
        manifest = load_json(home / 'current/release.json') or {}
        bad = []
        for name in sorted(manifest.get('files', {})):
            entry = manifest['files'][name]
            target = home / 'current' / name
            if entry.get('link') is not None:
                if not target.is_symlink() or os.readlink(target) != entry['link']:
                    bad.append(name + '(链接)')
                continue
            if not target.is_file():
                bad.append(name + '(缺失)')
            elif sha256_file(target) != entry['sha256']:
                bad.append(name + '(摘要不一致)')
        check('程序完整性', not bad,
              '全部 %d 个文件与清单一致' % len(manifest.get('files', {})) if not bad
              else '；'.join(bad[:6]))
    else:
        check('程序完整性', False, '未安装或当前版本清单不可读')
    # 2 configuration
    problems = []
    if not terminal:
        problems.append('terminal.json 缺失或损坏')
    else:
        if not terminal.get('soc_url'):
            problems.append('soc_url 为空')
        if not isinstance(terminal.get('port'), int):
            problems.append('port 不是整数')
    if not (home / 'config' / 'credentials.json').is_file():
        problems.append('凭据文件缺失')
    if not (home / 'config' / 'endpoint.json').is_file():
        problems.append('endpoint.json 缺失')
    check('配置', not problems, '必填配置完整可读' if not problems else '；'.join(problems))
    # 3 local database
    try:
        import sqlite3
        probe = home / 'state' / 'operations' / '.doctor-probe.sqlite'
        connection = sqlite3.connect(probe)
        connection.execute('CREATE TABLE IF NOT EXISTS probe(v INTEGER)')
        connection.execute('INSERT INTO probe VALUES(1)')
        connection.rollback()
        connection.close()
        probe.unlink(missing_ok=True)
        check('本地数据库', True, 'state 目录 SQLite 可开事务读写')
    except Exception as exc:
        check('本地数据库', False, '数据库不可写或损坏: %s' % exc)
    # 4 service identity (pid cross-check, never launchctl alone)
    label = (terminal or {}).get('service_label', default_label())
    loaded, pid = service_loaded(label)
    hb = read_heartbeat(home) or {}
    if not (loaded and pid):
        check('服务身份', False, '服务未注册或没有运行进程')
    elif hb.get('pid') and str(hb.get('pid')) != str(pid):
        check('服务身份', False, '心跳进程 %s 与服务管理器 pid %s 不一致' % (hb.get('pid'), pid))
    else:
        check('服务身份', True, 'pid=%s 与心跳进程一致' % pid)
    # 5 work loops
    engine = hb.get('engine') or {}
    endpoint = hb.get('endpoint') or {}
    loop_age = endpoint.get('loop_age_s')
    stale = not isinstance(hb.get('t'), (int, float)) or time.time() - float(hb['t']) > 25
    if stale:
        check('工作循环', False, '心跳已停止推进（服务未运行或监督器卡死）')
    elif not engine.get('reachable'):
        check('工作循环', False, '发现/调查引擎无响应: ' + str(engine.get('error') or ''))
    elif engine.get('scan_stalled'):
        check('工作循环', False, '发现循环停滞（超过 3 个扫描周期无新扫描）')
    elif loop_age is None or loop_age > 120:
        check('工作循环', False, '上报服务循环未推进' + ('' if loop_age is None else '（%d 秒）' % loop_age))
    else:
        check('工作循环', True, '发现循环与上报循环均在推进')
    # 6/7 SOC network and auth are separate conclusions
    soc = soc_probe(home)
    check('SOC 网络', soc['network'] in ('ok', 'reachable_error'),
          '网关可达' if soc['network'] in ('ok', 'reachable_error')
          else '网络失败（DNS/连接/超时）: ' + str(soc.get('detail')))
    if soc['network'] not in ('ok', 'reachable_error'):
        check('SOC 鉴权', False, '网络不可达，无法验证鉴权')
    else:
        check('SOC 鉴权', soc['auth'] == 'ok',
              '凭据有效' if soc['auth'] == 'ok' else
              ('凭据失效或权限不足: ' + str(soc.get('detail')) if soc['auth'] == 'rejected'
               else '未能验证: ' + str(soc.get('detail'))))
    # 8 outbound queue backlog
    queue = endpoint.get('queue') or {}
    pending = queue.get('pending_requests')
    if isinstance(pending, int):
        check('上报队列', True, '待发送 %d 条（断连时正常增长，恢复后自动清空）' % pending)
    else:
        check('上报队列', False, str(queue.get('error') or '队列状态未知'))
    # 9 hook installation vs records; 10 hook activity (absence of events is
    # never reported as a broken hook, and installed is never activation proof)
    records = hook_install_records(home)
    if not records:
        check('Hook 安装', True, '本机没有 ASG 登记的 Hook 安装记录')
        check('Hook 活动', True, '无安装记录，不适用')
    else:
        drifted = []
        activated = 0
        for item in records:
            if item['kind'] != 'learned':
                continue
            receipt = load_json(Path(item['state_dir']) / 'package-receipt.json')
            if not receipt:
                drifted.append(item['instance'] + '：收据缺失')
                continue
            tx = load_json(Path(item['state_dir']) / (str(receipt.get('plan_digest')) + '.json'))
            if not tx or tx.get('status') != 'installed':
                drifted.append(item['instance'] + '：安装事务记录异常')
                continue
            mismatch = []
            for change in tx.get('changes', []):
                target = Path(item['workspace']) / change['path']
                try:
                    if hashlib.sha256(target.read_bytes()).hexdigest() != change.get('after_sha256'):
                        mismatch.append(change['path'])
                except OSError:
                    mismatch.append(change['path'])
            if mismatch:
                drifted.append(item['instance'] + '：文件与收据不一致 ' + ', '.join(mismatch[:3]))
            else:
                activation = load_json(Path(item['state_dir']) / 'activation-receipt.json')
                if activation:
                    expected_instance = '%s:%s' % (activation.get('pid'), activation.get('create_time'))
                    if (expected_instance == item['instance']
                            and activation.get('package_digest') == receipt.get('bundle_digest')
                            and activation.get('plan_digest') == receipt.get('plan_digest')):
                        activated += 1
        check('Hook 安装', not drifted,
              '已核对 %d 项，文件与记录一致（其中 %d 项有真实回调凭据）' % (len(records), activated)
              if not drifted else '；'.join(drifted[:5]))
        check('Hook 活动', True,
              '%d/%d 项已见真实回调；其余等待目标新活动（无事件不代表 Hook 损坏）'
              % (activated, len(records)))

    payload = {'command': 'doctor', 'checks': checks,
               'healthy': all(item['ok'] for item in checks)}
    if args.json:
        # one machine-readable line, like every other --json command
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for item in checks:
            print('%-10s %s  %s' % (item['name'], 'OK  ' if item['ok'] else 'FAIL', item['detail']))
    return 0 if payload['healthy'] else 3


# ---------- upgrade ----------

def cmd_upgrade(home, args):
    home = Path(home)
    terminal = require_installed(home)
    label = terminal.get('service_label', default_label())
    pkg = Path(args.package).expanduser().resolve()
    try:
        manifest = verify_package(pkg)
    except ValueError as exc:
        emit(args, {'command': 'upgrade', 'status': 'failed', 'error': str(exc)},
             '新包校验失败: %s（当前安装保持不变）' % exc, error=True)
        return 2
    new_version = manifest['asg_version']
    old_version = terminal.get('asg_version')
    if new_version == old_version and not args.force:
        emit(args, {'command': 'upgrade', 'status': 'same_version', 'version': new_version},
             '当前已是版本 %s；如需重装同一版本请加 --force。' % new_version)
        return 0

    release_dir = home / 'releases' / new_version
    try:
        copy_release(pkg, release_dir)
    except (OSError, ValueError) as exc:
        emit(args, {'command': 'upgrade', 'status': 'failed', 'error': str(exc)},
             '复制新版本失败: %s（当前安装保持不变）' % exc, error=True)
        return 1
    failure = self_check(release_dir)
    if failure:
        shutil.rmtree(release_dir, ignore_errors=True)
        emit(args, {'command': 'upgrade', 'status': 'failed', 'error': failure},
             '新版本离线自检失败: %s（当前安装保持不变）' % failure, error=True)
        return 1

    # Backup config and the durable database before touching the live release.
    stamp = time.strftime('%Y%m%d-%H%M%S')
    backup = home / 'state' / 'backups' / ('pre-upgrade-' + stamp)
    backup.mkdir(parents=True, exist_ok=True)
    for name in ('terminal.json', 'endpoint.json', 'credentials.json'):
        source = home / 'config' / name
        if source.is_file():
            shutil.copy2(source, backup / name)
    database = home / 'state' / 'endpoint' / 'outbox.sqlite'
    if database.is_file():
        try:
            import sqlite3
            source = sqlite3.connect(database)
            target = sqlite3.connect(backup / 'outbox.sqlite')
            source.backup(target)
            source.close()
            target.close()
        except Exception as exc:
            emit(args, {'command': 'upgrade', 'status': 'failed', 'error': 'db backup: %s' % exc},
                 '数据库备份失败（当前安装保持不变）', error=True)
            return 1
    fingerprint = home / 'state' / 'fingerprints.json'
    fingerprint_existed = fingerprint.is_file()
    if fingerprint_existed:
        shutil.copy2(fingerprint, backup / 'fingerprints.json')

    def restore_fingerprints():
        saved = backup / 'fingerprints.json'
        if saved.is_file():
            shutil.copy2(saved, fingerprint)
            os.chmod(fingerprint, 0o600)
        elif not fingerprint_existed:
            fingerprint.unlink(missing_ok=True)

    old_target = os.readlink(home / 'current')
    unregister_service(label)
    try:
        seed_fingerprint_db(home, release_dir)
    except (OSError, ValueError) as exc:
        restore_fingerprints()
        register_service(home, label)
        emit(args, {'command': 'upgrade', 'status': 'failed', 'error': str(exc)},
             '新版本配方库合并失败（已恢复原服务）', error=True)
        return 1
    args.scan_interval = terminal.get('scan_interval', args.scan_interval)
    args.collection_mode = terminal.get('collection_mode', args.collection_mode)
    args.collect_interval = terminal.get('collect_interval', args.collect_interval)
    args.soc_url = args.soc_url or terminal.get('soc_url', '')
    args.port = args.port if args.port is not None else terminal.get('port', 8081)
    write_configs(home, args, new_version, label)
    link = home / ('.current-link-' + uuid.uuid4().hex[:6])
    os.symlink(str(release_dir), link)
    os.replace(link, home / 'current')
    try:
        register_service(home, label)
    except ValueError as exc:
        restore_fingerprints()
        restore = home / ('.current-link-' + uuid.uuid4().hex[:6])
        os.symlink(old_target, restore)
        os.replace(restore, home / 'current')
        register_service(home, label)
        emit(args, {'command': 'upgrade', 'status': 'failed_rolled_back', 'error': str(exc)},
             '升级失败，已恢复旧版本 %s 并重启服务。' % old_version, error=True)
        return 1

    health = wait_healthy(home, new_version)
    if not health['healthy']:
        unregister_service(label)
        restore_fingerprints()
        for name in ('terminal.json', 'endpoint.json', 'credentials.json'):
            source = backup / name
            if source.is_file():
                target = home / 'config' / name
                target.write_bytes(source.read_bytes())
                os.chmod(target, 0o600)
        if (backup / 'outbox.sqlite').is_file():
            try:
                import sqlite3
                source = sqlite3.connect(backup / 'outbox.sqlite')
                target = sqlite3.connect(home / 'state' / 'endpoint' / 'outbox.sqlite')
                source.backup(target)
                source.close()
                target.close()
            except Exception:
                pass
        restore = home / ('.current-link-' + uuid.uuid4().hex[:6])
        os.symlink(old_target, restore)
        os.replace(restore, home / 'current')
        register_service(home, label)
        old_health = wait_healthy(home, old_version, timeout=90)
        record = load_json(home / 'state' / 'installation.json') or {}
        record['upgraded_failed_at'] = time.time()
        write_json(home / 'state' / 'installation.json', record, mode=0o644)
        emit(args, {'command': 'upgrade', 'status': 'failed_rolled_back',
                    'new_version': new_version, 'restored': old_version,
                    'reasons': health['reasons'], 'backup': str(backup),
                    'old_healthy': old_health['healthy']},
             '升级失败，已恢复旧版本 %s（备份: %s）。' % (old_version, backup), error=True)
        return 1

    record = load_json(home / 'state' / 'installation.json') or {}
    record.update({'version': new_version, 'release_dir': str(release_dir),
                   'updated_at': time.time(), 'upgraded_from': old_version,
                   'backup': str(backup),
                   'release_manifest_sha256': sha256_file(pkg / 'release.json')})
    write_json(home / 'state' / 'installation.json', record, mode=0o644)
    keep = {old_version, new_version}
    for stale in sorted((home / 'releases').iterdir()):
        if stale.is_dir() and stale.name not in keep:
            shutil.rmtree(stale, ignore_errors=True)
    emit(args, {'command': 'upgrade', 'status': 'upgraded', 'version': new_version,
                'from': old_version, 'backup': str(backup)},
         '升级成功：%s → %s（备份: %s）。Agent Hook 不会被自动重写。' % (old_version, new_version, backup))
    return 0


# ---------- uninstall ----------

def is_release_layout(directory):
    # Only directories that match our own release layout are eligible for
    # cleanup after uninstall; anything a user placed there stays untouched.
    return (Path(directory) / 'app/service_main.py').is_file() and \
        (Path(directory) / 'release.json').is_file()


def remove_hooks(home, args):
    """Remove only ASG-written hook content that still matches its receipt."""
    removed = []
    conflicts = []
    notes = []
    python = home / 'current/python/bin/python3'
    if not python.is_file():
        python = Path(sys.executable)
    for item in hook_install_records(home):
        if item['kind'] != 'learned':
            notes.append('%s：原生 Hook 由目标自身的信任机制管理，请在 Agent 内确认卸载'
                         % item['instance'])
            continue
        receipt = load_json(Path(item['state_dir']) / 'package-receipt.json')
        if not receipt:
            conflicts.append(item['instance'] + '：收据缺失，保留现状')
            continue
        # Roll the exact learned plan back through its transaction manifest.
        # learned_install refuses (never overwrites) when a recorded file was
        # modified after installation, which is the required conflict path.
        script = ('import json,sys;from pathlib import Path;'
                  'sys.path.insert(0,%r);from runtime import learned_install;'
                  'receipt=json.loads(Path(%r).read_text());'
                  'ws=Path(%r);state=Path(%r);'
                  'r=learned_install.rollback(ws,state,approved_workspace=ws,'
                  'approved_digest=receipt["plan_digest"]);'
                  'print(json.dumps(r))'
                  % (str(home / 'current/app'), str(Path(item['state_dir']) / 'package-receipt.json'),
                     item['workspace'], item['state_dir']))
        result = subprocess.run([str(python), '-B', '-c', script],
                                capture_output=True, text=True, timeout=60)
        line = (result.stdout or '').strip().splitlines()
        body = load_json_str(line[-1] if line else None)
        if result.returncode == 0 and body and body.get('status') in ('rolled_back', 'already_rolled_back'):
            removed.append(item['instance'])
        else:
            detail = (result.stderr or '').strip().splitlines()
            detail = detail[-1] if detail else str(body)
            conflicts.append(item['instance'] + '：' + str(detail)[:160])
    return removed, conflicts, notes


def cmd_uninstall(home, args):
    home = Path(home)
    terminal = load_json(home / 'config' / 'terminal.json')
    record = load_json(home / 'state' / 'installation.json')
    if (not record and not (home / 'current').is_symlink()) or \
            (record and not (home / 'current').is_symlink()
             and not any((home / 'releases').glob('*/app/service_main.py'))):
        emit(args, {'command': 'uninstall', 'status': 'not_installed'}, '已卸载（没有安装记录）。')
        return 0
    label = (terminal or record or {}).get('service_label') or default_label()
    unregister_service(label)
    deadline = time.time() + 30
    while time.time() < deadline:
        loaded, _ = service_loaded(label)
        if not loaded:
            break
        time.sleep(1)

    removed, conflicts, notes = ([], [], [])
    if args.remove_hooks:
        removed, conflicts, notes = remove_hooks(home, args)

    if record and record.get('release_dir'):
        shutil.rmtree(record['release_dir'], ignore_errors=True)
    if (home / 'releases').is_dir():
        for stale in (home / 'releases').iterdir():
            if stale.is_dir() and is_release_layout(stale):
                shutil.rmtree(stale, ignore_errors=True)
        purge_staging(home / 'releases')
    if (home / 'current').is_symlink():
        (home / 'current').unlink()
    for stale in home.glob('.current-link-*'):
        stale.unlink(missing_ok=True)

    loaded, pid = service_loaded(label)
    result = {'command': 'uninstall', 'status': 'uninstalled', 'service_gone': not loaded,
              'kept': {'config': str(home / 'config'), 'state': str(home / 'state'),
                       'logs': str(home / 'logs'), 'hook_run_dir': str(hook_run_dir(home))},
              'hooks_removed': removed, 'hook_conflicts': conflicts, 'notes': notes,
              'remove_hooks_requested': bool(args.remove_hooks)}
    write_json(home / 'state' / 'uninstalled.json',
               {'uninstalled_at': time.time(), 'version': (record or {}).get('version')},
               mode=0o644)
    # The installation record itself goes: the retained state directory keeps
    # uninstalled.json, and a second uninstall must report not_installed.
    (home / 'state' / 'installation.json').unlink(missing_ok=True)
    human = 'ASG 服务与程序已移除；配置、数据与日志保留在 %s。' % home
    if args.remove_hooks:
        human += ' Hook：移除 %d 项。' % len(removed)
        if conflicts:
            human += ' 保留冲突项：' + '；'.join(conflicts)
    emit(args, result, human)
    return 0 if not loaded else 1


# ---------- entry ----------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS defaults so the sub-parser never clobbers a value parsed before
    # the subcommand; both "asgctl --json status" and "asgctl status --json" work.
    common.add_argument('--home', default=argparse.SUPPRESS,
                        help='ASG data directory (default: platform user directory)')
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                        help='machine-readable output')
    parser = argparse.ArgumentParser(prog='asgctl', parents=[common],
                                     description='ASG terminal lifecycle management')
    sub = parser.add_subparsers(dest='command', required=True)

    install = sub.add_parser('install', parents=[common],
                             help='install and start the ASG terminal service')
    install.add_argument('--soc-url', required=True)
    install.add_argument('--credential-file', required=True)
    install.add_argument('--package', default=None,
                         help='delivery package directory (default: package containing asgctl)')
    install.add_argument('--port', type=int, default=8081)
    install.add_argument('--scan-interval', type=int, default=60)
    install.add_argument('--collect-interval', type=int, default=600)
    install.add_argument('--collection-mode', choices=['manual', 'auto'], default='manual',
                         help='deep inventory mode; manual means user-triggered')
    install.add_argument('--service-label', default=None)
    install.add_argument('--engine-env', action='append', metavar='KEY=VALUE',
                         help='extra environment for the engine child (repeatable)')
    install.add_argument('--skip-soc-installation', action='store_true',
                         help='do not auto-write Hooks into discovered agents')
    install.add_argument('--skip-skill-upload', action='store_true',
                         help='report inventory without uploading skill packages')
    install.add_argument('--no-discovery', action='store_true',
                         help='do not auto-register discovered local agents with SOC')

    sub.add_parser('start', parents=[common], help='start the service')
    sub.add_parser('stop', parents=[common], help='stop the service (data and hooks stay)')
    sub.add_parser('status', parents=[common], help='runtime status (--json for machine output)')
    sub.add_parser('doctor', parents=[common], help='run diagnostics (never modifies configuration)')

    upgrade = sub.add_parser('upgrade', parents=[common],
                             help='upgrade with automatic rollback on failure')
    upgrade.add_argument('--package', required=True)
    upgrade.add_argument('--force', action='store_true')
    upgrade.add_argument('--soc-url', default='')
    upgrade.add_argument('--port', type=int, default=None)
    upgrade.add_argument('--scan-interval', type=int, default=60)
    upgrade.add_argument('--collect-interval', type=int, default=600)
    upgrade.add_argument('--collection-mode', choices=['manual', 'auto'], default='manual')
    upgrade.add_argument('--engine-env', action='append', metavar='KEY=VALUE')
    upgrade.add_argument('--skip-soc-installation', action='store_true')
    upgrade.add_argument('--skip-skill-upload', action='store_true')
    upgrade.add_argument('--no-discovery', action='store_true')

    uninstall = sub.add_parser('uninstall', parents=[common],
                               help='remove service and programs (hooks and data kept)')
    uninstall.add_argument('--remove-hooks', action='store_true',
                           help='also remove ASG-written hook files that are unmodified')
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    home_value = getattr(args, 'home', None)
    home = Path(home_value).expanduser().resolve() if home_value else default_home()
    args.json = bool(getattr(args, 'json', False))
    actions = {'install': cmd_install, 'start': cmd_start, 'stop': cmd_stop,
               'status': cmd_status, 'doctor': cmd_doctor,
               'upgrade': cmd_upgrade, 'uninstall': cmd_uninstall}
    try:
        return actions[args.command](home, args)
    except SystemExit:
        raise
    except Exception as exc:  # one clear failure line, never a raw traceback UX
        emit(args, {'command': args.command, 'status': 'failed',
                    'error': type(exc).__name__ + ': ' + str(exc)},
             '命令 %s 失败: %s' % (args.command, exc), error=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
