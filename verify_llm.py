import json
import os
import ssl
import sys
import urllib.request
from pathlib import Path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from runtime.llm_config import analyst_key
from runtime.llm_config import analyst_route
from runtime.llm_config import goose_env
from runtime.llm_config import mask_key
from runtime.llm_config import tls_exception_enabled
def _ctx() -> ssl.SSLContext:
    route = analyst_route()
    tls = route.get('tls') if isinstance(route.get('tls'), dict) else {}
    ca_bundle = str(tls.get('ca_bundle', '') or '').strip()
    return ssl.create_default_context(cafile=ca_bundle or None)
def post(base: str, path: str, payload: dict, key: str, timeout: int):
    url = base.rstrip('/') + path
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key}, method='POST')
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as resp:
        return resp.status, resp.read()[:4000].decode('utf-8', errors='ignore')
def extract_text(body: str) -> str:
    try:
        obj = json.loads(body)
    except Exception:
        return body[:500]
    try:
        choices = obj.get('choices') or []
        if choices:
            msg = choices[0].get('message') or {}
            if msg.get('content'):
                return str(msg.get('content'))[:500]
    except Exception:
        pass
    try:
        for item in obj.get('output') or []:
            if item.get('type') == 'message':
                for block in item.get('content') or []:
                    if block.get('type') == 'output_text' and block.get('text'):
                        return str(block.get('text'))[:500]
    except Exception:
        pass
    return body[:500]
def main() -> int:
    fails = []
    def check(name: str, cond: bool, hint: str = '') -> None:
        print(('PASS' if cond else 'FAIL') + ' ' + name + (' ' + hint if hint else ''), flush=True)
        if not cond:
            fails.append(name)
    route = analyst_route()
    key = analyst_key(route)
    print('route=' + str(route.get('route')) + ' model=' + str(route.get('model')) + ' base=' + str(route.get('base_url')) + ' key=' + mask_key(key) + ' (' + str(route.get('key_env')) + ')', flush=True)
    print('tls_transport=' + ('loopback-proxy (route-scoped)' if tls_exception_enabled(route) else 'direct-verified'), flush=True)
    check('llm.yaml exists', (ROOT / 'llm.yaml').exists())
    check('route has base_url', bool(route.get('base_url')))
    check('route has model', bool(route.get('model')))
    check('key present', bool(key), '(' + str(route.get('key_env')) + ')')
    if not key:
        print('hint: cp .env.example .env, fill ' + str(route.get('key_env')) + ', or export it', flush=True)
        print('verify failed: ' + str(fails), flush=True)
        return 1
    timeout = int(route.get('timeout_s') or 30)
    model = str(route.get('model'))
    effective_base = goose_env(route, key).get('OPENAI_BASE_URL', str(route.get('base_url')))
    ok_chat = False
    try:
        status, body = post(str(effective_base), str(route.get('chat_path') or '/chat/completions'), {'model': model, 'messages': [{'role': 'user', 'content': 'say ok'}], 'max_tokens': 32}, key, timeout)
        print('chat status=' + str(status) + ' text=' + extract_text(body).replace(chr(10), ' ')[:200], flush=True)
        ok_chat = status == 200
    except Exception as e:
        print('chat error: ' + type(e).__name__ + ': ' + str(e)[:300], flush=True)
    check('chat/completions 200', ok_chat)
    ok_resp = False
    try:
        status, body = post(str(effective_base), str(route.get('responses_path') or '/responses'), {'model': model, 'input': [{'role': 'user', 'content': 'say ok'}]}, key, timeout)
        print('responses status=' + str(status) + ' text=' + extract_text(body).replace(chr(10), ' ')[:200], flush=True)
        ok_resp = status == 200
    except Exception as e:
        print('responses error: ' + type(e).__name__ + ': ' + str(e)[:300], flush=True)
    check('responses 200', ok_resp)
    if tls_exception_enabled(route):
        print('goose proxy route=' + str(route.get('route')) + ' transport=loopback-proxy', flush=True)
        check('goose TLS proxy 200', ok_chat and ok_resp)
    if fails:
        print('verify failed: ' + str(fails), flush=True)
        return 1
    print('llm verify all passed', flush=True)
    return 0
if __name__ == '__main__':
    sys.exit(main())
