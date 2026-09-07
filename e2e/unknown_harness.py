"""Deterministic anonymous target used only to exercise OS discovery.

The target intentionally exposes generic process/runtime behavior: parameterized
startup, a child process, and JSONL events. It contains no product identity and
is replaceable at launch time by any external harness command.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def emit(path: Path, event: dict) -> None:
    line = json.dumps(event, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    print(line, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="runtime-model")
    ap.add_argument("--output-format", default="jsonl")
    ap.add_argument("--system-prompt", default="runtime-system")
    ap.add_argument("--duration", type=float, default=18.0)
    ap.add_argument("--stream", default=os.environ.get("ASG_TARGET_STREAM_FILE", ""))
    args = ap.parse_args()
    stream = Path(args.stream or (Path.cwd() / "target-stream.jsonl"))
    stream.parent.mkdir(parents=True, exist_ok=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(12)"])
    try:
        emit(stream, {"type": "system", "runtime": "structured-stream", "session": "anonymous"})
        emit(stream, {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "Inspect the workspace and report the result."}]}})
        time.sleep(0.35)
        emit(stream, {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "I will inspect the workspace."}, {"type": "tool_use", "name": "list_workspace", "input": {"path": "."}}]}})
        time.sleep(0.35)
        emit(stream, {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "note.txt"}]}})
        time.sleep(0.35)
        emit(stream, {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "Workspace inspection completed."}]}})
        emit(stream, {"type": "result", "subtype": "success"})
        time.sleep(max(0.0, args.duration - 1.1))
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                child.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
