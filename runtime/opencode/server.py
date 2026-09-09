# -*- coding: utf-8 -*-
"""隔离观测 HTTP 服务：把已验证的 event/health 接到 HTTP 接口（不驻留库对象）。

绑定 127.0.0.1 随机端口，仅本机可访问。端点：
  GET /health  当前健康（含撤销/时效/绑定过滤后的判定）
  GET /events  已验证事件列表（按 runid 读取 run 目录）
  GET /        状态总览
默认拒绝所有其它路径。运行：python3 -B runtime/opencode/server.py --workspace <ws>
"""
import argparse
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from runtime.opencode.event_api import EventVerifier


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server: "ObserveServer" = self.server
        if self.path == "/":
            return self._json(200, {"service": "asg-observe", "status": "ok"})
        if self.path == "/health":
            verifier = server.verifier
            rows = verifier.read_raw(server.events_file)
            h = verifier.current_health(rows)
            payload = {**h, "loaded_observed": verifier.loaded_observed(rows),
                       "wired": True, "events_count": len(rows)}
            return self._json(200 if h["healthy"] else 503, payload)
        if self.path == "/events":
            verifier = server.verifier
            rows = verifier.read_raw(server.events_file)
            valid, invalid = verifier.bound_events(rows)
            return self._json(200, {"valid": len(valid), "invalid": len(invalid),
                                    "events": valid[-200:]})
        self._json(404, {"error": "not found"})


class ObserveServer:
    def __init__(self, events_file: Path, expected_nonce: str, expected_pid: int,
                 expected_ct: float, ttl_s: float = 60.0, active: bool = True):
        self.events_file = events_file
        self.verifier = EventVerifier(expected_nonce, expected_pid, expected_ct, ttl_s=ttl_s, active=active)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # 让 handler 能访问 server 状态
        self.httpd.verifier = self.verifier  # type: ignore[attr-defined]
        self.httpd.events_file = self.events_file  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]

    def start(self):
        import threading
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def health(self):
        rows = self.verifier.read_raw(self.events_file)
        return self.verifier.current_health(rows)

    def api_status(self):
        return {"type": "http", "wired": True, "port": self.port,
                "endpoints": {"health": "/health", "events": "/events"}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--runid", default=None)
    ap.add_argument("--nonce", default="")
    ap.add_argument("--pid", type=int, default=os.getpid())
    ap.add_argument("--create-time", type=float, default=None)
    ap.add_argument("--ttl", type=float, default=60.0)
    a = ap.parse_args()
    ws = Path(a.workspace).resolve()
    runid = a.runid or "?"
    events_file = ws / ".opencode" / "plugins" / ".asg-observe" / "runs" / runid / "events.jsonl"
    ct = a.create_time if a.create_time is not None else 0.0
    # active 由安装器 manifest 驱动
    mp = ws / ".opencode" / "plugins" / ".asg-observe" / "manifest.json"
    active = mp.exists() and json.loads(mp.read_text()).get("active") is True
    srv = ObserveServer(events_file, a.nonce, a.pid, ct, ttl_s=a.ttl, active=active).start()
    print(json.dumps(srv.api_status(), ensure_ascii=False))
    try:
        import signal
        import threading
        stop = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        stop.wait()
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())