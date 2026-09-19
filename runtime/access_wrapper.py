"""Alternative access for targets with no native Hook: a governed model proxy
and a launch wrapper.

When a target exposes no extension point, it can still be observed and
controlled by pointing its model provider at a loopback proxy that captures
request/response bodies and applies an allow/deny decision before forwarding.
The proxy writes events in the same shape the observation contract already
reads, so the data model does not fork.

This is a mechanism plus local verification; it does not by itself prove
onboarding of any specific product, which still needs an isolated instance.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from runtime import event_vocabulary

MAX_FIELD_BYTES = 1048576
PROVIDER_ENV_KEYS = ("OPENAI_BASE_URL", "OPENAI_API_BASE", "OPENAI_API_BASE_URL",
                     "ANTHROPIC_BASE_URL", "AZURE_OPENAI_ENDPOINT")


def wrap_environment(env: dict, *, wrapper_base: str, keys=PROVIDER_ENV_KEYS) -> dict:
    """Point a target's model provider at the wrapper without touching other env.

    Returns the rewritten environment and the keys that changed; if the target
    had no known provider key, the documented default is added.
    """
    result = dict(env)
    changed = []
    for key in keys:
        if key in result:
            result[key] = wrapper_base
            changed.append(key)
    if not changed:
        result["OPENAI_BASE_URL"] = wrapper_base
        changed.append("OPENAI_BASE_URL(default)")
    return {"env": result, "changed": changed}


def _payload(value, max_bytes=MAX_FIELD_BYTES) -> dict:
    try:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(value)
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return {"present": True, "bytes": len(raw), "truncated": False, "value": text}
    head = raw[:max_bytes].decode("utf-8", "ignore")
    return {"present": True, "bytes": len(raw), "truncated": True,
            "retained_bytes": len(head.encode("utf-8")),
            "truncation": {"reason": "max_field_bytes", "limit_bytes": max_bytes, "full_bytes": len(raw)},
            "value": head + "\n...[truncated]"}


class GovernedProxy:
    """Loopback proxy: capture, decide, then forward (or block) the model call."""

    def __init__(self, upstream: str, *, log_path, instance: dict, decide=None,
                 host="127.0.0.1", port=0):
        self.upstream = upstream.rstrip("/")
        self.log_path = Path(log_path)
        self.instance = instance
        self.decide = decide or (lambda request: {"decision": "allow", "reason": "default"})
        self._server = ThreadingHTTPServer((host, port), _make_handler(self))
        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self._thread = None
        self.forwarded = []
        self.blocked = []

    @property
    def base_url(self) -> str:
        return "http://%s:%d/v1" % (self.host, self.port)

    def start(self):
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._server.server_close()

    def record(self, event: str, request_id: str, detail: dict):
        line = {"event": event, "pid": self.instance.get("pid"),
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
                "seq": None, "tool": None, "call_id": request_id,
                "detail": {"instance_id": self.instance.get("instance_id"),
                           "request_id": request_id, **detail}}
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")


def _make_handler(proxy: GovernedProxy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, code, data, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            import uuid
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length)
            request_id = uuid.uuid4().hex
            try:
                payload = json.loads(body) if body else {}
            except ValueError:
                payload = {"_raw": body[:512].decode("utf-8", "ignore")}
            verdict = proxy.decide(payload) or {}
            decision = "allow" if str(verdict.get("decision")).lower() == "allow" else "deny"
            proxy.record("control.applied", request_id, {
                "tool": "model", "decision": decision, "reason": verdict.get("reason"),
                "outcome": "allowed" if decision == "allow" else "blocked",
                "request": _payload(payload)})
            if decision != "allow":
                proxy.blocked.append(request_id)
                self._send(403, json.dumps({"error": {"message": "blocked by access wrapper",
                                                      "request_id": request_id}}).encode())
                return
            upstream_url = proxy.upstream + self.path
            request = urllib.request.Request(upstream_url, data=body, method="POST",
                headers={"Content-Type": self.headers.get("Content-Type", "application/json")})
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    status, data = response.status, response.read()
            except urllib.error.HTTPError as exc:
                status, data = exc.code, exc.read()
            except Exception as exc:  # noqa: BLE001
                status, data = 502, json.dumps({"error": type(exc).__name__}).encode()
            proxy.forwarded.append(request_id)
            proxy.record("model.request", request_id, {"content": _payload(payload),
                                                       "url": self.path, "method": "POST"})
            try:
                response_payload = json.loads(data)
            except (TypeError, ValueError):
                response_payload = {"_raw": data[:512].decode("utf-8", "ignore")}
            proxy.record("model.response", request_id, {"content": _payload(response_payload),
                                                        "status": status})
            self._send(status, data)

    return Handler


def canonical_event(name: str):
    return event_vocabulary.canonical(name) or name


__all__ = ["GovernedProxy", "MAX_FIELD_BYTES", "PROVIDER_ENV_KEYS", "canonical_event",
           "_payload", "wrap_environment"]
