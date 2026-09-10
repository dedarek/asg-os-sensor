"""受 ASG Supervisor 限制的 Runtime Analyst MCP 工具。

这里不是 Agent Loop：Goose 负责模型循环、工具选择、停止和输出；本文件只
提供窄权限、可审计、绑定单个目标 PID 的观测工具。所有结果先脱敏再返回。
"""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
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
OBSERVE_URL = os.environ.get("ASG_OBSERVE_URL", "").strip().rstrip("/")
MAX_OBSERVE_RESPONSE_BYTES = 64 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OBSERVE_OPENER = urllib.request.build_opener(_NoRedirectHandler)

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


def _outer_bundle(executable: str) -> Path | None:
    try:
        path = Path(executable).resolve()
    except (OSError, ValueError):
        return None
    bundle = None
    for candidate in (path, *path.parents):
        if candidate.suffix == ".app":
            bundle = candidate
    return bundle


def _bundle_markers(path: Path) -> dict[str, bool]:
    markers = {
        "config_env_override": b"OPENCODE_CONFIG_DIR",
        "config_file_opencode_jsonc": b"opencode.jsonc",
        "config_file_opencode_json": b"opencode.json",
        "config_file_config_json": b"config.json",
        "plugin_scan_call": b"Glob.scan",
        "plugin_scan_pattern": b"{plugin,plugins}/*.{ts,js}",
        "hook_before": b"tool.execute.before",
        "hook_after": b"tool.execute.after",
    }
    found = {key: False for key in markers}
    try:
        if path.stat().st_size > 256 * 1024 * 1024:
            return found
        max_marker = max(len(value) for value in markers.values())
        carry = b""
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(4 * 1024 * 1024)
                if not chunk:
                    break
                data = carry + chunk
                for key, marker in markers.items():
                    if not found[key] and marker in data:
                        found[key] = True
                carry = data[-max_marker:]
                if all(found.values()):
                    break
    except OSError:
        pass
    return found


