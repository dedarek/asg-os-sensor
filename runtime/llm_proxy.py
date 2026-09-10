"""Loopback-only reverse proxy for OpenAI-compatible endpoints with broken TLS.

This is a route-scoped compatibility path. It binds to loopback, forwards
only the origin selected by the active route, never follows redirects, and
never logs request bodies or keys.
"""
from __future__ import annotations

import os
import json
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests

from urllib.parse import urlsplit


_LOCK = threading.Lock()
_SERVER: ThreadingHTTPServer | None = None
_STATE: dict[str, Any] = {}


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        if self.path == "/healthz":
            body = b'{"status":"ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length)
        window = _STATE.get('tool_context_window')
        if window and self.path.endswith('/chat/completions'):
            from runtime.tool_context import compact
            payload, stats = compact(json.loads(body), window)
            if stats['applied']:
                before = len(body)
                body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                audit = os.environ.get('ASG_TOOL_CONTEXT_AUDIT')
                if audit:
                    with open(audit, 'a', encoding='utf-8') as log:
                        log.write(json.dumps({'ts': time.time(), **stats, 'before_bytes': before,
                                              'after_bytes': len(body)}) + '\n')
        base = str(_STATE["base_url"]).rstrip("/")
        # Goose prefixes /v1; configured vendor base already contains /v1.
        suffix = self.path[3:] if base.endswith('/v1') and self.path.startswith('/v1/') else self.path
        if suffix not in ('/chat/completions', '/responses'):
            self.send_error(404); return
        target = base + suffix
        parsed = urlsplit(target)
        if f"{parsed.scheme}://{parsed.netloc}" != _STATE.get("origin"):
            self.send_error(502); return
        headers = {
            "Authorization": "Bearer " + str(_STATE["key"]),
            "Content-Type": self.headers.get("Content-Type", "application/json"),
            "Accept": self.headers.get("Accept", "*/*"),
        }
        headers.update(_STATE.get("extra_headers") or {})
        try:
            upstream = requests.post(
                target,
                data=body,
                headers=headers,
                stream=True,
                timeout=float(_STATE.get("timeout_s", 30)),
                verify=_STATE.get('verify_tls', True),
                allow_redirects=False,
            )
            self.send_response(upstream.status_code)
            for name in ("Content-Type", "Cache-Control", "X-Request-Id"):
                value = upstream.headers.get(name)
                if value:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in upstream.iter_content(chunk_size=8192):
                if chunk:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            self.close_connection = True
        except Exception as exc:
            body = (type(exc).__name__ + ": " + str(exc)).encode("utf-8", errors="replace")[:1000]
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True

    def log_message(self, format: str, *args: Any) -> None:
        return


def ensure_proxy(route: dict[str, Any], key: str) -> str:
    """Start/update the process-local proxy and return its OpenAI base URL."""
    global _SERVER
    base = str(route.get('base_url', '')).rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme.lower() != 'https' or not parsed.netloc or parsed.query or parsed.fragment:
        raise ValueError('TLS loopback proxy requires an https base_url without query or fragment')
    with _LOCK:
        origin = f'{parsed.scheme}://{parsed.netloc}'
        if _SERVER is not None and _STATE.get('origin') not in (None, origin):
            raise ValueError('TLS loopback proxy is already bound to another route origin')
        _STATE.update(
            base_url=base,
            origin=origin,
            key=key,
            extra_headers=dict(route.get("extra_headers") or {}),
            timeout_s=float(route.get("timeout_s") or 30),
            tool_context_window=route.get('tool_context_window'),
        )
        from runtime.llm_config import tls_exception_enabled
        _STATE['verify_tls'] = not tls_exception_enabled(route)
        if _SERVER is None:
            host = os.environ.get("ASG_LLM_PROXY_HOST", "127.0.0.1").strip() or "127.0.0.1"
            try:
                port = int(os.environ.get("ASG_LLM_PROXY_PORT", "0") or "0")
            except ValueError:
                port = 0
            _SERVER = ThreadingHTTPServer((host, port), _ProxyHandler)
            thread = threading.Thread(target=_SERVER.serve_forever, name="asg-llm-tls-proxy", daemon=True)
            thread.start()
            actual_host, actual_port = _SERVER.server_address[:2]
            print(f"[LLM Proxy] TLS compatibility proxy: http://{actual_host}:{actual_port}", flush=True)
        actual_host, actual_port = _SERVER.server_address[:2]
        return f"http://{actual_host}:{actual_port}"
