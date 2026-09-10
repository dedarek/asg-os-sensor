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
# 扩展可能以脚本方式启动 (python runtime/analyst_tools.py):
# 显式把项目根加入导入路径, 保证 from runtime import matcher 可用。
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
AUDIT_DIR = Path(os.environ.get("ASG_AUDIT_DIR", ROOT / "e2e" / "artifacts"))
EVIDENCE_DIR = AUDIT_DIR / "evidence"
RECIPE_DIR = Path(os.environ.get("ASG_RECIPE_DIR", AUDIT_DIR / "recipes"))
TARGET_STREAM = Path(os.environ.get("ASG_TARGET_STREAM_FILE", "")) if os.environ.get("ASG_TARGET_STREAM_FILE") else None
TARGET_PID = int(os.environ.get("ASG_TARGET_PID", "0") or 0)
TARGET_CREATE_TIME = None
try:
    _ct = os.environ.get("ASG_TARGET_CREATE_TIME", "").strip()
    if _ct:
        TARGET_CREATE_TIME = float(_ct)
except (TypeError, ValueError):
    TARGET_CREATE_TIME = None
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


def record(method: str, request_id: Any, tool: str | None, args: Any, result: Any, error: str | None = None) -> str:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    safe_result = redact(result)
    evidence_id = f"ev-{time.time_ns()}-{hashlib.sha1(json.dumps(safe_result, sort_keys=True, default=str).encode()).hexdigest()[:10]}"
    # 证据必须绑定调查启动时冻结的目标实例 (pid + create_time)，
    # 供配方校验比对，杜绝跨实例/PID 复用混用历史证据。
    evidence = {"evidence_id": evidence_id, "ts": now(), "method": method, "request_id": request_id,
                "tool": tool, "args": redact(args), "result": safe_result, "error": error,
                "target": {"pid": TARGET_PID, "create_time": TARGET_CREATE_TIME}}
    (EVIDENCE_DIR / f"{evidence_id}.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    with (AUDIT_DIR / "analyst_tool_calls.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now(), "request_id": request_id, "tool": tool, "args": redact(args), "evidence_id": evidence_id, "error": error}, ensure_ascii=False) + "\n")

    return evidence_id

