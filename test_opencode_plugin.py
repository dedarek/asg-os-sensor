# -*- coding: utf-8 -*-
"""OpenCode 纯观测插件集成测试（隔离、可复现）。

- 插件模块真实加载（node fixture 动态导入 runtime/opencode/asg-observe.mjs），
  启动即写 hook.loaded；引擎长驻（fixture setTimeout 60s），事件写入后校验
  自报 pid 的实测 create_time（模拟接收端对引擎快照）。
- 事件仅含 nonce/pid/call_id/tool/outcome；无参数内容或密钥。
- 接收端绑定：nonce + PID+create_time 同时匹配才通过；错误 nonce、create_time
  不一致、pid 不存在均拒绝。
- 卸载：不注入事件文件/不设置非ce => 无事件。
"""
import json
import os
import secrets
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import psutil

from runtime.opencode.event_api import EventVerifier

ROOT = Path(__file__).resolve().parent
NODE = "/Users/mac/.nvm/versions/node/v24.16.0/bin/node"
FIXTURE = ROOT / "test_opencode_plugin.js"


def wait_for_events(path: Path, want: int, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            rows = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
            if len(rows) >= want:
                return rows
        time.sleep(0.1)
    return []


class OpenCodePluginTests(unittest.TestCase):
    def test_plugin_loads_events_bound_and_uninstall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nonce = secrets.token_hex(8)
            events_file = root / f"events-{nonce}.jsonl"
            env = dict(os.environ,
                       ASG_OBSERVE_EVENTS=str(events_file),
                       ASG_OBSERVE_NONCE=nonce,
                       ASG_OBSERVE_LOCK="1")
            proc = subprocess.Popen([NODE, FIXTURE], env=env, cwd=str(root),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                rows = wait_for_events(events_file, 3)
                self.assertTrue(rows, "no events written")
                loaded = [e for e in rows if e["event_type"] == "hook.loaded"]
                before = [e for e in rows if e["event_type"] == "tool.execute.before"]
                after = [e for e in rows if e["event_type"] == "tool.execute.after"]
                self.assertTrue(loaded and before and after, "loaded/before/after missing")
                blob = open(events_file).read()
                self.assertNotIn("TOPSECRET", blob)
                self.assertNotIn("ls -la", blob)
                for e in before + after:
                    self.assertEqual(e["call_id"], "call-abc-123")
                    self.assertEqual(e["tool"], "bash")
                    self.assertNotIn("args", e)
                    self.assertNotIn("output", e)
                ev_pid = loaded[0]["pid"]
                ev_ct = psutil.Process(ev_pid).create_time()
                verifier = EventVerifier(nonce, ev_pid, ev_ct)
                verifier.verify_instance(loaded[0])
                self.assertTrue(verifier.healthy(rows))
                with self.assertRaises(ValueError):
                    EventVerifier("wrong" + nonce, ev_pid, ev_ct).verify_instance(loaded[0])
                with self.assertRaises(ValueError):
                    EventVerifier(nonce, ev_pid, ev_ct + 1000.0).verify_instance(loaded[0])
                with self.assertRaises(ValueError):
                    EventVerifier(nonce, 999999, ev_ct).verify_instance(loaded[0])
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
            # 卸载：不设事件文件 => 无事件
            env2 = dict(os.environ, ASG_OBSERVE_NONCE=nonce)
            proc2 = subprocess.run([NODE, FIXTURE], env=env2, cwd=str(root),
                                   capture_output=True, text=True, timeout=30)
            self.assertEqual(proc2.returncode, 0, proc2.stderr)


if __name__ == "__main__":
    unittest.main()
