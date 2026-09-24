"""Build a self-contained ASG terminal delivery package.

Produces asg-terminal/ with: asgctl launcher, release.json (version, git commit,
platform, per-file SHA-256, config/db schema versions), app/ (engine + endpoint +
service supervisor), python/ (bundled standalone interpreter with pinned wheels),
services/ templates, README.md. Stdlib-only orchestrator; the bundled interpreter's
pip is used to install third-party dependencies into python/lib/site-packages.

Usage:
  python3 release/build_release.py --version 0.9.1 --out /tmp/out
  python3 release/build_release.py --version 0.9.3 --sabotage   # cannot-start build
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path(os.environ.get('ASG_REL_CACHE', '/tmp/asg-relcache'))

PYTHON_DIST = ('cpython-3.11.16+20260901-aarch64-apple-darwin'
               '-install_only_stripped.tar.gz')
PYTHON_URL = ('https://github.com/astral-sh/python-build-standalone/releases/download/'
              '20260901/' + PYTHON_DIST)
PYTHON_SHA256 = ('768f05cf200273bbdda9a5955a5a6892a4b22f2a0b1e4b0'
                 'a9160f5c7fce86816')

# Third-party modules imported (even lazily) by the engine or the endpoint.
DEPENDENCIES = ['psutil', 'pyyaml', 'tomlkit', 'json5', 'python-dotenv',
                'protobuf', 'opentelemetry-proto',
                # runtime/llm_proxy.py imports requests at module scope; the
                # 0.9.49 release shipped without it and every goose
                # investigation died with ModuleNotFoundError on first use.
                'requests']

# Repo files copied into app/ (paths relative to the repository root).
# Root data files are read relative to the app directory (monitor_dashboard
# ROOT / asg_os_sensor BASE / runtime module parents[1]): without them the
# scanner loop dies at import-time reads, so they are mandatory.
APP_ENTRIES = ['monitor_dashboard.py', 'asg_os_sensor.py', 'policies.yaml',
               'identities.yaml', 'llm.yaml', 'web', 'recipes', 'runtime',
               'integrations']


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def run(command, cwd=None, env=None):
    result = subprocess.run([str(part) for part in command], cwd=str(cwd or ROOT),
                            env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit('构建失败: %s\n%s' % (' '.join(map(str, command)),
                                               (result.stderr or result.stdout)[-800:]))
    return result


def copy_app(pkg):
    app = pkg / 'app'
    for entry in APP_ENTRIES:
        source = ROOT / entry
        target = app / entry
        if not source.exists():
            raise SystemExit('构建失败: 仓库缺少 %s' % entry)
        if source.is_dir():
            shutil.copytree(source, target, ignore=shutil.ignore_patterns(
                '__pycache__', '*.pyc', 'test_*', '*.bak-*', '*.lock'))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    # goose is optional: investigation falls back without it, but bundle it
    # when available on the build machine (copied dereferenced, chmod 0755).
    goose = Path(os.environ.get('ASG_GOOSE_BIN', '/opt/homebrew/bin/goose'))
    if goose.exists():
        target = app / 'bin' / 'goose'
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(goose.resolve(), target)
        os.chmod(target, 0o755)


def sanitize_portability(pkg):
    """Strip learning-machine specifics from the packaged fingerprint library.

    runtime/fingerprints.json accumulates recipes learned on the build
    machine; their plans and baselines reference paths under this user's home
    (crazytest B17: 63 hits shipped in one release).  A release that carries
    them cannot satisfy its own README promise (reuse on fresh machines):
    installs would fail with misleading precondition errors on every other
    host.  Recipes whose plan content mentions a home prefix are dropped from
    the package (the build machine keeps them in its live library), and the
    drop list is recorded in release.json for auditability.
    """
    home = str(Path.home())
    # crazytest B26: Windows drive letters are case-insensitive; the previous
    # literal 'C:\\Users\\' missed lowercase c:\\users\\ entries, which then
    # shipped a foreign machine's home path inside release packages.
    other_homes = ('/home/', ':' + chr(92) + 'users' + chr(92))
    fp = pkg / 'app' / 'runtime' / 'fingerprints.json'
    if not fp.exists():
        return
    data = json.loads(fp.read_text())
    kept, dropped = [], []
    for entry in data.get('fingerprints', []):
        blob = json.dumps(entry, ensure_ascii=False)
        folded = blob.replace(chr(92) * 2, chr(92)).lower()
        why = None
        if home in blob:
            why = 'build-machine home path'
        elif any(marker in folded for marker in other_homes):
            why = 'foreign user-home absolute path'
        if why:
            dropped.append({'id': entry.get('id'), 'name': entry.get('name'), 'reason': why})
        else:
            kept.append(entry)
    data['fingerprints'] = kept
    fp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    (pkg / 'release' / 'portability-scan.json').write_text(json.dumps(
        {'build_home_redacted': True, 'fingerprints_kept': len(kept),
         'fingerprints_dropped': dropped}, ensure_ascii=False, indent=1))
    if dropped:
        with (pkg / 'README.md').open('a') as readme:
            readme.write(
            '\n## 指纹库可移植性\n构建期从包内指纹库剔除了 %d 个含构建机路径的配方'
            '（清单见 release/portability-scan.json）。目标机器首次遇到这些类型时'
            '由发现/调查链路现场学习，学习结果保留在运行库中，不回写发布包。\n' % len(dropped))


def bundle_python(pkg, wheels_dir):
    tar = CACHE / 'pbs.tar.gz'
    tar.parent.mkdir(parents=True, exist_ok=True)
    if not tar.is_file() or sha256_file(tar) != PYTHON_SHA256:
        print('下载捆绑解释器...', flush=True)
        temporary = tar.with_suffix('.download-' + uuid.uuid4().hex[:6])
        request = urllib.request.Request(PYTHON_URL, headers={'User-Agent': 'asg-build'})
        with urllib.request.urlopen(request, timeout=600) as response, temporary.open('wb') as out:
            shutil.copyfileobj(response, out)
        if sha256_file(temporary) != PYTHON_SHA256:
            temporary.unlink(missing_ok=True)
            raise SystemExit('捆绑解释器下载摘要不一致，拒绝使用')
        os.replace(temporary, tar)
    extract = pkg / 'python'
    with tarfile.open(tar) as archive:
        archive.extractall(pkg / '.pbs')
    os.replace(pkg / '.pbs' / 'python', extract)
    (pkg / '.pbs').rmdir()
    python = extract / 'bin' / 'python3'
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    run([python, '-m', 'pip', 'install', '--no-compile',
         '--cache-dir', wheels_dir, '--target', extract / 'lib/python3.11/site-packages']
        + DEPENDENCIES, env=env)
    return python


def write_launcher(pkg):
    launcher = pkg / 'asgctl'
    launcher.write_text('#!/bin/sh\nDIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
                        'exec "$DIR/python/bin/python3" "$DIR/release/asgctl.py" "$@"\n')
    os.chmod(launcher, 0o755)


def write_services(pkg):
    services = pkg / 'services'
    services.mkdir(parents=True, exist_ok=True)
    (services / 'com.asg.terminal.plist.template').write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '<key>Label</key><string>com.asg.terminal</string>\n'
        '<key>ProgramArguments</key><array>\n'
        '<string>__HOME__/current/python/bin/python3</string>\n'
        '<string>__HOME__/current/app/service_main.py</string>\n'
        '<string>--home</string><string>__HOME__</string></array>\n'
        '<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>\n'
        '<key>ThrottleInterval</key><integer>10</integer>\n'
        '<key>StandardOutPath</key><string>__HOME__/logs/service.log</string>\n'
        '<key>StandardErrorPath</key><string>__HOME__/logs/service.log</string>\n'
        '</dict></plist>\n')
    (services / 'asg-terminal.service.template').write_text(
        '[Unit]\nDescription=ASG terminal service\nAfter=default.target\n'
        '[Service]\nType=simple\nWorkingDirectory=__HOME__/current/app\n'
        'ExecStart=__HOME__/current/python/bin/python3 __HOME__/current/app/'
        'service_main.py --home __HOME__\n'
        'Restart=always\nRestartSec=10\n[Install]\nWantedBy=default.target\n')


README = """# ASG 终端服务交付包

