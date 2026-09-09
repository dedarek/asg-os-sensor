# -*- coding: utf-8 -*-
"""真实子进程 CLI 全链路测试（可复现；node 经 ASG_TEST_NODE 或 which 解析，缺则 skip）。"""
import json, os, psutil, shutil, subprocess, sys, tempfile, time, unittest, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

NODE = os.environ.get("ASG_TEST_NODE") or shutil.which("node") or ""
REQUIRE_NODE = bool(NODE)
ROOT = Path(__file__).resolve().parent


def run(*a):
    return subprocess.run([sys.executable, "-B", str(ROOT / "runtime" / "opencode" / "ghost_install.py"), *a],
                          capture_output=True, text=True)


def state_dir(ws):
    return ws / ".opencode" / "plugins" / ".asg-observe"


def active_manifest(sd):
    return json.loads((sd / "manifest.json").read_text())


def start_server(ws, pid, ct):
    return subprocess.Popen([sys.executable, "-B", str(ROOT / "runtime" / "opencode" / "server.py"),
                             "--workspace", str(ws), "--pid", str(pid), "--create-time", str(ct)],
                            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_port(ws, timeout=10.0):
    # 有界轮询读取端口文件（不依赖子进程 stdout，无 ResourceWarning）
    pf = state_dir(ws) / 'server.port'
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pf.exists():
            try:
                return int(pf.read_text().strip())
            except (OSError, ValueError):
                pass
        time.sleep(0.1)
    raise RuntimeError('server no port')



class RealCliTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not REQUIRE_NODE:
            raise unittest.SkipTest("node not available (set ASG_TEST_NODE)")

    def test_full_chain_dynamic_revoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            pid = os.getpid(); ct = psutil.Process(pid).create_time()
            d1 = json.loads(run("--install", "--workspace", str(ws), "--nonce", "NR1").stdout)
            sd = state_dir(ws)
            man1 = active_manifest(sd)
            evf1 = sd / "runs" / man1["runid"] / "events.jsonl"
            evf1.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                        "event_type": "hook.loaded", "adapter_source": "asg-observe-v1",
                                        "nonce": "NR1", "pid": pid}) + chr(10))
            srv = start_server(ws, pid, ct)
            try:
                port = wait_port(ws)
                base = "http://127.0.0.1:%d" % port
                with urllib.request.urlopen(base + "/health") as r:
                    self.assertTrue(json.loads(r.read())["healthy"])
                run("--uninstall", "--workspace", str(ws), "--name", "asg-observe.js")
                try:
                    urllib.request.urlopen(base + "/health")
                    self.fail("expected 503")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 503)
                    self.assertIn("no active manifest", json.loads(e.read())["error"])
                d2 = json.loads(run("--install", "--workspace", str(ws), "--nonce", "NR2").stdout)
                man2 = active_manifest(sd)
                evf2 = sd / "runs" / man2["runid"] / "events.jsonl"
                evf2.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                            "event_type": "tool.execute.before", "adapter_source": "asg-observe-v1",
                                            "nonce": "NR2", "pid": pid, "call_id": "c2"}) + chr(10))
                with urllib.request.urlopen(base + "/events") as r:
                    ev = json.loads(r.read())
                    self.assertEqual(ev["valid"], 1)
                    self.assertEqual(ev["events"][0]["call_id"], "c2")
                    self.assertNotIn("nonce", json.dumps(ev["events"][0]))
            finally:
                srv.terminate(); srv.wait(timeout=3)

    def test_http_wrong_workspace_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            pid = os.getpid(); ct = psutil.Process(pid).create_time()
            d = json.loads(run("--install", "--workspace", str(ws), "--nonce", "NW").stdout)
            sd = state_dir(ws)
            man = active_manifest(sd)
            man["workspace"] = str(ws) + "/elsewhere"
            (sd / "manifest.json").write_text(json.dumps(man))
            evf = sd / "runs" / man["runid"] / "events.jsonl"
            evf.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                       "event_type": "hook.loaded", "adapter_source": "asg-observe-v1",
                                       "nonce": "NW", "pid": pid}) + chr(10))
            srv = start_server(ws, pid, ct)
            try:
                port = wait_port(ws)
                base = "http://127.0.0.1:%d" % port
                try:
                    urllib.request.urlopen(base + "/health")
                    self.fail("expected 503 for wrong workspace")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 503)
                    self.assertIn("workspace mismatch", json.loads(e.read())["error"])
            finally:
                srv.terminate(); srv.wait(timeout=3)

    def test_plugin_freeze_old_callback_dead_after_reinstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            d1 = json.loads(run("--install", "--workspace", str(ws), "--nonce", "RN").stdout)
            sd = state_dir(ws)
            man1 = active_manifest(sd)
            evf1 = sd / "runs" / man1["runid"] / "events.jsonl"
            script = r'''
const fs=require('node:fs'); const path=require('node:path');
const src=process.env.PLUGIN_PATH;
(async () => {
  const plug = await require(src)({directory:'/'});
  const inp={tool:'bash',callID:'c1'};
  await plug['tool.execute.before'](inp,{});
  await plug['tool.execute.after'](inp);
  console.log('PHASE1');
  await new Promise(r=>setTimeout(r,2500));
  await plug['tool.execute.before'](inp,{});
  await plug['tool.execute.after'](inp);
  console.log('PHASE2');
  process.exit(0);
})();
'''
            p = subprocess.Popen([NODE, "-e", script],
                                 env={**os.environ, "PLUGIN_PATH": str(Path(d1["installed"]))},
                                 cwd=str(ws), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                deadline = time.time() + 12; n = 0
                while time.time() < deadline and n < 3:
                    if evf1.exists(): n = len(evf1.read_text().splitlines())
                    time.sleep(0.1)
                self.assertGreaterEqual(n, 3, "A emits")
                run("--uninstall", "--workspace", str(ws), "--name", "asg-observe.js")
                d2 = json.loads(run("--install", "--workspace", str(ws), "--nonce", "RN2").stdout)
                man2 = active_manifest(sd)
                evf2 = sd / "runs" / man2["runid"] / "events.jsonl"
                time.sleep(3.0)
                out = p.communicate(timeout=5)[0]
                self.assertIn("PHASE2", out)
                size = evf2.stat().st_size if evf2.exists() else -1
                self.assertEqual(size, 0, "old callback wrote new run")
            finally:
                p.terminate(); p.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()