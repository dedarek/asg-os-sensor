"""Anthropic -> OpenAI-compatible 透明适配桥 (llm.yaml 可配路由).
只负责：接收 Anthropic /v1/messages，转给当前路由 chat/completions，返回 Anthropic SSE 流。
路由与 key 来自 llm.yaml + 环境变量/.env，默认 custom-openai。
"""
import json
import os
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runtime.llm_config import analyst_key
from runtime.llm_config import analyst_route
try:
    import requests
except ImportError:
    requests = None
def _bridge_target():
    route = analyst_route()
    base = str(route.get("base_url", "")).rstrip("/")
    path = str(route.get("chat_path", "/chat/completions"))
    return route, base + path, str(route.get("model", ""))

class BridgeHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)
        try:
            req = json.loads(body_bytes.decode("utf-8"))
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        # 转换 Anthropic messages 到 OpenAI chat completions
        messages = []
        if req.get("system"):
            sys_content = req["system"]
            if isinstance(sys_content, list):
                sys_text = "\n".join(b.get("text", "") for b in sys_content if isinstance(b, dict))
            else:
                sys_text = str(sys_content)
            messages.append({"role": "system", "content": sys_text})

        for m in req.get("messages", []):
            role = m.get("role")
            content = m.get("content")
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
            elif isinstance(content, list):
                text_parts = []
                for b in content:
                    if isinstance(b, dict):
                        if b.get("type") == "text":
                            text_parts.append(b.get("text", ""))
                        elif b.get("type") == "tool_result":
                            text_parts.append(f"[Tool Result]: {b.get('content', '')}")
                messages.append({"role": role, "content": "\n".join(text_parts)})

        stream = req.get("stream", False)
        route, target_url, target_model = _bridge_target()
        key = analyst_key(route)
        if not key:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(("missing credential " + str(route.get("key_env")) + " for route " + str(route.get("route"))).encode("utf-8"))
            return
        if requests is None:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"missing dependency: requests (pip install -r requirements.txt)")
            return
        openai_payload = {
            "model": target_model,
            "messages": messages,
            "stream": stream,
            "max_tokens": req.get("max_tokens", 4096)
        }
        headers = {
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        try:
            upstream = requests.post(target_url, headers=headers, json=openai_payload, stream=stream, timeout=60)
            if upstream.status_code != 200:
                self.send_response(upstream.status_code)
                self.end_headers()
                self.wfile.write(upstream.content)
                return

            if stream:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                # 发送 message_start
                msg_id = f"msg_{int(time.time()*1000)}"
                start_ev = {
                    "type": "message_start",
                    "message": {
                        "id": msg_id,
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-3-5-sonnet-20241022",
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 10, "output_tokens": 0}
                    }
                }
                self.wfile.write(f"event: message_start\ndata: {json.dumps(start_ev)}\n\n".encode("utf-8"))
                
                # 发送 content_block_start
                cb_start = {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""}
                }
                self.wfile.write(f"event: content_block_start\ndata: {json.dumps(cb_start)}\n\n".encode("utf-8"))

                for line in upstream.iter_lines():
                    if not line:
                        continue
                    line_str = line.decode("utf-8")
                    if line_str.startswith("data: "):
                        data_part = line_str[6:].strip()
                        if data_part == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_part)
                            delta = chunk["choices"][0].get("delta", {})
                            content_delta = delta.get("content", "")
                            if content_delta:
                                ev = {
                                    "type": "content_block_delta",
                                    "index": 0,
                                    "delta": {"type": "text_delta", "text": content_delta}
                                }
                                self.wfile.write(f"event: content_block_delta\ndata: {json.dumps(ev)}\n\n".encode("utf-8"))
                                self.wfile.flush()
                        except Exception:
                            pass

                # 发送 content_block_stop 和 message_delta
                cb_stop = {"type": "content_block_stop", "index": 0}
                self.wfile.write(f"event: content_block_stop\ndata: {json.dumps(cb_stop)}\n\n".encode("utf-8"))

                msg_delta = {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 50}
                }
                self.wfile.write(f"event: message_delta\ndata: {json.dumps(msg_delta)}\n\n".encode("utf-8"))
                
                msg_stop = {"type": "message_stop"}
                self.wfile.write(f"event: message_stop\ndata: {json.dumps(msg_stop)}\n\n".encode("utf-8"))
                self.wfile.flush()
            else:
                resp_json = upstream.json()
                content_text = resp_json["choices"][0]["message"].get("content", "")
                anthropic_resp = {
                    "id": f"msg_{int(time.time()*1000)}",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-3-5-sonnet-20241022",
                    "content": [{"type": "text", "text": content_text}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 50}
                }
                resp_bytes = json.dumps(anthropic_resp).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp_bytes)))
                self.end_headers()
                self.wfile.write(resp_bytes)
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(str(e).encode("utf-8"))

    def log_message(self, format, *args):
        pass

def run():
    host = os.environ.get("ASG_BRIDGE_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(str(os.environ.get("ASG_BRIDGE_PORT", "8765")))
    except Exception:
        port = 8765
    server = HTTPServer((host, port), BridgeHandler)
    print(f"Bridge listening on http://{host}:{port}", flush=True)
    server.serve_forever()

if __name__ == "__main__":
    run()
