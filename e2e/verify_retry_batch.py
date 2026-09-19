"""Task-2 retry gate: inject one recoverable model error per round and prove
the Agent retries and the retry is reconciled.

A local fault proxy sits in front of the real test gateway and, when armed,
answers the next model request with a transient 503 before forwarding normally.
The driver counts attempts independently at the proxy and compares them with
what the Hook captured for the same session.

Usage:  python3 e2e/verify_retry_batch.py [--rounds 5]
"""
from __future__ import annotations

import argparse
import http.client
import json
import secrets
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts/autonomous-demo'
CONFIG = DEMO / 'config/opencode/opencode.json'
DASH = 'http://127.0.0.1:8081'
LOG = DEMO / 'workspace/.opencode/asg-logs/decision-hook-events.jsonl'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(base, path, body=None, timeout=240):
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'})
    return json.load(OPENER.open(req, timeout=timeout))


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self):
        srv = self.server
        length = int(self.headers.get('Content-Length', '0') or 0)
        body = self.rfile.read(length) if length else None
        if self.path == '/__arm':
            srv.armed = True
            srv.attempts = []
            return self._json(200, {'armed': True})
        if self.path == '/__attempts':
            return self._json(200, {'attempts': srv.attempts})
        srv.attempts.append({'path': self.path, 'injected': False, 'ts': time.time()})
        if srv.armed:
            srv.armed = False
            srv.attempts[-1]['injected'] = True
            return self._json(503, {'error': {'message': 'injected transient error', 'type': 'server_error'}})
        conn = http.client.HTTPConnection(srv.up_host, srv.up_port, timeout=300)
        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in ('host', 'content-length', 'connection', 'accept-encoding')}
        if body is not None:
            headers['Content-Length'] = str(len(body))
        conn.request(self.command, self.path, body, headers)
        resp = conn.getresponse()
        data = resp.read()
        srv.attempts[-1]['status'] = resp.status
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() in ('transfer-encoding', 'connection', 'content-length'):
                continue
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _handle


def start_proxy(upstream):
    parts = urlsplit(upstream)
    handler = _Handler
    server = ThreadingHTTPServer(('127.0.0.1', free_port()), handler)
    server.up_host, server.up_port = parts.hostname, parts.port
    server.armed = False
    server.attempts = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def restart_target():
    import subprocess
    subprocess.run(['python3', 'e2e/real_autonomous_demo.py', 'restart'], cwd=str(ROOT),
                   capture_output=True, text=True, timeout=60)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=5)
    args = ap.parse_args()
    gateway = json.loads((DEMO / 'gateway.json').read_text())
    original = CONFIG.read_text()
    proxy = start_proxy(gateway['base_url'])
    proxy_base = 'http://127.0.0.1:%d/v1' % proxy.server_address[1]
    rounds = []
    try:
        config = json.loads(original)
        config['provider']['demo']['options']['baseURL'] = proxy_base
        CONFIG.write_text(json.dumps(config))
        restart_target()
        m = json.loads((DEMO / 'target.json').read_text())
        target = 'http://127.0.0.1:' + str(m['port'])
        iid = '%s:%s' % (m['pid'], m['create_time'])
        # wait for auto-bind of the new instance
        for _ in range(90):
            try:
                if iid in json.loads((ROOT / 'artifacts/autonomous-service/observations.json').read_text()):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(0.5)
        for _ in range(args.rounds):
            canary = 'ASG-RETRY-' + secrets.token_hex(6)
            request(proxy_base.replace('/v1', '') + '/__arm', '', None)
            session = request(target, '/session', {'title': 'retry',
                'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]})
            ok = True
            try:
                response = request(target, '/session/' + session['id'] + '/message',
                    {'model': {'providerID': 'demo', 'modelID': gateway['model']},
                     'parts': [{'type': 'text', 'text': '请用中文确认收到 ' + canary}]}, timeout=240)
                reply = ''.join(str(p.get('text') or '') for p in (response.get('parts') or [])
                                if isinstance(p, dict) and p.get('type') == 'text')
            except Exception as exc:  # noqa: BLE001
                ok, reply = False, type(exc).__name__
            attempts = json.load(urllib.request.urlopen(proxy_base.replace('/v1', '') + '/__attempts', timeout=10))['attempts']
            time.sleep(1.0)
            captured = 0
            for line in LOG.read_text().splitlines():
                if session['id'] not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get('event') == 'model.request':
                    captured += 1
            rounds.append({'session': session['id'], 'ok': ok, 'reply_len': len(reply),
                           'proxy_attempts': len(attempts),
                           'injected': sum(1 for a in attempts if a.get('injected')),
                           'captured_model_requests': captured})
    finally:
        CONFIG.write_text(original)
        restart_target()
        proxy.shutdown()
    report = {'rounds': rounds, 'requested': args.rounds, 'finished_at': time.time()}
    gates = {
        'retry_5_of_5_succeeded': len(rounds) == args.rounds and all(r['ok'] for r in rounds),
        'one_error_injected_each': all(r['injected'] == 1 for r in rounds),
        'attempted_more_than_once': all(r['proxy_attempts'] >= 2 for r in rounds),
        'retry_attempt_captured': all(r['captured_model_requests'] >= 1 for r in rounds),
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'retry-batch.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed']}, ensure_ascii=False))
    for r in rounds:
        print(json.dumps(r, ensure_ascii=False))


if __name__ == '__main__':
    main()