def target_process() -> psutil.Process:
    if TARGET_PID <= 0:
        raise RuntimeError("target PID is not bound")
    try:
        p = psutil.Process(TARGET_PID)
        expected = os.environ.get('ASG_TARGET_CREATE_TIME')
        if expected and p.create_time() != float(expected):
            raise RuntimeError('target PID reused')
        return p
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
    # 保留父进程血缘链 (脱敏，仅保留进程 basename，供 Analyst 推断宿主 Harness/IDE/平台)
    parent_lineage = []
    try:
        curr = p.parent()
        while curr:
            pname = curr.name()
            if pname.lower() not in ["explorer.exe", "svchost.exe", "services.exe", "init", "system"]:
                parent_lineage.append(pname)
            curr = curr.parent()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    # Blind Analyst view: identity-bearing process fields are abstracted, but package tokens & script names are retained for identity discovery
    argv_raw = []
    for x in cmd:
        xs = str(x)
        if xs.startswith("-"):
            argv_raw.append(xs)
        elif any(ext in xs.lower() for ext in [".js", ".py", ".ts", ".sh", ".mjs", "bundle", "cli", "agent"]):
            # 保留执行脚本/包的语义名，便于 Analyst 推断 Agent 身份
            p_obj = Path(xs)
            p_token = p_obj.name
            # 如果路径中含有具体组件/插件目录名（非通用目录如 node_modules/plugins/cache/mcp），提取其语义包名
            parts = p_obj.parts
            pkg_name = ""
            for idx, part in enumerate(parts):
                part_l = part.lower()
                if any(k in part_l for k in ["agent", "tool", "plugin", "skill"]) and part_l not in ["plugins", "cache", "mcp"]:
                    pkg_name = part
                    break
            if pkg_name and pkg_name != p_token:
                p_token = f"{pkg_name}/{p_token}"
            argv_raw.append(p_token)
        else:
            argv_raw.append("<value>")
    config_candidates = []
    for i, arg in enumerate(cmd):
        arg_s = str(arg)
        if any(ext in arg_s.lower() for ext in [".json", ".yaml", ".yml", ".toml", ".ini", ".env", "config", "settings"]):
            # Generic pattern: find args that resemble config file/dir paths or flags
            config_candidates.append({
                "source": "cmdline_arg",
                "flag_context": cmd[i-1] if i > 0 and cmd[i-1].startswith("-") else "<positional>",
                "filename_class": Path(arg_s).name if ("." in arg_s or "/" in arg_s or chr(92) in arg_s) else "<flag_val>",
                "is_file_like": any(arg_s.lower().endswith(e) for e in [".json", ".yaml", ".yml", ".toml", ".ini", ".env"])
            })
    return {
        "pid": p.pid,
        "ppid_present": p.ppid() > 0,
        "parent_lineage": parent_lineage[:4],
        "identity": "unknown-runtime",
        "argv_shape": argv_raw[:15],
        "config_candidates_in_argv": config_candidates,
        "cwd": cwd,
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
    if name == "get_prior_experience":
        from runtime import onboarding
        instance_id = None
        if TARGET_PID > 0 and TARGET_CREATE_TIME is not None:
            instance_id = onboarding.make_instance_id(TARGET_PID, TARGET_CREATE_TIME)
        return onboarding.load_prior_experience(instance_id=instance_id)
    if name == "propose_recipe":
        target_process()
        recipe = redact(args.get("recipe", {}))
        if not isinstance(recipe, dict):
            raise ValueError("recipe must be an object")
        required = {"match_features", "observation", "hook", "fallback"}
        missing = sorted(required - set(recipe))
        if missing:
            raise ValueError("recipe missing fields: " + ", ".join(missing))
        from runtime.recipe_validation import validate
        validate(recipe, EVIDENCE_DIR,
                 target={"pid": TARGET_PID, "create_time": TARGET_CREATE_TIME})
        RECIPE_DIR.mkdir(parents=True, exist_ok=True)
        path = RECIPE_DIR / "candidate.json"
        payload = {"status": "candidate", "created_at": now(), "recipe": recipe}
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {"candidate_path": str(path), "recipe": recipe, "next": "Supervisor must verify and commit; Analyst cannot activate hooks."}
    p = target_process()
    if name == 'inspect_config_surface':
        if args.get('path'):
            raise ValueError('Arbitrary config paths are not permitted; inspect bound process only')
        from runtime.collection import collect
        return collect(p)
    if name == 'inspect_network_peers':
        from runtime.collection import collect
        return collect(p)['assets']['network_surface']
    if name in ('observe_tree', 'inspect_execution_trace'):
        return {'processes': [{'pid': c.pid, 'name': c.name()} for c in p.children(recursive=True)[:40]],
                'execution_events': {'status': 'not_collected'}}
    if name == 'probe_help':
        return {'status': 'unsupported', 'reason': 'Executing the target binary may launch another application; disabled'}
    if name == "get_target_context":
        from runtime.collection import collect
        return {"target": {'pid': p.pid, 'create_time': p.create_time()},
                "local_evidence": collect(p), "prior_memory": load_prior(),
                "prior_experience": call_tool("get_prior_experience", {})}
    if name == "observe_runtime_surface":
        from runtime.collection import collect
        return {'local_evidence': collect(p), 'runtime': 'unknown',
                'children': [{'pid': c.pid, 'name': c.name()} for c in p.children()[:40]],
                'stream': stream_tail()}
    raise ValueError(f"tool not allowlisted: {name}")


def _prior_db() -> Path:
    """指纹库路径: 统一走 matcher.db_path()(ASG_FINGERPRINT_DB 可隔离)。

    子进程(goose 扩展)通过继承环境变量获得同一隔离配置, 避免读到另一份历史。
    一旦显式指定隔离库, 任何导入/读取错误都必须显式报错, 禁止静默回退默认库。
    """
    from runtime import matcher as _matcher
    return _matcher.db_path()


def load_prior() -> dict[str, Any]:
    candidates = [RECIPE_DIR / "committed.json", _prior_db()]
    for path in candidates:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        if not isinstance(value, dict):
            raise ValueError("Invalid prior: expected object")
        return {"path": str(path), "value": value}
    return {"empty": True}


TOOLS = [
    {"name": "get_target_context", "description": "Read the supervisor-bound target dossier, process tree, stream shape, and prior memory.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "observe_tree", "description": "Observe only the target process and descendants within the supervisor scope.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "observe_runtime_surface", "description": "Inspect generic runtime, files, network shape, children, and stream capabilities; no secrets are returned.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_config_surface", "description": "Read selected configuration names from bound target files. No raw contents, secrets, .env or arbitrary paths.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_network_peers", "description": "Inspect active local listening ports and remote model endpoint connections.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_execution_trace", "description": "Inspect sensitive files accessed and active child command lines spawned by the agent.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "probe_help", "description": "Run only --help and --version against the observed executable, without a shell or arbitrary arguments.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_stream", "description": "Inspect a supervisor-exposed output stream and return structure samples after redaction.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_prior_recipe", "description": "Read prior committed generic memory for candidate validation.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_prior_experience", "description": "Read bounded prior investigation/install/verification outcomes for this exact PID+create_time; corrupt history is an explicit error.", "inputSchema": {"type": "object", "properties": {}}},
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
                evidence_id = record(method, request_id, name, args, result)
                result = dict(result, evidence_id=evidence_id)
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
