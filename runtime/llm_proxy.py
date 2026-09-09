"""Loopback-only reverse proxy for OpenAI-compatible endpoints with broken TLS.

This is an emergency compatibility path. It is started only when
ASG_INSECURE_SSL=1, binds to loopback, and never logs request bodies or keys.
"""
from __future__ import annotations

import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests


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
        target = str(_STATE["base_url"]).rstrip("/") + self.path
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
                verify=False,
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
    with _LOCK:
        _STATE.update(
            base_url=str(route.get("base_url", "")).rstrip("/"),
            key=key,
            extra_headers=dict(route.get("extra_headers") or {}),
            timeout_s=float(route.get("timeout_s") or 30),
        )
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