安装:  ./asgctl install --soc-url http://<SOC地址> --credential-file <凭据文件>
状态:  ./asgctl status            检查: ./asgctl doctor
升级:  ./asgctl upgrade --package <新包目录>
卸载:  ./asgctl uninstall         （--remove-hooks 一并回滚 ASG 写入的 Hook）

数据目录: macOS ~/Library/Application Support/ASG, Linux ~/.local/share/asg。
故障处理先看 ./asgctl doctor 的输出与数据目录 logs/。凭据只保存在 config/ 下,
请勿提交或粘贴凭据内容。

## 本包对应的构建

版本与提交见包内 release.json（asg_version / git_commit / 每个文件 SHA-256）。
安装时会校验摘要；升级/回滚/健康检查行为以随包验收报告为准
（仓库 docs/stage1/TERMINAL_LIFECYCLE_ACCEPTANCE_20260921.md 与
docs/stage1/evidence/ 下按包摘要命名的报告）。

## 已支持范围（本 PoC 承诺）

- macOS 与 Linux 用户级服务；Windows 不在本轮范围。
- 自动发现机器上的 Agent 进程（含进程/包/配置/网络线索），区分 Agent、
  模型网关、宿主与其他类别。
- 四类已知 Agent（Codex/OpenCode/OpenClaw/Hermes）按 SOC 发布包确定性安装
  （无模型调用）；兼容新实例复用已验证配方，构建或安装文件变化时拒绝复用
  并转入调查。
