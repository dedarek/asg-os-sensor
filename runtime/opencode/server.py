# -*- coding: utf-8 -*-
"""隔离观测 HTTP 服务：把已验证的 event/health 接到 HTTP 接口（不驻留库对象）。

绑定 127.0.0.1 随机端口或 --port 指定端口，仅本机可访问：
  GET /           浏览器：只读状态页（安装/实例绑定/新鲜度/事件投影，不公开 nonce）
  GET /page       状态页（同上，强制 HTML）
  GET /health     健康（撤销/时效/绑定过滤判定，manifest 每请求动态校验）
  GET /events     已验证事件（投影最小字段，不公开 nonce；限条数）
运行中卸载下一请求立即 revoked（不等待 TTL）；manifest 缺失/损坏/不匹配即 fail closed。
runid 与 nonce 由 manifest 提供；实例绑定由调用方显式给引擎快照（--pid --create-time），
不从事件自报选目标。

页面语义（如实，不虚构能力）：
  - 加载握手（hook.loaded）只代表「引擎曾加载插件」，不代表已防护。
  - 观测事件（tool.execute.before/after）只证明工具调用可被记录，不代表可阻断。
  - 当前新鲜度由最新有效事件时间与 TTL 判定；空闲=未知，非故障。
  - 只描述绑定实例（--pid 快照）；不把其他进程/家族实例标为已挂接。
"""
import argparse
import datetime as _dt
import html as _html
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

CAPABILITIES = {
    "observation": {"status": "supported", "label": "事件观测"},
    "blocking": {"status": "unsupported", "label": "未支持"},
}

EVENT_LABELS = {
    "hook.loaded": "加载握手（仅观测：引擎已加载插件，不代表已防护）",
    "tool.execute.before": "工具调用开始（仅观测：事件可记录，不代表可阻断）",
    "tool.execute.after": "工具调用结束（仅观测）",
}

HEALTH_LABELS = {
    "healthy": "健康：有效且新鲜且有加载握手",
    "stale": "时效过期：无 TTL 窗口内的有效事件",
    "unknown": "未知：无事件（空闲，非故障，不虚构状态）",
    "unbound": "绑定不匹配：事件无法匹配已声明的实例",
    "nohandshake": "有有效事件但加载握手不在新鲜窗口内",
    "revoked": "已撤销：安装器已标记 inactive（下一请求立即生效）",
}

CAPABILITY_NOTE = (
    "本阶段仅实现观测记录：工具事件写入隔离 events 文件并由接收端校验绑定。"
    "未安装控制面、无阻断/防护能力，任何显示均不代表「已挂接防护」。"
)


def _runid_ok(runid):
    # bare run id: reject path traversal and empty
    if not isinstance(runid, str) or not runid:
        return False
    return not (runid.startswith('/') or '..' in runid or chr(92) in runid or '/' in runid)


