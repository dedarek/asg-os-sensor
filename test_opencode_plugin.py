# -*- coding: utf-8 -*-
"""OpenCode 纯观测插件集成测试（隔离、可复现、真实加载路径）。"""
import json, os, secrets, shutil, subprocess, tempfile, time, unittest
from pathlib import Path
import psutil
from runtime.opencode.event_api import EventVerifier

ROOT = Path(__file__).resolve().parent
NODE = "/Users/mac/.nvm/versions/node/v24.16.0/bin/node"
PLUGIN = ROOT / "runtime" / "opencode" / "asg-observe.js"


def glob_scan(plugins_dir: Path):
    import glob as g
    found = set()
    for ext in ("ts", "js"):
        for p in g.glob(str(plugins_dir / ("*." + ext))):
            if Path(p).is_file():
                found.add(Path(p))
    return sorted(found)


def mk_fixture(plugin_path: Path, directory: str) -> str:
    import json as _j
    return chr(10).join([
        "const init = async () => {",
        "  const mod = await import('file://" + str(plugin_path) + "');",
        "  const plug = await mod.default({ directory: " + _j.dumps(directory) + " });",
        "  const input = { tool: 'bash', callID: 'call-abc-123' };",
        "  await plug['tool.execute.before'](input, { args: { command: 'ls -la', secret: 'TOPSECRET' } });",
        "  await plug['tool.execute.after'](input);",
        "  setTimeout(() => process.exit(0), 30000);",
        "};",
        "init().then(() => {}).catch((e) => { console.error(e); process.exit(1); });",
    ])


def read_events(path: Path):
    if not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def wait_for(path: Path, want: int, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = read_events(path)
        if len(rows) >= want:
            return rows
        time.sleep(0.05)
    return read_events(path)


class OpenCodePluginTests(unittest.TestCase):
    def test_discoverable_by_engine_glob_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugins_dir = root / ".opencode" / "plugins"
            plugins_dir.mkdir(parents=True)
            shutil.copy2(PLUGIN, plugins_dir / "asg-observe.js")
            # 引擎 glob 必须发现插件（.js 在 {ts,js} 扫描范围，.mjs 不在）
            self.assertIn(plugins_dir / "asg-observe.js", glob_scan(plugins_dir))
            # nonce 与事件文件放在插件同目录（真实部署形态，不依赖 env）
            sdir = plugins_dir / ".asg-observe"
            sdir.mkdir()
            nonce = secrets.token_hex(8)
            (sdir / "nonce").write_text(nonce + chr(10))
            events_file = sdir / "events.jsonl"
            events_file.touch()
            os.chmod(events_file, 0o600)
            # 运行 fixture：引擎长驻（Popen），加载插件并触发 before/after
            fx = mk_fixture(plugins_dir / "asg-observe.js", str(root))
            proc = subprocess.Popen([NODE, "-e", fx], env=dict(os.environ), cwd=str(root),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                rows = wait_for(events_file, 3)
                self.assertTrue(rows, "no events written")
                loaded = [e for e in rows if e["event_type"] == "hook.loaded"]
                before = [e for e in rows if e["event_type"] == "tool.execute.before"]
                after = [e for e in rows if e["event_type"] == "tool.execute.after"]
                self.assertTrue(loaded and before and after, "loaded/before/after missing")
                blob = events_file.read_text()
                self.assertNotIn("TOPSECRET", blob)
                self.assertNotIn("ls -la", blob)
                self.assertNotIn("directory", blob)
                allowed = {"ts", "event_type", "adapter_source", "sdk", "nonce",
                           "pid", "call_id", "tool", "outcome"}
                for e in rows:
                    self.assertTrue(set(e.keys()) <= allowed, "unexpected fields: %s" % (set(e.keys()) - allowed))
                for e in before + after:
                    self.assertEqual(e["call_id"], "call-abc-123")
                    self.assertEqual(e["tool"], "bash")
                self.assertEqual(oct(events_file.stat().st_mode & 0o777), "0o600")
                # 绑定：进程仍存活（引擎长驻），实测 create_time
                pid = loaded[0]["pid"]; ct = psutil.Process(pid).create_time()
                v = EventVerifier(nonce, pid, ct)
                v.verify_instance(loaded[0])
                with self.assertRaises(ValueError):
                    EventVerifier("x" + nonce, pid, ct).verify_instance(loaded[0])
                with self.assertRaises(ValueError):
                    EventVerifier(nonce, 999999, ct).verify_instance(loaded[0])
                with self.assertRaises(ValueError):
                    EventVerifier(nonce, pid, ct + 1000).verify_instance(loaded[0])
                # 健康语义与撤销/时效
                self.assertTrue(v.loaded_observed(rows))
                self.assertTrue(v.current_health(rows)["healthy"])
                stale = [dict(r, ts="2000-01-01T00:00:00+00:00") for r in rows]
                self.assertFalse(v.current_health(stale)["healthy"])
                revoked = rows + [{"ts": "2999-01-01T00:00:00+00:00", "event_type": "hook.revoked"}]
                self.assertFalse(v.current_health(revoked)["healthy"])
                # API 未接线
                self.assertFalse(v.api_status()["wired"])
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            # 卸载：删插件后同 fixture 不再写事件；历史 events 保留
            (plugins_dir / "asg-observe.js").unlink()
            events2 = root / "events2.jsonl"
            proc2 = subprocess.Popen([NODE, "-e", fx], env=dict(os.environ), cwd=str(root),
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(1.0)
                self.assertFalse(events2.exists() and events2.stat().st_size > 0,
                                 "uninstalled plugin wrote events")
                self.assertTrue(events_file.exists(), "historical events file must be preserved")
            finally:
                proc2.terminate()
                try:
                    proc2.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc2.kill()


if __name__ == "__main__":
    unittest.main()