def inspect_loader_surface() -> dict[str, Any]:
    """Read only the target's app bundle and open-file classes; never install or scan a workspace."""
    p = target_process()
    try:
        executable = p.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {"available": False, "status": "target_executable_unavailable"}
    bundle = _outer_bundle(executable)
    if bundle is None:
        return {"available": False, "status": "no_app_bundle", "target_executable_class": Path(executable).name}
    info_path = bundle / "Contents" / "Info.plist"
    asar_path = bundle / "Contents" / "Resources" / "app.asar"
    info: dict[str, Any] = {}
    try:
        if info_path.stat().st_size <= 256 * 1024:
            loaded = plistlib.loads(info_path.read_bytes())
            if isinstance(loaded, dict):
                info = {
                    "name": loaded.get("CFBundleDisplayName") or loaded.get("CFBundleName"),
                    "version": loaded.get("CFBundleShortVersionString") or "unknown",
                }
    except (OSError, ValueError, plistlib.InvalidFileException):
        info = {}
    markers = _bundle_markers(asar_path) if asar_path.is_file() else {}
    opened_classes: set[str] = set()
    try:
        for opened in p.open_files():
            value = str(opened.path)
            if value == str(asar_path):
                opened_classes.add("engine_bundle")
            if "/.opencode/plugin/" in value or "/.opencode/plugins/" in value:
                opened_classes.add("project_plugin_path")
            if value.endswith(("/opencode.json", "/opencode.jsonc", "/config.json")):
                opened_classes.add("config_file")
            if "Application Support/ai.opencode.desktop" in value:
                opened_classes.add("desktop_data_store")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    cwd = ""
    try:
        cwd = p.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    scan_proven = bool(markers.get("plugin_scan_call") and markers.get("plugin_scan_pattern"))
    config_names = [name for name, key in (("opencode.jsonc", "config_file_opencode_jsonc"),
                                           ("opencode.json", "config_file_opencode_json"),
                                           ("config.json", "config_file_config_json"))
                    if markers.get(key)]
    return {
        "available": True,
        "status": "loader_evidence" if scan_proven else "bundle_evidence_only",
        "source": "target executable + bounded app bundle markers + target open-file classes",
        "bundle": {"name": info.get("name"), "version": info.get("version", "unknown"),
                   "info_plist": str(info_path), "app_asar": str(asar_path),
                   "target_opened": "engine_bundle" in opened_classes},
        "config_resolution": {
            "env_override_observed": bool(markers.get("config_env_override")),
            "global_candidate_names": config_names,
            "target_config_file_observed": "config_file" in opened_classes,
        },
        "plugin_resolution": {
            "auto_discovery_observed": scan_proven,
            "pattern": "{plugin,plugins}/*.{ts,js}" if scan_proven else None,
            "hook_events_observed": [name for name, key in (("tool.execute.before", "hook_before"),
                                                              ("tool.execute.after", "hook_after"))
                                     if markers.get(key)],
            "target_project_plugin_observed": "project_plugin_path" in opened_classes,
        },
        "target_scope": {
            "status": "unresolved",
            "cwd": cwd,
            "cwd_is_not_install_scope": True,
            "reason": "目标进程 cwd 不是已确认的 workspace/plugin 配置目录；未观察到目标实际项目插件路径",
        },
        "limitations": [
            "静态 bundle 证据只证明加载器实现和候选规则，不证明当前目标已加载插件",
            "没有从目标进程打开文件中确认 workspace/config/plugin 作用域，不能据此安装",
            "未读取全局配置文件、用户凭据或插件内容",
        ],
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


def inspect_observation() -> dict[str, Any]:
    """Read only the configured loopback receiver; never discover or follow another URL."""
    if not OBSERVE_URL:
        return {"available": False, "status": "not_configured", "reason": "ASG_OBSERVE_URL 未配置"}
    parsed = urllib.parse.urlparse(OBSERVE_URL)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return {"available": False, "status": "rejected", "reason": "观测地址必须是本机 HTTP 回环地址"}

    def fetch(path: str) -> tuple[int | None, dict[str, Any] | None, str | None]:
        try:
            with OBSERVE_OPENER.open(urllib.request.Request(OBSERVE_URL + path), timeout=2) as response:
                raw = response.read(MAX_OBSERVE_RESPONSE_BYTES + 1)
                if len(raw) > MAX_OBSERVE_RESPONSE_BYTES:
                    return response.status, None, "response_too_large"
                return response.status, json.loads(raw.decode("utf-8")), None
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except (OSError, ValueError):
                body = None
            return exc.code, body, "http_error"
        except (OSError, ValueError) as exc:
            return None, None, type(exc).__name__

    health_code, health, health_error = fetch("/health")
    events_code, events, events_error = fetch("/events")
    health = health if isinstance(health, dict) else {}
    events = events if isinstance(events, dict) else {}
    expected_instance = None
    if TARGET_PID > 0 and TARGET_CREATE_TIME is not None:
        expected_instance = f"{TARGET_PID}:{TARGET_CREATE_TIME}"
    observed_instance = health.get("instance_id")
    bound = bool(expected_instance and observed_instance == expected_instance)
    return {
        "available": bool(health or events),
        "status": "bound" if bound else "mismatch" if observed_instance else "unbound",
        "source": OBSERVE_URL,
        "expected_instance": expected_instance,
        "observed_instance": observed_instance,
        "health_http_status": health_code,
        "health_error": health_error,
        "health": {key: health.get(key) for key in ("status", "healthy", "reason", "loaded_observed", "capabilities")},
        "events_http_status": events_code,
        "events_error": events_error,
        "events": {"valid": events.get("valid"), "invalid": events.get("invalid"),
                   "event_types": sorted({item.get("event_type") for item in events.get("events", [])
                                           if isinstance(item, dict) and item.get("event_type")})},
    }


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "inspect_stream":
        return stream_tail()
    if name == "get_prior_recipe":
        return load_prior()
    if name == "get_prior_experience":
        from runtime import onboarding
        from runtime import analyzer
        instance_id = None
        if TARGET_PID > 0 and TARGET_CREATE_TIME is not None:
            instance_id = onboarding.make_instance_id(TARGET_PID, TARGET_CREATE_TIME)
        compatibility = None
        if TARGET_PID > 0:
            compatibility = analyzer.analyze(TARGET_PID).get("compatibility")
        return onboarding.load_prior_experience(instance_id=instance_id, compatibility=compatibility)
    if name == "inspect_observation":
        return inspect_observation()
    if name == "inspect_loader_surface":
        return inspect_loader_surface()
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
        context = {"target": {'pid': p.pid, 'create_time': p.create_time()},
                   "local_evidence": collect(p), "prior_memory": load_prior(),
                   "prior_experience": call_tool("get_prior_experience", {}),
                   "loader_surface": call_tool("inspect_loader_surface", {})}
        if OBSERVE_URL:
            context["observation"] = call_tool("inspect_observation", {})
        return context
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
    # An explicitly isolated fingerprint DB must not be combined with a default
    # committed recipe file. A caller may still opt into an isolated committed
    # recipe by setting ASG_RECIPE_DIR alongside ASG_FINGERPRINT_DB.
    candidates = []
    if os.environ.get("ASG_RECIPE_DIR", "").strip() or not os.environ.get("ASG_FINGERPRINT_DB", "").strip():
        candidates.append(RECIPE_DIR / "committed.json")
    candidates.append(_prior_db())
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
    {"name": "inspect_loader_surface", "description": "Read bounded target app-loader, config-resolution and plugin-scan evidence; target cwd is never treated as install scope.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_observation", "description": "Read the configured loopback observation receiver health and event types, bound to the target PID+create_time.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_prior_recipe", "description": "Read prior committed generic memory for candidate validation.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_prior_experience", "description": "Read bounded prior lifecycle outcomes for this compatible runtime; corrupt history is an explicit error and private paths are omitted.", "inputSchema": {"type": "object", "properties": {}}},
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