def load_manifest(ws: Path, expected_workspace=None):
    """Strict validation: active must be True, runid bare-safe, nonce non-empty,
    workspace must equal bound path; anything else fails (503)."""
    mp = ws / '.opencode' / 'plugins' / '.asg-observe' / 'manifest.json'
    try:
        if not mp.exists():
            return None, 'no active manifest'
        man = json.loads(mp.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        return None, 'manifest unreadable/corrupt: %s' % type(exc).__name__
    if not isinstance(man, dict) or man.get('active') is not True:
        return None, 'manifest not active (active must be boolean true)'
    if not _runid_ok(man.get('runid')):
        return None, 'manifest runid invalid (must be non-empty bare id)'
    if not isinstance(man.get('nonce'), str) or not man.get('nonce'):
        return None, 'manifest nonce empty/invalid'
    expected = str(expected_workspace) if expected_workspace is not None else str(ws)
    if man.get('workspace') != expected:
        return None, 'manifest workspace mismatch (expected %s)' % expected
    return man, None


def resolve_events_file(ws: Path, runid: str) -> Path:
    if not _runid_ok(runid):
        raise ValueError('invalid runid')
    return ws / '.opencode' / 'plugins' / '.asg-observe' / 'runs' / runid / 'events.jsonl'


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

    def _rejected_payload(self, reason):
        """即使 manifest 无效/被卸载也返回已知实例绑定，且始终 fail closed。"""
        srv = self.server
        return {
            "status": "revoked",
            "healthy": False,
            "reason": reason,
            "error": reason,
            "manifest_valid": False,
            "instance_pid": srv.engine_pid,
            "instance_create_time": srv.engine_ct,
            "instance_id": "%s:%s" % (srv.engine_pid, srv.engine_ct),
            "capabilities": CAPABILITIES,
            "wired": True,
        }

    def _verify_paths(self, ws, evf):
        """读取路径安全：state 目录、manifest 文件与 events 文件全部组件拒绝 symlink。"""
        import stat
        base = ws / '.opencode' / 'plugins' / '.asg-observe'
        for path in (base, base / 'manifest.json', evf):
            cur = Path(path.anchor) if path.anchor else Path('/')
            for part in path.parts[1:]:
                cur = cur / part
                try:
                    st = cur.lstat()
                except FileNotFoundError:
                    continue
                if stat.S_ISLNK(st.st_mode):
                    return 'symlink in path: %s' % cur
        return None

    def _verifier(self):
        """每请求动态重建：先校验路径（含 manifest 自身）再读取 manifest，避免先读外部文件。"""
        srv = self.server
        evf = resolve_events_file(srv.workspace, 'x')
        bad = self._verify_paths(srv.workspace, evf)
        if bad:
            return None, bad
        man, err = load_manifest(srv.workspace, expected_workspace=srv.workspace)
        if err:
            return None, err
        runid = man['runid']
        evf = resolve_events_file(srv.workspace, runid)
        bad2 = self._verify_paths(srv.workspace, evf)
        if bad2:
            return None, bad2
        verifier = EventVerifier(man.get('nonce', ''), srv.engine_pid, srv.engine_ct,
                                 ttl_s=srv.ttl_s, active=True)
        return (verifier, evf, runid), None

    def _accepts_html(self):
        return "text/html" in (self.headers.get("Accept", "") or "")

    def _view_context(self):
        """构建页面视图上下文；err 不为空时页面/健康接口必须 fail closed。"""
        srv = self.server
        res, err = self._verifier()
        if err:
            return {"error": err, "rejected": self._rejected_payload(err)}, err
        verifier, evf, runid = res
        rows = verifier.read_raw(evf)
        valid, invalid = verifier.bound_events(rows)
        health = verifier.current_health(rows)
        loaded_observed = verifier.loaded_observed(rows)
        latest_ts = None
        ts_list = [e.get("ts") for e in valid if isinstance(e.get("ts"), str)]
        if ts_list:
            latest_ts = max(ts_list)
        # 重新读取 manifest 的快照字段用于展示（不展示 nonce）
        man = None
        try:
            mf, _ = load_manifest(srv.workspace, expected_workspace=srv.workspace)
            if mf:
                man = {k: mf.get(k) for k in ("name", "runid", "workspace", "sha256", "installed_at")}
        except Exception:
            man = None
        ctx = {
            "runid": runid,
            "health": health,
            "loaded_observed": loaded_observed,
            "latest_ts": latest_ts,
            "valid": valid,
            "invalid": invalid,
            "manifest": man,
            "engine_pid": srv.engine_pid,
            "engine_ct": srv.engine_ct,
            "ttl_s": srv.ttl_s,
            "now_ts": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        return ctx, None

    def _render_page(self, ctx):
        err = ctx.get("error")
        if err:
            title = "ASG Observe - 不可用（fail closed）"
            rejected = ctx.get("rejected") or {}
            body = (
                "<h2>状态不可用</h2>"
                "<p>隔离观测接口拒绝提供服务（fail closed），原因：</p>"
                "<pre>" + _html.escape(str(err)) + "</pre>"
                "<p>状态：<strong>revoked</strong>（manifest 无效或已卸载） · "
                "绑定实例 PID：<code>" + _html.escape(str(rejected.get("instance_pid", "未知")))
                + "</code> · create_time：<code>"
                + _html.escape(str(rejected.get("instance_create_time", "未知"))) + "</code></p>"
                "<p>这符合预期语义：manifest 缺失/损坏/非 active/实例不匹配时，不虚构状态。</p>"
            )
        else:
            h = ctx["health"]
            title = "ASG Observe - 实例 " + _html.escape(str(ctx["engine_pid"]))
            badge = h["status"]
            label = HEALTH_LABELS.get(h["status"], str(h.get("reason") or h["status"]))
            man = ctx.get("manifest") or {}
            rows = []
            for e in (ctx["valid"] + ctx["invalid"])[-MAX_EVENTS:]:
                et = e.get("event_type", "?")
                rows.append(
                    "<tr><td>" + _html.escape(e.get("ts") or "") + "</td>"
                    + "<td>" + _html.escape(str(et)) + "</td>"
                    + "<td>" + _html.escape(str(e.get("tool") or "")) + "</td>"
                    + "<td>" + _html.escape(str(e.get("call_id") or "")) + "</td>"
                    + "<td>" + _html.escape(str(e.get("outcome") or "")) + "</td>"
                    + "<td>" + _html.escape(str(e.get("pid") or "")) + "</td>"
                    + "<td>" + _html.escape(EVENT_LABELS.get(et, et)) + "</td></tr>"
                )
            if rows:
                events_html = "".join(rows)
            else:
                events_html = (
                    "<tr><td colspan=7>尚无事件（空闲，非故障；"
                    "打开工作区并由引擎加载插件后出现）</td></tr>"
                )
            body = (
                "<h2>ASG OpenCode 纯观测插件 - 状态页</h2>"
                "<div class=cap>" + _html.escape(CAPABILITY_NOTE) + "</div>"
                "<h3>安装与配方</h3><ul>"
                "<li>runid: <code>" + _html.escape(str(ctx["runid"])) + "</code></li>"
                "<li>workspace: <code>" + _html.escape(str(man.get("workspace") or "?")) + "</code></li>"
                "<li>插件 sha256 (manifest): <code>" + _html.escape(str(man.get("sha256") or "?")) + "</code></li>"
                "<li>安装时间: <code>" + _html.escape(str(man.get("installed_at") or "?")) + "</code></li>"
                "</ul>"
                "<h3>实例绑定（可信快照，非事件自报）</h3><ul>"
                "<li>engine pid: <code>" + _html.escape(str(ctx["engine_pid"])) + "</code>，"
                "create_time: <code>" + _html.escape(str(round(ctx["engine_ct"], 6))) + "</code></li>"
                "<li>ttl: <code>" + _html.escape(str(ctx["ttl_s"])) + "s</code></li>"
                "</ul>"
                "<h3>健康与新鲜度</h3><ul>"
                "<li>状态: <strong>" + _html.escape(str(badge)) + "</strong> - " + _html.escape(label) + "</li>"
                "<li>healthy: <code>" + _html.escape(str(h.get("healthy"))) + "</code></li>"
                "<li>reason: <code>" + _html.escape(str(h.get("reason") or "")) + "</code></li>"
                "<li>已观测到加载握手（历史）: <code>" + _html.escape(str(ctx["loaded_observed"])) + "</code>"
                "（历史握手 ≠ 当前有效；当前有效性由上面状态判定）</li>"
                "<li>最新有效事件: <code>" + _html.escape(str(ctx["latest_ts"] or "无")) + "</code></li>"
                "<li>服务器时间 (UTC): <code>" + _html.escape(str(ctx["now_ts"])) + "</code></li>"
                "</ul>"
                "<h3>事件（投影，不公开 nonce；有效 " + str(len(ctx["valid"]))
                + " / 无效 " + str(len(ctx["invalid"])) + "）</h3>"
                "<table border=1 cellspacing=0 cellpadding=4>"
                "<tr><th>ts</th><th>事件类型</th><th>tool</th><th>call_id</th><th>outcome</th>"
                "<th>pid</th><th>语义</th></tr>" + events_html + "</table>"
                "<p><em>注意：同一进程的其他路径、同族其他实例、其他 Agent 均未在此页面证明；"
                "本页只描述绑定实例。</em></p>"
            )
        html_doc = (
            "<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>"
            "<title>" + _html.escape(title) + "</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:2rem;line-height:1.5}"
            ".cap{background:#fdf6e3;border:1px solid #c9b458;padding:.6rem}"
            "code{background:#eee;padding:0 .2rem}table{border-collapse:collapse}"
            "td,th{padding:4px;border:1px solid #999;font-size:13px}</style></head>"
            "<body>" + body + "</body></html>"
        )
        return html_doc

    def do_GET(self):
        srv = self.server
        if self.path in ("/", "/page"):
            ctx, err = self._view_context()
            if self.path == "/" and not self._accepts_html():
                # API 兼容：不带浏览器 Accept 时 / 仍返回 JSON 状态
                if err:
                    return self._json(503, self._rejected_payload(err))
                return self._json(200, {"service": "asg-observe", "status": "ok",
                                        "runid": ctx["runid"]})
            code = 503 if err else 200
            body = self._render_page(ctx).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        res, err = self._verifier()
        if err:
            return self._json(503, self._rejected_payload(err))  # fail closed + known binding
        verifier, evf, runid = res
        if self.path == "/health":
            rows = verifier.read_raw(evf)
            h = verifier.current_health(rows)
            return self._json(200 if h["healthy"] else 503, {**h, "runid": runid,
                                                               "loaded_observed": verifier.loaded_observed(rows),
                                                               "instance_pid": verifier.expected_pid,
                                                               "instance_create_time": verifier.expected_ct,
                                                               "instance_id": "%s:%s" % (verifier.expected_pid, verifier.expected_ct),
                                                               "capabilities": CAPABILITIES,
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
    def __init__(self, workspace: Path, engine_pid: int, engine_ct: float,
                 ttl_s: float = 60.0, port: int = 0):
        self.workspace = workspace
        self.engine_pid = engine_pid
        self.engine_ct = engine_ct
        self.ttl_s = ttl_s
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
                "endpoints": {"status": "/", "page": "/page", "health": "/health",
                              "events": "/events"}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--pid", type=int, required=True, help="engine PID from trusted snapshot (explicit)")
    ap.add_argument("--create-time", type=float, default=None, help="engine create_time from trusted snapshot")
    ap.add_argument("--ttl", type=float, default=60.0)
    ap.add_argument("--port", type=int, default=0, help="listen port (0 = random)")
    a = ap.parse_args()
    if a.create_time is None:
        print("error: --create-time required (trusted engine snapshot)", file=sys.stderr)
        return 1
    ws = Path(a.workspace).resolve()
    srv = ObserveServer(ws, a.pid, a.create_time, ttl_s=a.ttl, port=a.port).start()
    port_file = ws / '.opencode' / 'plugins' / '.asg-observe' / 'server.port'
    port_file.write_text(str(srv.port))
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
