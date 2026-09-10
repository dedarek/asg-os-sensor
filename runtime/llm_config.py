from __future__ import annotations
import os
import threading
from pathlib import Path
from typing import Any
ROOT = Path(__file__).resolve().parents[1]
LLM_YAML = ROOT / 'llm.yaml'
DOTENV = ROOT / '.env'
_DOTENV_LOADED = False
_DOTENV_LOCK = threading.Lock()
def _load_dotenv() -> None:
    global _DOTENV_LOADED
    if _DOTENV_LOADED:
        return
    with _DOTENV_LOCK:
        if _DOTENV_LOADED:
            return
        try:
            from dotenv import load_dotenv
            load_dotenv(DOTENV, override=False)
        except Exception:
            pass
        if DOTENV.exists():
            try:
                for line in DOTENV.read_text(encoding='utf-8', errors='ignore').splitlines():
                    s = line.strip()
                    if not s or s.startswith('#') or '=' not in s:
                        continue
                    k, v = s.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip(chr(34)).strip(chr(39))
                    if k and k not in os.environ:
                        os.environ[k] = v
            except Exception:
                pass
        _DOTENV_LOADED = True
def _yaml() -> dict:
    try:
        import yaml
    except Exception:
        return {}
    try:
        if LLM_YAML.exists():
            return yaml.safe_load(LLM_YAML.read_text(encoding='utf-8')) or {}
    except Exception:
        pass
    return {}
def load_environment() -> None:
    """Load project .env once; safe to call concurrently."""
    _load_dotenv()
def _legacy_routes() -> dict:
    return {
        'commandcode': {
            'provider': 'openai',
            'model': 'deepseek/deepseek-v4-flash',
            'base_url': 'https://api.commandcode.ai/provider/v1',
            'key_env': 'COMMANDCODE_API_KEY',
            'chat_path': '/chat/completions',
            'responses_path': '/responses',
            'extra_headers': {},
            'timeout_s': 30,
        },
        'opencode-go': {
            'provider': 'openai',
            'model': 'deepseek-v4-flash',
            'base_url': 'https://opencode.ai/zen/go/v1',
            'key_env': 'OPENCODE_GO_API_KEY',
            'chat_path': '/chat/completions',
            'responses_path': '/responses',
            'extra_headers': {},
            'timeout_s': 30,
        },
    }
def analyst_route(route_name: str = '') -> dict:
    _load_dotenv()
    cfg = _yaml()
    routes = cfg.get('routes') or {}
    default = str(cfg.get('default_route') or '').strip().lower()
    legacy = _legacy_routes()
    for k, v in legacy.items():
        routes.setdefault(k, v)
    name = (route_name or os.environ.get('ASG_ANALYST_ROUTE', '') or default or 'custom-openai').strip().lower()
    if name not in routes:
        if default and default in routes:
            name = default
        elif routes:
            name = sorted(routes)[0]
        else:
            name = 'custom-openai'
    r = dict(routes.get(name, {}))
    model_override = os.environ.get('ASG_ANALYST_MODEL', '').strip()
    if model_override:
        r['model'] = model_override
    base = str(r.get('base_url', '')).rstrip('/')
    r['base_url'] = base
    r.setdefault('provider', 'openai')
    r.setdefault('model', 'qwen38-27b')
    r.setdefault('key_env', 'ASG_ANALYST_API_KEY')
    r.setdefault('chat_path', '/chat/completions')
    r.setdefault('responses_path', '/responses')
    r.setdefault('extra_headers', {})
    r.setdefault('timeout_s', 30)
    r['route'] = name
    return r
def analyst_key(route: dict) -> str:
    _load_dotenv()
    key = os.environ.get(str(route.get('key_env', '')), '') or ''
    return key.strip()
def mask_key(key: str) -> str:
    if not key:
        return 'missing'
    return 'present(len=hidden)'
def goose_env(route: dict, key: str, pid: int = 0) -> dict:
    base = str(route.get('base_url', '')).rstrip('/')
    env = {}
    env['GOOSE_PROVIDER'] = str(route.get('provider', 'openai'))
    env['GOOSE_MODEL'] = str(route.get('model', ''))
    env['GOOSE_MODE'] = 'auto'
    env['OPENAI_BASE_URL'] = base
    env['OPENAI_API_KEY'] = key
    if os.environ.get('ASG_INSECURE_SSL', '').strip() == '1':
        # Goose uses a Rust HTTP client and does not honor PYTHONHTTPSVERIFY.
        # Keep its traffic local, then let the loopback proxy handle broken
        # upstream certificates without weakening TLS for unrelated traffic.
        from runtime.llm_proxy import ensure_proxy
        env['OPENAI_BASE_URL'] = ensure_proxy(route, key)
        env['ASG_INSECURE_SSL'] = '1'
        env['PYTHONHTTPSVERIFY'] = '0'
    headers = route.get('extra_headers') or {}
    if isinstance(headers, dict) and headers:
        flat = ','.join([str(k) + '=' + str(v) for k, v in headers.items()])
        if flat:
            env['OPENAI_CUSTOM_HEADERS'] = flat
    if route.get('route') == 'opencode-go' and pid:
        extra = 'x-opencode-session=asg-live-' + str(pid) + ',x-opencode-client=asg-live-analyst'
        prev = env.get('OPENAI_CUSTOM_HEADERS', '')
        env['OPENAI_CUSTOM_HEADERS'] = (prev + ',' + extra) if prev else extra
    return env
