"""Read-only native admission checks, grouped into one action per instance/workspace."""
import json
import subprocess
import threading
import time
from pathlib import Path
import shutil
import psutil

_cache = {}
_pending = set()
_lock = threading.Lock()


def summarize(report, workspace):
    items = [i for i in report.get('items', []) if 'asg-run.sh' in str(i.get('displayCommand', ''))]
    if not items:
        return {'status': 'unknown'}
    blocked = [i for i in items if i.get('trustState') != 'trusted_persistent']
    return {'status': 'approval_required' if blocked else 'approved',
            'title': '需要你批准 Hook 接入' if blocked else '原生授权已完成，等待真实事件',
            'workspace': workspace, 'event_count': len(items),
            'reason_code': report.get('reasonCode'), 'bundle_digest': report.get('bundleDigest'),
            'instructions': '在目标应用的工作区 Hook 审核界面，审阅来自 .zcode/config.json、命令包含 asg-run.sh 的条目。确认后新开会话；ASG 会自动检查授权和真实事件。',
            'detail': '这是一份接入配置，包含 %d 个事件回调，不是 %d 次重复安装。等待期间不重复安装，也不自动批准。' % (len(items), len(items))}


def current(agent):
    """No blocking subprocess on the HTTP thread. Probe only declared workspace hooks."""
    adapter = agent.get('adapter') or {}
    plan = (adapter.get('onboarding') or {}).get('plan') or {}
    workspace = plan.get('workspace')
    if not workspace or not (Path(workspace) / '.zcode/config.json').is_file():
        return None
    key = (agent.get('instance_id'), workspace)
    with _lock:
        cached = _cache.get(key)
        if key not in _pending and (not cached or time.monotonic() - cached[0] > 15):
            _pending.add(key)
            threading.Thread(target=_probe, args=(key, agent['pid']), daemon=True).start()
    return cached[1] if cached else {'status': 'checking', 'title': '正在检查原生授权'}


def _probe(key, pid):
    result = {'status': 'unknown', 'title': '原生授权检查未完成'}
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - float(key[0].split(':', 1)[1])) > .001:
            raise ValueError('instance_changed')
        exe = Path(proc.exe())
        bundle = next(p for p in exe.parents if p.suffix == '.app')
        cli = bundle / 'Contents/Resources/glm/zcode.cjs'
        if not cli.is_file():
            raise ValueError('native_trust_cli_unavailable')
        node = shutil.which('node')
        if not node:
            raise ValueError('node_unavailable')
        response = subprocess.run([node, str(cli), 'hooks', 'trust', 'status', '--workspace', key[1], '--json'],
                                  capture_output=True, text=True, timeout=12)
        if response.returncode:
            raise ValueError('native_trust_probe_failed')
        result = summarize(json.loads(response.stdout), key[1])
    except (OSError, ValueError, StopIteration, subprocess.TimeoutExpired, psutil.Error) as exc:
        result['reason'] = type(exc).__name__
    finally:
        with _lock:
            _cache[key] = (time.monotonic(), result)
            _pending.discard(key)


def attach(snapshot):
    for agent in snapshot.get('agents', []):
        adapter = agent.get('adapter') or {}
        action = current(agent)
        if not action:
            stages = (adapter.get('capability') or {}).get('stages', [])
            if any(s.get('id') == 'trusted' and s.get('state') == 'blocked' for s in stages):
                action = {'status': 'approval_required', 'title': '需要你批准 Hook 接入',
                          'instructions': '请在目标应用的原生 Hook 审核界面审阅 ASG 接入配置；批准后新开会话。',
                          'detail': '等待批准期间不重复安装，不自动代替你批准。'}
        if action:
            adapter['user_action'] = action
