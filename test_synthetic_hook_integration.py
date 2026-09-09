# -*- coding: utf-8 -*-
"""合成 SDK 钩子集成测试（不是真实 Agent B 阶段验收，不虚构安装器/握手完成）。

可验证的真实机制：
- 真实 python 目标进程 + 真实 PYTHONPATH 注入安装 e2e/hook/sitecustomize.py；
- 真实事件文件写盘（llm.request/llm.response 成对，adapter_source=auto-runtime-v2）；
- 真实本地回环 stub 端点（HTTP）收到 2 次请求（安装后 1 次 + 卸载后 1 次）；
- 卸载：移除 sitecustomize 路径后同一调用不再写事件，但第二次 HTTP 请求仍成功
  （returncode=0），证明"无事件"不是因调用失败而假通过。

合成元素（明确标注，不冒充真实 Agent）：fake openai 包（仅
chat.completions.Completions.create 一个方法）在 temp 目录动态生成，用于激活
sitecustomize 的包裹面；本机无 openai/litellm 且离线装不上。未验证 OpenCode
引擎/配置/插件加载，未安装任何东西到用户工作实例。

隔离边界：所有产物在临时目录；socket 在 finally 中关闭；不触碰用户全局配置、
不重启真实工作实例（OpenCode 45780 原样保留）。
"""
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SITE = ROOT / "e2e" / "hook" / "sitecustomize.py"


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def build_fake_sdk(root: Path) -> Path:
    pkg = root / "fake-openai"
    (pkg / "openai" / "resources" / "chat").mkdir(parents=True, exist_ok=True)
    (pkg / "openai" / "resources" / "chat" / "__init__.py").write_text(
        "from openai.resources.chat.completions import Completions" + chr(10))
    (pkg / "openai" / "resources" / "chat" / "completions.py").write_text(
        "class Completions:\n"
        "    @staticmethod\n"
        "    def create(**kwargs):\n"
        "        import json, urllib.request\n"
        "        url = kwargs.get('api_base','').rstrip('/') + '/chat/completions'\n"
        "        body = json.dumps({'model': kwargs.get('model'), 'messages': kwargs.get('messages')}).encode()\n"
        "        req = urllib.request.Request(url, data=body, headers={'Content-Type':'application/json', 'Authorization':'Bearer '+str(kwargs.get('api_key',''))}, method='POST')\n"
        "        with urllib.request.urlopen(req, timeout=10) as r:\n"
        "            return json.loads(r.read().decode())\n")
    (pkg / "openai" / "resources" / "__init__.py").write_text("")
    (pkg / "openai" / "__init__.py").write_text(
        "from openai.resources.chat.completions import Completions" + chr(10))
    return pkg


def stub_server(port):
    srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port)); srv.listen(5)
    reqs = []

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                reqs.append(data)
                body = json.dumps({"id": "chatcmpl-stub", "object": "chat.completion", "created": 0,
                                   "model": "synthetic-stub",
                                   "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong-from-stub"}}]}).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()
    return srv, reqs


def read_events(path):
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return rows


class SyntheticHookIntegrationTests(unittest.TestCase):
    def test_install_hook_events_uninstall_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nonce = secrets.token_hex(8)
            pkg = build_fake_sdk(root)
            port = free_port()
            srv, reqs = stub_server(port)
            try:
                events_file = root / f"events-{nonce}.jsonl"
                env = dict(os.environ,
                           PYTHONPATH=os.pathsep.join([str(SITE.parent), str(pkg)]) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                           ASG_ADAPTER_EVENTS=str(events_file))
                code = ("import openai.resources.chat.completions as cc; "
                        "r=cc.Completions.create(model='synthetic-stub', messages=[{'role':'user','content':'hi'}], "
                        "api_base='http://127.0.0.1:%d/v1', api_key='nokey'); print(r)") % port
                target = subprocess.run([sys.executable, "-B", "-c", code], env=env, cwd=str(root),
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(target.returncode, 0, target.stderr)
                rows = read_events(events_file)
                req_events = [r for r in rows if r.get("event_type") == "llm.request" and r.get("adapter_source") == "auto-runtime-v2"]
                resp_events = [r for r in rows if r.get("event_type") == "llm.response" and r.get("adapter_source") == "auto-runtime-v2"]
                self.assertTrue(req_events, "no before/request event captured")
                self.assertTrue(resp_events, "no after/response event captured")
                self.assertEqual(req_events[0].get("sdk"), "openai-chat")

                events2 = root / f"events-{nonce}-unloaded.jsonl"
                env2 = dict(os.environ, PYTHONPATH=str(pkg) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                            ASG_ADAPTER_EVENTS=str(events2))
                code2 = ("import openai.resources.chat.completions as cc; "
                         "cc.Completions.create(model='synthetic-stub', messages=[{'role':'user','content':'hi'}], "
                         "api_base='http://127.0.0.1:%d/v1', api_key='nokey')") % port
                target2 = subprocess.run([sys.executable, "-B", "-c", code2], env=env2, cwd=str(root),
                                         capture_output=True, text=True, timeout=30)
                self.assertEqual(target2.returncode, 0, target2.stderr)
                self.assertEqual(len(reqs), 2, "stub should receive 2 successful requests (installed + uninstalled)")
                self.assertFalse(events2.exists() and events2.stat().st_size > 0,
                                 "uninstalled hook still produced events")
                print("synthetic hook integration OK: request+response=%d/%d, stub_reqs=%d" %
                      (len(req_events), len(resp_events), len(reqs)))
            finally:
                srv.close()


if __name__ == "__main__":
    unittest.main()