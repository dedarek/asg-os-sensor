# -*- coding: utf-8 -*-
"""observe HTTP 页面/健康/事件语义测试（隔离临时目录 + 真实子进程 CLI，无 node）。

覆盖：真实绑定实例事件通过校验；nonce 绝不出现在页面或 JSON 投影；
未绑定/空闲/已撤销/损坏 manifest 均 fail closed，不虚构状态。"""
import json, os, subprocess, sys, tempfile, time, unittest, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path
import psutil

ROOT = Path(__file__).resolve().parent
GHOST = ROOT / "runtime" / "opencode" / "ghost_install.py"
SERVER = ROOT / "runtime" / "opencode" / "server.py"


def run_ghost(*a):
    return subprocess.run([sys.executable, "-B", str(GHOST), *a], capture_output=True, text=True)


def state_dir(ws):
    return ws / ".opencode" / "plugins" / ".asg-observe"


def events_file(ws, runid):
    return state_dir(ws) / "runs" / runid / "events.jsonl"


def write_events(ws, runid, events):
    p = events_file(ws, runid)
    with p.open("a", encoding="utf-8") as f:
        for e in events:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def make_event(event_type, pid, call_id=None, tool=None, outcome=None, nonce="TEST-1"):
    return {"ts": now_ts(), "event_type": event_type, "adapter_source": "asg-observe-v1",
            "sdk": "opencode-plugin", "nonce": nonce, "pid": pid, "call_id": call_id,
            "tool": tool, "outcome": outcome}


class ObservePageHttpTests(unittest.TestCase):
    def setUp(self):
        self.pid = os.getpid()
        self.ct = psutil.Process(self.pid).create_time()
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = Path(self.tmp.name)
        self.install = json.loads(run_ghost("--install", "--workspace", str(self.ws),
                                            "--nonce", "TEST-1").stdout)
        self.runid = self.install["runid"]
        self.srv = subprocess.Popen(
            [sys.executable, "-B", str(SERVER), "--workspace", str(self.ws),
             "--pid", str(self.pid), "--create-time", str(self.ct), "--ttl", "60"],
            cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.port = self._wait_port()

    def _wait_port(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.srv.stdout.readline() if self.srv.stdout else ""
            if '"port"' in line:
                return json.loads(line)["port"]
        raise RuntimeError("server did not report port")

    def tearDown(self):
        self.srv.terminate()
        try:
            self.srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.srv.kill()
        self.tmp.cleanup()

    def _get(self, path, accept="application/json"):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path),
                                     headers={"Accept": accept})
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    def test_browser_page_never_leaks_nonce(self):
        # 页面（无论有无事件）都不得包含 nonce
        s0, b0 = self._get("/", accept="text/html")
        self.assertEqual(s0, 200)
        self.assertNotIn("TEST-1", b0)
        write_events(self.ws, self.runid, [
            make_event("hook.loaded", self.pid),
            make_event("tool.execute.before", self.pid, call_id="c1", tool="read", outcome="started"),
            make_event("tool.execute.after", self.pid, call_id="c1", tool="read", outcome="finished"),
        ])
        s, b = self._get("/page", accept="text/html")
        self.assertEqual(s, 200)
        self.assertNotIn("TEST-1", b)
        self.assertIn("hook.loaded", b)
        self.assertIn("已观测到加载握手", b)
        self.assertIn("工具调用开始（仅观测", b)
        self.assertIn("未安装控制面", b)  # 不宣称防护

    def test_real_bound_events_pass_and_health_healthy(self):
        write_events(self.ws, self.runid, [
            make_event("hook.loaded", self.pid),
            make_event("tool.execute.before", self.pid, call_id="c1", tool="read", outcome="started"),
        ])
        status, body = self._get("/health")
        self.assertEqual(status, 200)
        h = json.loads(body)
        self.assertTrue(h["healthy"])
        self.assertEqual(h["status"], "healthy")
        self.assertTrue(h["loaded_observed"])
        s2, b2 = self._get("/events")
        self.assertEqual(s2, 200)
        ev = json.loads(b2)
        self.assertEqual(ev["valid"], 2)
        self.assertEqual(ev["invalid"], 0)
        for e in ev["events"]:
            self.assertNotIn("nonce", e)  # 投影不公开 nonce

    def test_unbound_event_counts_as_invalid_and_blocks_health(self):
        write_events(self.ws, self.runid, [
            make_event("hook.loaded", self.pid),
            make_event("tool.execute.before", 999999, call_id="c1", tool="read", outcome="started"),
        ])
        s, b = self._get("/events")
        ev = json.loads(b)
        self.assertEqual(ev["valid"], 1)
        self.assertEqual(ev["invalid"], 1)
        s2, b2 = self._get("/health")
        self.assertEqual(s2, 503)
        self.assertEqual(json.loads(b2)["status"], "unbound")

    def test_idle_unknown_not_fault(self):
        s, b = self._get("/health")
        self.assertEqual(s, 503)
        h = json.loads(b)
        self.assertEqual(h["status"], "unknown")
        self.assertFalse(h["healthy"])

    def test_revoked_after_uninstall_fail_closed(self):
        r = run_ghost("--uninstall", "--workspace", str(self.ws), "--name", "asg-observe.js")
        self.assertEqual(r.returncode, 0, r.stderr)
        for path in ("/health", "/events", "/page"):
            s, b = self._get(path, accept="text/html" if path == "/page" else "application/json")
            self.assertEqual(s, 503, path)
            self.assertIn("manifest", b)  # 无 active manifest -> fail closed

    def test_corrupt_manifest_never_becomes_valid_history(self):
        # 损坏 prior 不能被静默解释为“无历史”：损坏 manifest 即 fail closed
        mp = state_dir(self.ws) / "manifest.json"
        mp.write_text("{corrupt json", encoding="utf-8")
        s, b = self._get("/health")
        self.assertEqual(s, 503)
        self.assertIn("corrupt", b)

    def test_pid_reuse_rejected_by_live_create_time(self):
        # PID 被另一存活进程复用：实时 create_time 不匹配 -> unbound（防 PID 复用串结果）
        other_pid = self.srv.pid  # 服务器进程本身是另一存活进程
        write_events(self.ws, self.runid, [make_event("hook.loaded", other_pid)])
        s, b = self._get("/health")
        self.assertEqual(s, 503)
        self.assertEqual(json.loads(b)["status"], "unbound")


if __name__ == "__main__":
    unittest.main()
