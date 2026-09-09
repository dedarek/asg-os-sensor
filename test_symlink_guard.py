# -*- coding: utf-8 -*-
"""server 读取端 symlink 防护负例（manifest 文件 symlink -> 503 fail closed）。注TOCTOU不完全。"""
import json, os, psutil, subprocess, sys, tempfile, time, unittest, urllib.request, urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def run(*a):
    return subprocess.run([sys.executable, "-B", str(ROOT / "runtime" / "opencode" / "ghost_install.py"), *a],
                          capture_output=True, text=True)


def state_dir(ws):
    return ws / ".opencode" / "plugins" / ".asg-observe"


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



class SymlinkGuardTests(unittest.TestCase):
    def test_manifest_symlink_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            pid = os.getpid(); ct = psutil.Process(pid).create_time()
            d = json.loads(run("--install", "--workspace", str(ws), "--nonce", "NS").stdout)
            sd = state_dir(ws)
            man = json.loads((sd / "manifest.json").read_text())
            evf = sd / "runs" / man["runid"] / "events.jsonl"
            evf.write_text(json.dumps({"ts": datetime.now(timezone.utc).isoformat(),
                                       "event_type": "hook.loaded", "adapter_source": "asg-observe-v1",
                                       "nonce": "NS", "pid": pid}) + chr(10))
            outside = Path(tmp) / "external.json"
            outside.write_text(json.dumps({"active": True, "runid": man["runid"],
                                           "nonce": "NS", "workspace": str(ws)}))
            (sd / "manifest.json").unlink()
            try:
                (sd / "manifest.json").symlink_to(outside)
            except OSError:
                self.skipTest("symlink unsupported")
            srv = start_server(ws, pid, ct)
            try:
                port = wait_port(ws)
                base = "http://127.0.0.1:%d" % port
                try:
                    urllib.request.urlopen(base + "/health")
                    self.fail("expected 503 for manifest symlink")
                except urllib.error.HTTPError as e:
                    self.assertEqual(e.code, 503)
                    self.assertIn("symlink", json.loads(e.read())["error"])
            finally:
                srv.terminate(); srv.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()