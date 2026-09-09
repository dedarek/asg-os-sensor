# -*- coding: utf-8 -*-
"""OpenCode 纯观测插件/安装器/健康/HTTP 集成测试（隔离、可复现）。"""
import json, os, subprocess, sys, tempfile, time, unittest, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
import psutil
from runtime.opencode.event_api import EventVerifier

ROOT = Path(__file__).resolve().parent
GHOST = ROOT / "runtime" / "opencode" / "ghost_install.py"
NODE = "/Users/mac/.nvm/versions/node/v24.16.0/bin/node"


def run(*a):
    return subprocess.run([sys.executable, "-B", str(GHOST), *a], capture_output=True, text=True)


def state_dir(ws):
    return ws / ".opencode" / "plugins" / ".asg-observe"


def active_manifest(sd):
    m = json.loads((sd / "manifest.json").read_text())
    return m


class TransactionInstallTests(unittest.TestCase):
    def test_install_uninstall_reinstall_and_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            d = json.loads(run("--install", "--workspace", str(ws), "--nonce", "N1").stdout)
            self.assertTrue(Path(d["installed"]).exists())
            sd = state_dir(ws)
            man = active_manifest(sd)
            self.assertEqual(oct(sd.stat().st_mode & 0o777), "0o700")
            self.assertEqual(oct((sd / "manifest.json").stat().st_mode & 0o777), "0o600")
            self.assertEqual(run("--install", "--workspace", str(ws), "--nonce", "N2").returncode, 1)
            r = run("--uninstall", "--workspace", str(ws), "--name", "asg-observe.js")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(Path(d["installed"]).exists())
            self.assertTrue((sd / "runs" / man["runid"] / "events.jsonl").exists())
            self.assertFalse((sd / "manifest.json").exists())
            self.assertTrue((sd / "manifest.json.inactive").exists())
            d2 = json.loads(run("--install", "--workspace", str(ws), "--nonce", "N3").stdout)
            man2 = active_manifest(sd)
            self.assertNotEqual(man2["runid"], man["runid"])
            (ws / ".opencode" / "plugins" / "asg-observe.js").write_bytes(b"occupied")
            r = run("--install", "--workspace", str(ws), "--nonce", "N4")
            self.assertEqual(r.returncode, 1)
            self.assertEqual(len(list((sd / "runs").iterdir())), 2)

    def test_path_safety_rejects_symlink_components(self):
        with tempfile.TemporaryDirectory() as tmp:
            linkroot = Path(tmp) / "linkroot"; linkroot.mkdir()
            target = Path(tmp) / "outside"; target.mkdir()
            try:
                (linkroot / ".opencode").symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("symlink unsupported")
            sentinel = target / "keep.txt"; sentinel.write_text("user file")
            r = run("--install", "--workspace", str(linkroot), "--name", "x.js")
            self.assertEqual(r.returncode, 1)
            self.assertTrue(sentinel.exists(), "external sentinel preserved")

    def test_name_traversal_rejected(self):
        for name in ("../evil.js", "/etc/evil.js", "."):
            with tempfile.TemporaryDirectory() as tmp:
                r = run("--install", "--workspace", str(tmp), "--name", name)
                self.assertEqual(r.returncode, 1, name)

    def test_modified_plugin_refuses_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            d = json.loads(run("--install", "--workspace", str(ws), "--nonce", "N1").stdout)
            p = Path(d["installed"]); p.write_bytes(p.read_bytes() + b"X")
            r = run("--uninstall", "--workspace", str(ws), "--name", "asg-observe.js")
            self.assertEqual(r.returncode, 1)
            self.assertTrue(p.exists())


def node_fixture(plugin: str, directory: str, phase: int) -> str:
    # 同一 node 进程内多次触发；phase=1 安装后触发，phase=2 卸载后再次触发
    import json as _j
    return chr(10).join([
        "const init = async () => {",
        "  const mod = await import('file://" + plugin + "');",
        "  const plug = await mod.default({ directory: " + _j.dumps(directory) + " });",
        "  const input = { tool: 'bash', callID: 'c1' };",
        "  await plug['tool.execute.before'](input, {});",
        "  await plug['tool.execute.after'](input);",
        "  setTimeout(() => process.exit(0), 5000);",
        "};",
        "init().then(() => {}).catch((e) => { console.error(e); process.exit(1); });",
    ])


