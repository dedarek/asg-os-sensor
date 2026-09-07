"""通用 stream 解析器: harness 结构化输出(JSONL) -> 标准 adapter 事件.

不认任何 harness 私有字段, 只认通用键 (type/message/content/tool_use/tool_result).
adapter_source=auto-runtime-stream. stdin 进, 事件文件出. 失败静默(空输出).
"""
import json
import os
import sys
from datetime import datetime, timezone

SRC = "auto-runtime-stream"
OUT = os.environ.get("ASG_ADAPTER_EVENTS", "")


_SECRET = ("authorization", "api_key", "apikey", "access_token", "refresh_token", "cookie", "password", "secret")


def redact(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if any(x in str(k).lower() for x in _SECRET) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        import re
        value = re.sub(r"(?i)\b(?:sk|rk|ghp|xoxb|xoxp)-[A-Za-z0-9._-]+\b", "[REDACTED]", value)
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


def emit(ev):
    if not OUT:
        return
    try:
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass


def main():
    n_req = n_resp = 0
    for line in sys.stdin:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        t = e.get("type", "")
        if t == "system":
            emit({"ts": now(), "event_type": "llm.session",
                  "adapter_source": SRC, "detail": "session-init"})
        elif t == "assistant":
            blocks = []
            for b in (e.get("message", {}) or {}).get("content", []) or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_use":
                    call = {"tool": b.get("name"), "input": redact(b.get("input", {}))}
                    emit({"ts": now(), "event_type": "tool.call", "adapter_source": SRC, "call": call})
                    blocks.append(call)
                elif b.get("type") == "text":
                    blocks.append({"text": str(b.get("text") or "")[:2000]})
            if blocks:
                emit({"ts": now(), "event_type": "llm.response", "adapter_source": SRC, "blocks": blocks})
            n_resp += 1
        elif t == "user":
            texts = []
            results = []
            for b in (e.get("message", {}) or {}).get("content", []) or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    results.append(str(redact(b.get("content", "")))[:2000])
                elif b.get("type") == "text":
                    texts.append(str(b.get("text", ""))[:2000])
            if texts:
                emit({"ts": now(), "event_type": "llm.request", "adapter_source": SRC, "prompt": texts})
                n_req += 1
            if results:
                emit({"ts": now(), "event_type": "tool.result", "adapter_source": SRC, "results": results})
                n_resp += 1
        elif t == "result":
            emit({"ts": now(), "event_type": "llm.session",
                  "adapter_source": SRC,
                  "detail": f"session-end:{str(e.get('subtype', ''))[:50]}"})
        elif t in ("text", "content_block_delta", "message_delta"):
            # Generic assistant text emission
            emit({"ts": now(), "event_type": "llm.response", "adapter_source": SRC, "detail": "delta"})
    print(f"parsed resp={n_resp}", flush=True)


if __name__ == "__main__":
    main()
