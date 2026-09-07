"""受 ASG Supervisor 限制的 Runtime Analyst MCP 工具。

这里不是 Agent Loop：Goose 负责模型循环、工具选择、停止和输出；本文件只
提供窄权限、可审计、绑定单个目标 PID 的观测工具。所有结果先脱敏再返回。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parents[1]
AUDIT_DIR = Path(os.environ.get("ASG_AUDIT_DIR", ROOT / "e2e" / "artifacts"))
EVIDENCE_DIR = AUDIT_DIR / "evidence"
RECIPE_DIR = Path(os.environ.get("ASG_RECIPE_DIR", AUDIT_DIR / "recipes"))
TARGET_STREAM = Path(os.environ.get("ASG_TARGET_STREAM_FILE", "")) if os.environ.get("ASG_TARGET_STREAM_FILE") else None
TARGET_PID = int(os.environ.get("ASG_TARGET_PID", "0") or 0)
MAX_OUTPUT = 6000

SECRET_RE = re.compile(r"(?i)(authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|cookie|password|secret|private[_-]?key|bearer)\s*[:=]\s*[^\s,;]+")
TOKEN_RE = re.compile(r"(?i)\b(?:sk|rk|ghp|xoxb|xoxp)-[A-Za-z0-9._-]+\b")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): ("[REDACTED]" if re.search(r"(?i)(key|token|secret|password|cookie|authorization)", str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return TOKEN_RE.sub("[REDACTED]", SECRET_RE.sub(lambda m: m.group(1) + "=[REDACTED]", value))
    return value


def record(method: str, request_id: Any, tool: str | None, args: Any, result: Any, error: str | None = None) -> None:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    safe_result = redact(result)
    evidence_id = f"ev-{int(time.time() * 1000)}-{hashlib.sha1(json.dumps(safe_result, sort_keys=True, default=str).encode()).hexdigest()[:10]}"
    evidence = {"evidence_id": evidence_id, "ts": now(), "method": method, "request_id": request_id, "tool": tool, "args": redact(args), "result": safe_result, "error": error}
    (EVIDENCE_DIR / f"{evidence_id}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    with (AUDIT_DIR / "analyst_tool_calls.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "request_id": request_id, "tool": tool, "args": redact(args), "evidence_id": evidence_id, "error": error}, ensure_ascii=False) + "\n")


def target_process() -> psutil.Process:
    if TARGET_PID <= 0:
        raise RuntimeError("target PID is not bound")
    try:
        return psutil.Process(TARGET_PID)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        raise RuntimeError(f"target PID unavailable: {TARGET_PID}") from exc


def safe_text(text: str, limit: int = MAX_OUTPUT) -> str:
    return redact(text[-limit:])


def process_row(p: psutil.Process) -> dict[str, Any]:
    try:
        cmd = p.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        cmd = []
    try:
        cwd = p.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        cwd = ""
    try:
        children = len(p.children(recursive=False))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = 0
    # Blind Analyst view: identity-bearing process fields never cross the
    # Supervisor boundary. Raw OS evidence remains in the Sensor audit stream.
    argv_raw = [(str(x) if str(x).startswith("-") else "<value>") for x in cmd]
    config_candidates = []
    for i, arg in enumerate(cmd):
        arg_s = str(arg)
        if any(ext in arg_s.lower() for ext in [".json", ".yaml", ".yml", ".toml", ".ini", ".env", "config", "settings"]):
            # Generic pattern: find args that resemble config file/dir paths or flags
            config_candidates.append({
                "source": "cmdline_arg",
                "flag_context": cmd[i-1] if i > 0 and cmd[i-1].startswith("-") else "<positional>",
                "filename_class": Path(arg_s).name if ("." in arg_s or "/" in arg_s or "\\" in arg_s) else "<flag_val>",
                "is_file_like": any(arg_s.lower().endswith(e) for e in [".json", ".yaml", ".yml", ".toml", ".ini", ".env"])
            })
    return {
        "pid": p.pid,
        "ppid_present": p.ppid() > 0,
        "identity": "unknown-runtime",
        "argv_shape": argv_raw[:15],
        "config_candidates_in_argv": config_candidates,
        "cwd_class": "local-workspace" if cwd else "unavailable",
        "status": p.status(),
        "children": children,
        "create_time": p.create_time(),
    }


def tree_rows(root: psutil.Process) -> list[dict[str, Any]]:
    rows = []
    queue: list[tuple[psutil.Process, int]] = [(root, 0)]
    seen: set[int] = set()
    while queue:
        p, depth = queue.pop(0)
        if p.pid in seen or depth > 4:
            continue
        seen.add(p.pid)
        try:
            row = process_row(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        row["depth"] = depth
        rows.append(row)
        try:
            queue.extend((c, depth + 1) for c in p.children(recursive=False))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return rows


def event_shape(item: dict[str, Any]) -> dict[str, Any]:
    shape: dict[str, Any] = {"keys": sorted(item.keys()), "type": item.get("type")}
    if item.get("type") == "result":
        shape["subtype"] = item.get("subtype")
    message = item.get("message")
    if isinstance(message, dict):
        shape["message_role"] = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            blocks = [x for x in content if isinstance(x, dict)]
            shape["content_block_types"] = [x.get("type") for x in blocks]
            shape["has_text"] = any(x.get("type") == "text" for x in blocks)
            shape["has_tool_call"] = any(x.get("type") in {"tool_use", "toolRequest", "tool_call"} for x in blocks)
    return shape


def stream_tail() -> dict[str, Any]:
    if not TARGET_STREAM:
        return {"available": False, "reason": "supervisor did not expose a stream file"}
    try:
        raw = TARGET_STREAM.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"available": False, "path_class": "supervisor-exposed-stream", "reason": type(exc).__name__}
    lines = [x for x in raw.splitlines() if x.strip()]
    parsed = []
    for line in lines[-30:]:
        try:
            item = json.loads(line)
            if isinstance(item, dict):
                parsed.append(event_shape(item))
        except json.JSONDecodeError:
            pass
    return {"available": True, "path_class": "supervisor-exposed-stream", "line_count": len(lines), "json_line_count": len(parsed), "samples": parsed[-10:]}


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "inspect_stream":
        return stream_tail()
    if name == "get_prior_recipe":
        return load_prior()
    if name == "propose_recipe":
        recipe = redact(args.get("recipe", {}))
        if not isinstance(recipe, dict):
            raise ValueError("recipe must be an object")
        required = {"match_features", "observation", "hook", "fallback"}
        missing = sorted(required - set(recipe))
        if missing:
            raise ValueError("recipe missing fields: " + ", ".join(missing))
        RECIPE_DIR.mkdir(parents=True, exist_ok=True)
        path = RECIPE_DIR / "candidate.json"
        payload = {"status": "candidate", "created_at": now(), "recipe": recipe}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"candidate_path": str(path), "recipe": recipe, "next": "Supervisor must verify and commit; Analyst cannot activate hooks."}
    p = target_process()
    if name == "get_target_context":
        return {"target": process_row(p), "tree": tree_rows(p), "stream": stream_tail(), "prior_memory": load_prior()}
    if name == "observe_tree":
        return {"tree": tree_rows(p)}
    if name == "observe_runtime_surface":
        row = process_row(p)
        children = tree_rows(p)[1:]
        runtime = "unknown-runtime"
        open_paths = []
        try:
            for f in p.open_files():
                path = Path(str(f.path))
                open_paths.append({"extension": path.suffix.lower() or "<none>", "depth": len(path.parts), "is_config_like": path.suffix.lower() in {".json", ".yaml", ".yml", ".toml", ".ini", ".env"}})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        conns = []
        try:
            for c in p.net_connections(kind="inet"):
                conns.append({"status": c.status, "family": str(c.family), "type": str(c.type), "has_remote": bool(c.raddr)})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        return {"runtime_class": runtime, "target": row, "descendant_count": len(children), "child_count": len(children), "open_path_shapes": open_paths[:80], "network_shape": conns[:80], "stream": stream_tail()}
    if name == "probe_help":
        exe = str(p.exe())
        cwd = None
        try:
            cwd = p.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        attempts = []
        for flag in ("--help", "--version"):
            try:
                cp = subprocess.run([exe, flag], cwd=cwd, capture_output=True, text=True, timeout=5, shell=False)
                out = cp.stdout or ""
                err = cp.stderr or ""
                attempts.append({
                    "flag_class": flag,
                    "returncode": cp.returncode,
                    "stdout_shape": {"line_count": len(out.splitlines()), "has_json": "{" in out, "has_stream": "stream" in out.lower(), "has_tool": "tool" in out.lower()},
                    "stderr_shape": {"line_count": len(err.splitlines())},
                })
            except (OSError, subprocess.TimeoutExpired) as exc:
                attempts.append({"flag_class": flag, "error_class": type(exc).__name__})
        return {"identity": "unknown-runtime", "attempts": attempts}
    if name == "inspect_stream":
        return stream_tail()
    if name == "get_prior_recipe":
        return load_prior()
    if name == "propose_recipe":
        recipe = redact(args.get("recipe", {}))
        if not isinstance(recipe, dict):
            raise ValueError("recipe must be an object")
        required = {"match_features", "observation", "hook", "fallback"}
        missing = sorted(required - set(recipe))
        if missing:
            raise ValueError("recipe missing fields: " + ", ".join(missing))
        RECIPE_DIR.mkdir(parents=True, exist_ok=True)
        path = RECIPE_DIR / "candidate.json"
        payload = {"status": "candidate", "created_at": now(), "recipe": recipe}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"candidate_path": str(path), "recipe": recipe, "next": "Supervisor must verify and commit; Analyst cannot activate hooks."}
    raise ValueError(f"tool not allowlisted: {name}")


def load_prior() -> dict[str, Any]:
    candidates = [RECIPE_DIR / "committed.json", ROOT / "runtime" / "fingerprints.json"]
    for path in candidates:
        if path.exists():
            try:
                return {"path": str(path), "value": json.loads(path.read_text(encoding="utf-8"))}
            except (OSError, json.JSONDecodeError):
                continue
    return {"empty": True}


TOOLS = [
    {"name": "get_target_context", "description": "Read the supervisor-bound target dossier, process tree, stream shape, and prior memory.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "observe_tree", "description": "Observe only the target process and descendants within the supervisor scope.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "observe_runtime_surface", "description": "Inspect generic runtime, files, network shape, children, and stream capabilities; no secrets are returned.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "probe_help", "description": "Run only --help and --version against the observed executable, without a shell or arbitrary arguments.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_stream", "description": "Inspect a supervisor-exposed output stream and return structure samples after redaction.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_prior_recipe", "description": "Read prior committed generic memory for candidate validation.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "propose_recipe", "description": "Write a typed candidate recipe for Supervisor verification; cannot activate hooks or execute commands.", "inputSchema": {"type": "object", "required": ["recipe"], "properties": {"recipe": {"type": "object"}}}},
]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = req.get("method")
        request_id = req.get("id")
        try:
            if method == "initialize":
                result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "asg-runtime-tools", "version": "0.1.0"}}
                send({"jsonrpc": "2.0", "id": request_id, "result": result})
            elif method == "notifications/initialized":
                continue
            elif method == "tools/list":
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
            elif method == "tools/call":
                params = req.get("params") or {}
                name = params.get("name")
                args = params.get("arguments") or {}
                result = call_tool(name, args)
                record(method, request_id, name, args, result)
                text = json.dumps(redact(result), ensure_ascii=False)
                send({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}], "isError": False}})
            elif request_id is not None:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"unsupported method: {method}"}})
        except Exception as exc:
            record(method or "unknown", request_id, (req.get("params") or {}).get("name"), (req.get("params") or {}).get("arguments"), {}, error=f"{type(exc).__name__}: {exc}")
            if request_id is not None:
                send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32000, "message": str(exc)}})


if __name__ == "__main__":
    main()