- Hook 事件直报 SOC、断连本地缓冲补报（容量 2000、事件 ID 幂等）、
  严格模式决策超时阻断。

## 已知限制

- 目标应用原生的 Hook 信任审批（如桌面 Codex /hooks）只能由用户本人在目标
  应用内完成，本服务不代批；需要时会明确提示而不是静默重试。
- 未列入支持清单的陌生 Agent：能发现、调查并生成候选配方，但安装接通需要
  该配方的独立验证完成。
- 内容安全扫描（Guardian）与跨机器配方分发依赖外部平台，未随本包交付。
"""


def manifest_files(pkg):
    files = {}
    # Byte-code caches are runtime artifacts: remove any before hashing and
    # never list them, so a first interpreter run cannot invalidate digests.
    for stale in sorted(Path(pkg).rglob('__pycache__'), reverse=True):
        shutil.rmtree(stale, ignore_errors=True)
    for path in sorted(pkg.rglob('*')):
        if not path.is_file() and not path.is_symlink():
            continue
        name = str(path.relative_to(pkg))
        if name == 'release.json':
            continue
        if path.suffix == '.pyc' or '__pycache__' in path.parts:
            continue
        if path.is_symlink():
            files[name] = {'sha256': '', 'link': os.readlink(path)}
            continue
        entry = {'sha256': sha256_file(path)}
        if os.stat(path).st_mode & 0o111:
            entry['executable'] = True
        files[name] = entry
    return files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', required=True)
    parser.add_argument('--out', default=str(Path(tempdir_default()) / 'asg-build'))
    parser.add_argument('--wheels-dir', default=str(CACHE / 'wheels'))
    parser.add_argument('--sabotage', action='store_true',
                        help='mark the release as unable to start (rollback test only)')
    args = parser.parse_args()
    out = Path(args.out).expanduser()
    pkg = out / 'asg-terminal'
    if pkg.exists():
        raise SystemExit('输出目录已存在: %s（验收要求每次构建使用干净目录）' % pkg)
    out.mkdir(parents=True, exist_ok=True)
    pkg.mkdir(parents=True)

    commit = run(['git', 'rev-parse', 'HEAD']).stdout.strip()
    copy_app(pkg)
    (pkg / 'release').mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / 'release' / 'asgctl.py', pkg / 'release' / 'asgctl.py')
    # The supervisor is launched by the platform service at a fixed path:
    # current/app/service_main.py (matches register_service in asgctl.py).
    shutil.copy2(ROOT / 'release' / 'service_main.py', pkg / 'app' / 'service_main.py')
    # monitor_dashboard reads web/dashboard.html relative to its own parents[0];
    # inside app/ the layout is app/monitor_dashboard.py + app/web/ -> consistent.
    bundle_python(pkg, args.wheels_dir)
    if args.sabotage:
        marker = pkg / 'app' / 'bin' / 'sabotage.json'
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"reason": "intentional broken release for rollback test"}\n')
    write_launcher(pkg)
    write_services(pkg)
    (pkg / 'README.md').write_text(README)
    # The sanitizer writes a release audit and appends a README note. Run it
    # after both destinations exist, before manifest_files hashes the package.
    sanitize_portability(pkg)

    release = {'asg_version': args.version, 'git_commit': commit,
               'platform': platform.system(), 'architecture': platform.machine(),
               'config_version': 1, 'db_schema_version': 1,
               'built_at': __import__('time').strftime('%Y-%m-%dT%H:%M:%S%z'),
               'files': manifest_files(pkg)}
    (pkg / 'release.json').write_text(json.dumps(release, ensure_ascii=False, indent=1))

    # Deterministic self-verification using the bundled interpreter's asgctl.
    verify = subprocess.run([str(pkg / 'python/bin/python3'),
                             str(pkg / 'release/asgctl.py'), 'install', '--help'],
                            capture_output=True, text=True,
                            env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
    if verify.returncode != 0:
        raise SystemExit('交付包内 asgctl 无法运行: ' + verify.stderr[-400:])
    print(json.dumps({'package': str(pkg), 'version': args.version,
                      'git_commit': commit, 'files': len(release['files']),
                      'manifest_sha256': sha256_file(pkg / 'release.json'),
                      'sabotage': bool(args.sabotage)}, ensure_ascii=False))


def tempdir_default():
    import tempfile
    return tempfile.gettempdir()


if __name__ == '__main__':
    main()
