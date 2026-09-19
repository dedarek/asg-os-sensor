"""Alternative access for a target with no native Hook: a governed model proxy.

Local end-to-end verification against a stub upstream (a local stand-in for the
provider, not a product test): allow forwards and captures, deny blocks before
the provider is ever called, request/response correlate by id, oversized bodies
carry truncation metadata, and the launch wrapper rewrites only provider env.

Usage: python3 e2e/verify_access_wrapper.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runtime import access_wrapper, event_vocabulary  # noqa: E402


class StubUpstream:
    """Local stand-in provider; counts calls so a blocked request is provable."""

    def __init__(self):
        self.calls = 0

    def __call__(self, method, url, body, headers=None):
        self.calls += 1
        return 200, json.dumps({"id": "stub-%d" % self.calls, "object": "chat.completion",
                                "choices": [{"index": 0, "message": {"role": "assistant",
                                                                     "content": "ok"}}]}).encode()


def start_stub():
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            self.rfile.read(length)
            self.server.upstream.calls += 1
            body = json.dumps({"id": "stub-%d" % self.server.upstream.calls,
                               "object": "chat.completion",
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = StubUpstream()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.upstream = upstream
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, upstream


def post(url, payload):
    request = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main():
    upstream_server, upstream = start_stub()
    report = {'started_at': time.time()}
    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp).resolve() / 'wrapper-events.jsonl'
        decide = lambda payload: (  # noqa: E731
            {"decision": "deny", "reason": "marker_present"}
            if "DENY-ME" in json.dumps(payload)
            else {"decision": "allow", "reason": "default_allow"})
        proxy = access_wrapper.GovernedProxy(
            "http://127.0.0.1:%d" % upstream_server.server_address[1],
            log_path=log_path, instance={'pid': 999999, 'instance_id': 'stub:1'},
            decide=decide).start()
        try:
            allow_status, _ = post(proxy.base_url + '/chat/completions',
                                   {'model': 'x', 'messages': [{'role': 'user', 'content': 'hello'}]})
            calls_after_allow = upstream.calls
            deny_status, deny_body = post(proxy.base_url + '/chat/completions',
                                          {'model': 'x', 'messages': [{'role': 'user', 'content': 'DENY-ME'}]})
            calls_after_deny = upstream.calls
            big = 'A' * (1_200_000)
            post(proxy.base_url + '/chat/completions',
                 {'model': 'x', 'messages': [{'role': 'user', 'content': big}]})
            forwarded_calls = upstream.calls
        finally:
            proxy.stop()
            upstream_server.shutdown()
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]

    by_event = {}
    for event in events:
        by_event.setdefault(event['event'], []).append(event)
    request_ids = {e['detail']['request_id'] for e in by_event.get('model.request', [])}
    response_ids = {e['detail']['request_id'] for e in by_event.get('model.response', [])}
    blocked = [e for e in by_event.get('control.applied', []) if e['detail']['decision'] == 'deny']
    denied_ids = {e['detail']['request_id'] for e in blocked}
    oversized = [e for e in by_event.get('model.request', [])
                 if (e['detail'].get('content') or {}).get('truncated') is True]

    wrapped = access_wrapper.wrap_environment(
        {'OPENAI_BASE_URL': 'https://api.example/v1', 'PATH': '/usr/bin', 'HOME': '/Users/x'},
        wrapper_base=proxy.base_url)
    env, changed = wrapped['env'], wrapped['changed']

    report.update({
        'allow_status': allow_status, 'deny_status': deny_status,
        'upstream_calls_after_allow': calls_after_allow,
        'upstream_calls_after_deny': calls_after_deny,
        'events': [e['event'] for e in events],
        'canonical_request': event_vocabulary.canonical('model.request'),
        'oversized_request_truncated': len(oversized),
        'blocked_decisions': len(blocked),
        'env_wrapper_base': env.get('OPENAI_BASE_URL'), 'env_path_kept': env.get('PATH'),
        'env_changed': changed,
    })
    gates = {
        'allow_forwards_and_captures': allow_status == 200 and calls_after_allow == 1
        and len(by_event.get('model.request', [])) >= 1 and len(by_event.get('model.response', [])) >= 1,
        'deny_blocks_before_provider': deny_status == 403 and calls_after_deny == calls_after_allow
        and len(blocked) >= 1,
        'denied_call_not_forwarded_or_captured': not (denied_ids & request_ids)
        and len(request_ids) == forwarded_calls,
        'request_response_correlate': bool(request_ids) and request_ids == response_ids,
        'oversized_body_truncation_marked': len(oversized) >= 1,
        'events_use_contract_names': all(access_wrapper.canonical_event(name) == name
                                         for name in ('model.request', 'model.response', 'control.applied'))
        and report['canonical_request'] == 'model.request',
        'launch_wrapper_rewrites_only_provider_env': report['env_changed'] == ['OPENAI_BASE_URL']
        and report['env_path_kept'] == '/usr/bin',
    }
    report['gates'] = gates
    report['passed'] = all(gates.values())
    report['external_condition'] = '对具体无原生 Hook 目标的端到端接入仍需隔离实例；Goose 调查需完整完成'
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    (out / 'access-wrapper.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'gates': gates, 'passed': report['passed']}, ensure_ascii=False))
    print({k: report[k] for k in ('allow_status', 'deny_status', 'upstream_calls_after_allow',
                                  'upstream_calls_after_deny', 'blocked_decisions',
                                  'oversized_request_truncated', 'env_changed')})


if __name__ == '__main__':
    main()
