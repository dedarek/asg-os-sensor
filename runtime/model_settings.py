"""Dashboard model settings: activate only after a real isolated Goose reply."""
from __future__ import annotations
import json
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from runtime.learned_install import _atomic

LOCK = threading.RLock()
JOB = {'status': 'idle'}

def path():
    return Path(os.environ.get('ASG_RUN_DIR', 'artifacts/stage1/dashboard')) / 'model_settings.json'

def load():
    try:
        return json.loads(path().read_text())
    except FileNotFoundError:
        return None

def public():
    from runtime.llm_config import analyst_route, analyst_key
    route = analyst_route()
    with LOCK:
        return {'config': {'base_url': route.get('base_url'), 'model': route.get('model'),
                'provider': route.get('provider'), 'has_key': bool(analyst_key(route))}, 'test': dict(JOB) if JOB.get('status') != 'idle' else (load() or {}).get('verification', dict(JOB))}

def prepare(data):
    from runtime.llm_config import analyst_route, analyst_key
    if not isinstance(data, dict):
        raise ValueError('配置必须是对象')
    base = str(data.get('base_url') or '').strip().rstrip('/')
    url = urlsplit(base)
    if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('请输入完整的 HTTP/HTTPS API 地址，不要在地址中包含密钥')
    route_path = url.path
    for suffix in ('/chat/completions', '/responses'):
        if route_path.endswith(suffix):
            route_path = route_path[:-len(suffix)]
    base = urlunsplit((url.scheme, url.netloc, route_path, '', '')).rstrip('/')
    model = str(data.get('model') or '').strip()
    if not model or len(model) > 200:
        raise ValueError('请输入模型名称')
    current = analyst_route()
    same_endpoint = base == str(current.get('base_url', '')).rstrip('/')
    key = str(data.get('api_key') or '').strip()
    if not key and same_endpoint:
        key = analyst_key(current)
    if not key:
        raise ValueError('请输入 API Key；更换地址时不能沿用旧地址的密钥')
    route = dict(current) if same_endpoint else {}
    route.update(provider='openai', model=model, base_url=base, route='dashboard', key_env='ASG_DASHBOARD_KEY')
    return {'route': route, 'key': key}

def assistant_text(stdout):
    messages = []
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        msg = item.get('message', item)
        if not isinstance(msg, dict) or msg.get('role') != 'assistant':
            continue
        for block in msg.get('content', msg.get('blocks', [])):
            if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
                messages.append(block['text'])
    return ''.join(messages)

def start(data):
    candidate = prepare(data)
    with LOCK:
        if JOB.get('status') == 'testing':
            raise ValueError('已有验证正在进行，请等待结果')
        JOB.clear(); JOB.update(status='testing', message='正在启动 Goose，等待真实模型回复…')
    threading.Thread(target=_test, args=(candidate,), daemon=True).start()
    return {'status': 'testing'}

def _test(candidate):
    from runtime.llm_config import goose_env
    nonce = 'ASG-' + secrets.token_hex(8)
    try:
        goose = os.environ.get('ASG_GOOSE_BIN') or shutil.which('goose')
        if not goose:
            raise ValueError('未找到 Goose CLI')
        env = os.environ.copy()
        env.update(goose_env(candidate['route'], candidate['key']))
        result = subprocess.run([goose, 'run', '--no-profile', '--no-session',
            '--provider', 'openai', '--model', candidate['route']['model'],
            '--max-turns', '1', '--output-format', 'stream-json', '--text',
            'This is a connectivity test. Do not use tools. Reply with exactly: ' + nonce],
            env=env, capture_output=True, text=True, timeout=180)
        reply = assistant_text(result.stdout)
        if result.returncode != 0 or nonce not in reply:
            raise ValueError('Goose 未返回预期验证消息，原配置未改变。请检查地址、模型和密钥。')
        with LOCK:
            path().parent.mkdir(parents=True, exist_ok=True)
            verification = dict(status='succeeded', message='Goose 已回复，配置已保存并生效', reply=reply[:500], verified_at=time.time())
            candidate['verification'] = verification
            _atomic(path(), json.dumps(candidate, ensure_ascii=False).encode())
            JOB.clear(); JOB.update(verification)
    except subprocess.TimeoutExpired:
        with LOCK:
            JOB.clear(); JOB.update(status='failed', message='Goose 验证超过 180 秒，原配置未改变')
    except Exception as exc:
        with LOCK:
            JOB.clear(); JOB.update(status='failed', message=str(exc) if isinstance(exc, ValueError) else '验证失败，原配置未改变')