class PluginEventsTests(unittest.TestCase):
    def test_uninstall_stops_emit_without_engine_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            d = json.loads(run("--install", "--workspace", str(ws), "--nonce", "NN").stdout)
            sd = state_dir(ws)
            man = active_manifest(sd)
            events_file = sd / "runs" / man["runid"] / "events.jsonl"
            # 阶段1：真实 node 进程加载插件并触发（引擎长驻）
            fx = node_fixture(str(Path(d["installed"])), str(ws), 1)
            p1 = subprocess.Popen([NODE, "-e", fx], cwd=str(ws), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 15
                n = 0
                while time.time() < deadline and n < 2:
                    if events_file.exists():
                        n = len(events_file.read_text().splitlines())
                    time.sleep(0.1)
                self.assertGreaterEqual(n, 2, "installed emits before+after")
            finally:
                p1.terminate(); p1.wait(timeout=3)
            # 卸载（同进程插件仍在内存；重跑相同 fixture）
            run("--uninstall", "--workspace", str(ws), "--name", "asg-observe.js")
            p2 = subprocess.Popen([NODE, "-e", fx], cwd=str(ws), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(1.5)
                n_after = len(events_file.read_text().splitlines()) if events_file.exists() else 0
                self.assertEqual(n_after, n, "uninstall must stop emit without engine restart")
            finally:
                p2.terminate(); p2.wait(timeout=3)

    def test_event_contract_and_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            d = json.loads(run("--install", "--workspace", str(ws), "--nonce", "NC").stdout)
            sd = state_dir(ws)
            man = active_manifest(sd)
            events_file = sd / "runs" / man["runid"] / "events.jsonl"
            fx = node_fixture(str(Path(d["installed"])), str(ws), 1)
            p = subprocess.Popen([NODE, "-e", fx], cwd=str(ws), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.time() + 15; rows = []
                while time.time() < deadline and len(rows) < 3:
                    if events_file.exists():
                        rows = [json.loads(l) for l in events_file.read_text().splitlines() if l.strip()]
                    time.sleep(0.1)
                self.assertGreaterEqual(len(rows), 2)
            finally:
                p.terminate(); p.wait(timeout=3)
            blob = events_file.read_text()
            self.assertNotIn("TOPSECRET", blob)
            self.assertNotIn("ls -la", blob)
            self.assertNotIn("directory", blob)
            self.assertEqual(oct(events_file.stat().st_mode & 0o777), "0o600")
            allowed = {"ts", "event_type", "adapter_source", "sdk", "nonce", "pid", "call_id", "tool", "outcome"}
            for line in blob.splitlines():
                if line.strip():
                    self.assertTrue(set(json.loads(line).keys()) <= allowed, line)


class EventVerifierHealthTests(unittest.TestCase):
    def _mk(self, pid, ct):
        return EventVerifier("N", pid, ct, ttl_s=60.0)

    def test_health_requires_fresh_bound_no_future(self):
        pid = os.getpid(); ct = psutil.Process(pid).create_time()
        v = self._mk(pid, ct)
        now = datetime.now(timezone.utc)
        ok = [{"ts": now.isoformat(), "event_type": "hook.loaded", "adapter_source": "s", "nonce": "N", "pid": pid},
              {"ts": now.isoformat(), "event_type": "tool.execute.before", "adapter_source": "s", "nonce": "N", "pid": pid}]
        self.assertTrue(v.current_health(ok)["healthy"])
        future = datetime.fromtimestamp(time.time() + 99999, timezone.utc).isoformat()
        fut = [{"ts": future, "event_type": "hook.loaded", "adapter_source": "s", "nonce": "N", "pid": pid}]
        self.assertFalse(v.current_health(fut)["healthy"])
        stale_ok = (now - timedelta(seconds=5)).isoformat()
        mixed = [{"ts": stale_ok, "event_type": "hook.loaded", "adapter_source": "s", "nonce": "N", "pid": pid},
                 {"ts": now.isoformat(), "event_type": "tool.execute.before", "adapter_source": "s", "nonce": "BAD", "pid": pid}]
        self.assertFalse(v.current_health(mixed)["healthy"])
        self.assertEqual(v.current_health([])["status"], "unknown")
        v_in = EventVerifier("N", pid, ct, ttl_s=60.0, active=False)
        self.assertEqual(v_in.current_health(ok)["status"], "revoked")

    def test_read_raw_rejects_arrays_and_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "e.jsonl"
            payload = '[{"a":1}]' + chr(10) + '42' + chr(10) + '"str"' + chr(10) + '{"ok":true}' + chr(10)
            f.write_text(payload)
            rows = EventVerifier("N", 1, 1.0).read_raw(f)
            self.assertEqual([r for r in rows if r.get("ok")], [{"ok": True}])

    def test_api_status_not_wired_library(self):
        self.assertFalse(EventVerifier("N", 1, 1.0).api_status()["wired"])


class HttpResponseTests(unittest.TestCase):
    def test_http_endpoints_wired(self):
        from runtime.opencode.server import ObserveServer
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            d = json.loads(run("--install", "--workspace", str(ws), "--nonce", "NH").stdout)
            sd = state_dir(ws)
            man = active_manifest(sd)
            evf = sd / "runs" / man["runid"] / "events.jsonl"
            evf.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                       "event_type": "hook.loaded", "adapter_source": "asg-observe-v1",
                                       "nonce": "NH", "pid": os.getpid()}) + chr(10))
            srv = ObserveServer(evf, "NH", os.getpid(), psutil.Process(os.getpid()).create_time(), active=True)
            try:
                srv.start()
                base = "http://127.0.0.1:%d" % srv.port
                with urllib.request.urlopen(base + "/") as r:
                    self.assertEqual(json.loads(r.read())["status"], "ok")
                with urllib.request.urlopen(base + "/health") as r:
                    h = json.loads(r.read())
                    self.assertTrue(h["healthy"] and h["wired"])
                with urllib.request.urlopen(base + "/events") as r:
                    self.assertEqual(json.loads(r.read())["valid"], 1)
            finally:
                srv.stop()


if __name__ == "__main__":
    unittest.main()