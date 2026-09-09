# -*- coding: utf-8 -*-
"""隔离观测 HTTP 服务：把已验证的 event/health 接到 HTTP 接口（不驻留库对象）。

绑定 127.0.0.1 随机端口，仅本机可访问：
  GET /        状态总览
  GET /health  健康（撤销/时效/绑定过滤判定，manifest 每请求动态校验）
  GET /events  已验证事件（投影最小字段，不公开 nonce；限条数）
运行中卸载下一请求立即 revoked（不等待 TTL）；manifest 缺失/损坏/不匹配即 fail closed。
runid 与 nonce 由 manifest 提供；实例绑定由调用方显式给引擎快照（--pid --create-time），
不从事件自报选目标。"""
import argparse
import json
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # 脚本方式启动保证 runtime 可导入
    sys.path.insert(0, str(ROOT))

from runtime.opencode.event_api import EventVerifier  # noqa: E402

MAX_EVENTS = 500


def load_manifest(ws: Path):
    """返回 (manifest, err)。manifest 缺失/损坏/非 active 均视为不可信。"""
    mp = ws / ".opencode" / "plugins" / ".asg-observe" / "manifest.json"
    try:
        if not mp.exists():
            return None, "no active manifest"
        man = json.loads(mp.read_text(encoding="utf-8"))
        if not man.get("active") or not man.get("runid"):
            return None, "manifest not active or missing runid"
        return man, None
    except (OSError, ValueError) as exc:
        return None, "manifest unreadable/corrupt: %s" % type(exc).__name__


def resolve_events_file(ws: Path, runid: str) -> Path:
    return ws / ".opencode" / "plugins" / ".asg-observe" / "runs" / runid / "events.jsonl"


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

    def _verifier(self):
        """每请求动态重建：manifest 提供 runid/nonce/active，绑定用显式引擎快照。"""
        srv = self.server
        man, err = load_manifest(srv.workspace)
        if err:
            return None, err
        runid = man["runid"]
        evf = resolve_events_file(srv.workspace, runid)
        verifier = EventVerifier(man.get("nonce", ""), srv.engine_pid, srv.engine_ct,
                                 ttl_s=srv.ttl_s, active=True)
        return (verifier, evf, runid), None

    def do_GET(self):
        srv = self.server
        if self.path == "/":
            return self._json(200, {"service": "asg-observe", "status": "ok"})
        res, err = self._verifier()
        if err:
            return self._json(503, {"error": err})  # fail closed
        verifier, evf, runid = res
        if self.path == "/health":
            rows = verifier.read_raw(evf)
            h = verifier.current_health(rows)
            return self._json(200 if h["healthy"] else 503, {**h, "runid": runid,
                                                               "loaded_observed": verifier.loaded_observed(rows),
                                                               "wired": True})
        if self.path == "/events":
            rows = verifier.read_raw(evf)[-MAX_EVENTS:]
            valid, invalid = verifier.bound_events(rows)
            def _project(e):  # 最小公开字段，不公开 nonce（nonce 用于绑定）
                return {k: e.get(k) for k in ("ts", "event_type", "adapter_source", "sdk",
                                             "pid", "call_id", "tool", "outcome")}
            return self._json(200, {"valid": len(valid), "invalid": len(invalid),
                                    "events": [_project(e) for e in valid]})
        self._json(404, {"error": "not found"})


class ObserveServer:
    def __init__(self, workspace: Path, engine_pid: int, engine_ct: float, ttl_s: float = 60.0):
        self.workspace = workspace
        self.engine_pid = engine_pid
        self.engine_ct = engine_ct
        self.ttl_s = ttl_s
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.workspace = workspace  # type: ignore[attr-defined]
        self.httpd.engine_pid = engine_pid  # type: ignore[attr-defined]
        self.httpd.engine_ct = engine_ct  # type: ignore[attr-defined]
        self.httpd.ttl_s = ttl_s  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]

    def start(self):
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def api_status(self):
        return {"type": "http", "wired": True, "port": self.port,
                "endpoints": {"health": "/health", "events": "/events"}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--pid", type=int, default=os.getpid(), help="engine PID from trusted snapshot")
    ap.add_argument("--create-time", type=float, default=None, help="engine create_time from trusted snapshot")
    ap.add_argument("--ttl", type=float, default=60.0)
    a = ap.parse_args()
    if a.create_time is None:
        print("error: --create-time required (trusted engine snapshot)", file=sys.stderr)
        return 1
    ws = Path(a.workspace).resolve()
    srv = ObserveServer(ws, a.pid, a.create_time, ttl_s=a.ttl).start()
    print(json.dumps(srv.api_status(), ensure_ascii=False), flush=True)
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        stop.wait()
    finally:
        srv.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())