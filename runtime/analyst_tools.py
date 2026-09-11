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
from runtime.analyst_evidence import (  # noqa: E402
    entry_surface,
    find_related_files,
    metadata_candidates,
    read_related_file,
)
from runtime import investigation_findings
from runtime.recipe_schema import RECIPE_SCHEMA
AUDIT_DIR = Path(os.environ.get("ASG_AUDIT_DIR", ROOT / "e2e" / "artifacts"))
EVIDENCE_DIR = AUDIT_DIR / "evidence"
RECIPE_DIR = Path(os.environ.get("ASG_RECIPE_DIR", AUDIT_DIR / "recipes"))
FINDINGS_PATH = Path(os.environ.get("ASG_FINDINGS_FILE", AUDIT_DIR / "investigation_findings.json"))
RESUME_RUN_DIR = Path(os.environ["ASG_RESUME_FROM_RUN_DIR"]) if os.environ.get("ASG_RESUME_FROM_RUN_DIR") else None
RESUME_FINDINGS_FILE = Path(os.environ["ASG_RESUME_FINDINGS_FILE"]) if os.environ.get("ASG_RESUME_FINDINGS_FILE") else None
RESUME_LIFECYCLE_FILE = Path(os.environ["ASG_RESUME_LIFECYCLE_FILE"]) if os.environ.get("ASG_RESUME_LIFECYCLE_FILE") else None
RESUME_AUDIT_FILE = Path(os.environ["ASG_RESUME_AUDIT_FILE"]) if os.environ.get("ASG_RESUME_AUDIT_FILE") else None
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
        # A key/value record carries a field NAME, not an API credential.
        # Keep that label as field, but redact the paired value when the label
        # names a secret. Do not globally exempt the key field from redaction.
        if set(value) == {'key', 'value'} and isinstance(value['key'], str):
            label = value['key']
            secret = re.search(r"(?i)(key|token|secret|password|cookie|authorization)", label)
            return {'field': redact(label), 'value': '[REDACTED]' if secret else redact(value['value'])}
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
    """Find generic loader/extension markers without choosing a product."""
    markers = {
        "config_env_override": b"CONFIG_DIR",
        "config_file_name": b"config",
        "plugin_scan_call": b"Glob.scan",
        "plugin_scan_pattern": b"{plugin,plugins}",
        "hook_before": b".before",
        "hook_after": b".after",
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
    """Read generic bundle, loader and extension evidence; never install or scan a workspace."""
    p = target_process()
    try:
        executable = p.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {"available": False, "status": "target_executable_unavailable"}
    bundle = _outer_bundle(executable)
    if bundle is None:
        return {"available": False, "status": "no_app_bundle", "target_executable_class": Path(executable).name}
    info_path = bundle / "Contents" / "Info.plist"
    bundle_candidates = [
        bundle / "Contents" / "Resources" / "app.asar",
        bundle / "Contents" / "app.asar",
        bundle / "Contents" / "Resources" / "app.zip",
    ]
    package_path = next((candidate for candidate in bundle_candidates if candidate.is_file()), None)
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
    markers = _bundle_markers(package_path) if package_path else {}
    opened_classes: set[str] = set()
    try:
        for opened in p.open_files():
            value = str(opened.path)
            if package_path and value == str(package_path):
                opened_classes.add("engine_bundle")
            parts = {part.lower() for part in Path(value).parts}
            if "plugin" in parts or "plugins" in parts:
                opened_classes.add("project_plugin_path")
            if Path(value).suffix.lower() in {".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini"}:
                opened_classes.add("config_file")
            if "application support" in value.lower():
                opened_classes.add("desktop_data_store")
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    cwd = ""
    try:
        cwd = p.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    scan_proven = bool(markers.get("plugin_scan_call") and markers.get("plugin_scan_pattern"))
    config_names = ["config-like file"] if markers.get("config_file_name") else []
    return {
        "available": True,
        "status": "loader_evidence" if scan_proven else "bundle_evidence_only",
        "source": "target executable + bounded app bundle markers + target open-file classes",
        "bundle": {"name": info.get("name"), "version": info.get("version", "unknown"),
                   "info_plist": str(info_path), "bundle_package": str(package_path) if package_path else None,
                   "target_opened": "engine_bundle" in opened_classes},
        "config_resolution": {
            "env_override_observed": bool(markers.get("config_env_override")),
            "global_candidate_names": config_names,
            "target_config_file_observed": "config_file" in opened_classes,
        },
        "plugin_resolution": {
            "auto_discovery_observed": scan_proven,
            "pattern": "{plugin,plugins}/*.{text-module}" if scan_proven else None,
            "hook_events_observed": [name for name, key in (("before", "hook_before"),
                                                              ("after", "hook_after"))
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
            "没有从目标进程打开文件中确认扩展/配置作用域，不能据此安装",
            "未读取全局配置文件、用户凭据或扩展内容",
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


def _validate_investigation_summary(recipe: dict[str, Any]) -> None:
    """Require Goose to account for identity and the four requested asset groups."""
    summary = recipe.get("investigation")
    if not isinstance(summary, dict):
        raise ValueError("recipe investigation summary is required")
    identity = summary.get("identity_evidence") or summary.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("recipe investigation identity summary is required")
    identity_sources = identity.get("sources") or identity.get("evidence_refs")
    if not isinstance(identity_sources, list) or not identity_sources or not isinstance(identity.get("uncertainty", []), list):
        raise ValueError("investigation.identity_evidence must include sources")
    assets = summary.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("investigation.assets is required")
    required_assets = ("model_gateway", "mcp", "skills", "rules")
    allowed_status = {"collected", "empty", "failed", "unsupported", "unknown", "not_collected"}
    missing = [name for name in required_assets if not isinstance(assets.get(name), dict)]
    if missing:
        raise ValueError("investigation.assets missing: " + ", ".join(missing))
    for name in required_assets:
        item = assets[name]
        if item.get("status") not in allowed_status:
            raise ValueError("invalid investigation asset status: " + name)
        if not isinstance(item.get("sources", []), list) or not isinstance(item.get("uncertainty", []), list):
            raise ValueError("investigation asset sources must be a list: " + name)


_FINDING_EVIDENCE_TOOLS = {
    "get_target_context", "inspect_entry_surface", "find_related_files", "read_related_file",
    "inspect_config_surface", "inspect_loader_surface", "inspect_network_peers",
    "inspect_execution_trace", "inspect_stream", "inspect_observation", "observe_tree",
    "observe_runtime_surface", "search_target_image", "read_evidence",
}
_EVIDENCE_REF = re.compile(r"ev-[0-9]+-[a-f0-9]{10}")


def _validate_finding_evidence(refs: Any) -> list[str]:
    if not isinstance(refs, list) or not refs or len(refs) > 16:
        raise ValueError("finding evidence_refs must contain 1-16 references")
    result: list[str] = []
    expected = (TARGET_PID, TARGET_CREATE_TIME)
    for ref in refs:
        if not isinstance(ref, str) or not _EVIDENCE_REF.fullmatch(ref) or ref in result:
            raise ValueError("invalid finding evidence reference: " + str(ref))
        path = EVIDENCE_DIR / (ref + ".json")
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError("finding evidence is not in the current audit: " + ref) from exc
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError("finding evidence is unreadable: " + ref) from exc
        if not isinstance(item, dict) or item.get("error") or item.get("tool") not in _FINDING_EVIDENCE_TOOLS:
            raise ValueError("finding evidence is not a successful observation: " + ref)
        observed = item.get("result")
        if observed in (None, {}, [], ""):
            raise ValueError("finding evidence has no result: " + ref)
        bound = item.get("target") or {}
        if bound.get("pid") != expected[0] or bound.get("create_time") is None:
            raise ValueError("finding evidence is bound to another instance: " + ref)
        if expected[1] is not None and float(bound["create_time"]) != float(expected[1]):
            raise ValueError("finding evidence create_time does not match target: " + ref)
        result.append(ref)
    return result


def _submit_investigation_finding(args: dict[str, Any]) -> dict[str, Any]:
    kind = args.get("kind")
    evidence_refs = _validate_finding_evidence(args.get("evidence_refs"))
    uncertainty = args.get("uncertainty", [])
    open_questions = args.get("open_questions", [])
    if kind == "identity":
        status = args.get("status", "unknown")
        # The advertised schema and prompt allow identity.value. Preserve its
        # role conclusion instead of silently dropping it in the flattened API.
        supplied = args.get("value", {})
        if isinstance(supplied, str):
            supplied = json.loads(supplied)  # Some compatible providers stringify nested tool values.
        if not isinstance(supplied, dict):
            raise ValueError('identity.value must be an object or JSON-encoded object')
        value = {
            "name": str(args.get("name") or supplied.get("name") or "unidentified-agent")[:240],
            "runtime": str(args.get("runtime") or supplied.get("runtime") or "unknown")[:160],
            "entry": str(args.get("entry") or supplied.get("entry") or "unknown")[:400],
            "version": str(args.get("version") or supplied.get("version") or "unknown")[:160],
        }
        roles = args.get("roles", supplied.get("roles"))
        if roles is not None:
            value["roles"] = roles  # enum/bound validation happens in save_finding
        reasoning = args.get("role_reasoning", supplied.get("role_reasoning"))
        if reasoning is not None:
            value["role_reasoning"] = reasoning
        if isinstance(args.get("details"), dict):
            value["details"] = args["details"]
        finding = {"kind": kind, "status": status, "value": redact(value),
                   "evidence_refs": evidence_refs, "uncertainty": uncertainty,
                   "open_questions": open_questions}
    elif kind == "asset":
        asset = args.get("asset")
        status = args.get("status")
        value = args.get("value", args.get("summary"))
        finding = {"kind": kind, "asset": asset, "status": status, "value": redact(value),
                   "evidence_refs": evidence_refs, "uncertainty": uncertainty,
                   "open_questions": open_questions}
    else:
        raise ValueError("finding kind must be identity or asset")
    if os.environ.get('ASG_REQUIRE_STANDARD_DISPLAY') == '1' and args.get('display') is None:
        raise ValueError('display is required: {version:1, summary:Chinese conclusion, facts:[{label:string,value:string}], scope:Chinese scope}; preserve evidence_refs and uncertainty')
    if args.get('display') is not None:
        finding['display'] = redact(args['display'])
    saved = investigation_findings.save_finding(
        FINDINGS_PATH,
        {"pid": TARGET_PID, "create_time": TARGET_CREATE_TIME},
        finding,
    )
    current = saved["findings"]["identity"] if kind == "identity" else saved["findings"]["assets"][args.get("asset")]
    return {"status": "saved", "finding_path": str(FINDINGS_PATH), "finding": current,
            "open_questions": saved.get("open_questions", [])}


def _read_resume_json(path: Path | None, label: str) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        if not path.exists():
            return None
        if path.stat().st_size > 1024 * 1024:
            raise ValueError(label + " exceeds bounded resume size")
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(label + " is unreadable: " + type(exc).__name__) from exc
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    return value


def _saved_investigation() -> dict[str, Any]:
    if RESUME_RUN_DIR is None:
        return {"status": "not_requested", "reason": "no continuation source was supplied"}
    lifecycle = _read_resume_json(RESUME_LIFECYCLE_FILE, "previous lifecycle")
    saved = _read_resume_json(RESUME_FINDINGS_FILE, "previous findings")
    if lifecycle is None and saved is None:
        raise ValueError("continuation source has no lifecycle or findings")
    audit_rows: list[dict[str, Any]] = []
    if RESUME_AUDIT_FILE is not None and RESUME_AUDIT_FILE.exists():
        try:
            lines = RESUME_AUDIT_FILE.read_text(encoding="utf-8").splitlines()[-128:]
        except (OSError, UnicodeError) as exc:
            raise ValueError("previous audit is unreadable: " + type(exc).__name__) from exc
        for line in lines:
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict) and item.get("evidence_id"):
                audit_rows.append({"tool": item.get("tool"), "evidence_id": item.get("evidence_id"),
                                   "args": redact(item.get("args", {})), "error": bool(item.get("error"))})
    previous_result = _read_resume_json(RESUME_RUN_DIR / 'result.json', 'previous result') or {}
    execution_feedback = {'message': previous_result.get('message'),
                          'onboarding': previous_result.get('onboarding')}
    selected_lifecycle = {}
    if lifecycle:
        for key in ("status", "end_reason", "target", "tool_call_count", "elapsed_ms", "timed_out", "progress", "resume"):
            if key in lifecycle:
                selected_lifecycle[key] = lifecycle[key]
    return {
        "status": "available",
        "source": str(RESUME_RUN_DIR),
        "previous_lifecycle": selected_lifecycle,
        "execution_feedback": redact(execution_feedback),
        "findings": (saved or {}).get("findings", {"identity": None, "assets": {}}),
        "open_questions": (saved or {}).get("open_questions", []),
        "evidence_refs": audit_rows[-64:],
        "note": "Only the bounded summary and evidence ids are supplied; previous stdout is not replayed.",
    }


SEARCH_IMAGE_MAX_MIRROR_BYTES = 1024 * 1024 * 1024  # refuse whole-disk style mirrors politely
SEARCH_IMAGE_CONTEXT = 120          # printable context bytes kept around each hit
SEARCH_IMAGE_MAX_HITS = 8           # hits per page
SEARCH_IMAGE_MAX_QUERY = 256        # literal query length
SEARCH_IMAGE_CHUNK_BYTES = 1024 * 1024


def _printable_context(data: bytes) -> str:
    """Make bounded bytes printable; non-printable runs collapse to escapes."""
    return "".join(chr(b) if 32 <= b < 127 else "\\x%02x" % b for b in data)


def search_target_image(args: dict[str, Any]) -> dict[str, Any]:
    """Literal, bounded search inside the bound process's actual executable image.

    Evidence for embedded loader/plugin strings; no process memory, no execution,
    no whole-binary dumps. Query is a literal (no regex), matched case-sensitively.
    """
    query = args.get("query")
    if not isinstance(query, str) or not query or len(query.encode('utf-8')) > SEARCH_IMAGE_MAX_QUERY:
        raise ValueError("query must be a non-empty literal string up to "
                         + str(SEARCH_IMAGE_MAX_QUERY) + " bytes")
    offset = args.get("offset", 0)
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise ValueError("offset must be an integer")
    if offset < 0:
        raise ValueError("offset must be >= 0")
    context_bytes = args.get('context_bytes', SEARCH_IMAGE_CONTEXT)
    if not isinstance(context_bytes, int) or isinstance(context_bytes, bool) or not 0 <= context_bytes <= 2048:
        raise ValueError('context_bytes must be an integer from 0 to 2048')
    max_hits = min(SEARCH_IMAGE_MAX_HITS, max(1, 8192 // (2 * context_bytes + len(query.encode('utf-8')))))

    p = target_process()
    try:
        exe = Path(p.exe())
        stat_result = exe.stat()
    except (psutil.Error, OSError) as exc:
        raise ValueError("target executable is unavailable: " + type(exc).__name__) from exc
    if not exe.is_file():
        raise ValueError("target exe is not a regular file")
    size = stat_result.st_size
    if size > SEARCH_IMAGE_MAX_MIRROR_BYTES:
        return {"status": "refused", "reason": "image exceeds the bounded search limit",
                "size": size, "path_class": "target-exe"}

    needle = query.encode("utf-8")
    hits = []
    # Bounded buffered reads, not mmap: mapping some installed executable images
    # can terminate the reader on macOS. Keep only a chunk and query overlap.
    position = min(offset, size)
    if position < size:
        with exe.open("rb") as image, exe.open("rb") as excerpts:
            image.seek(position)
            carry = b''
            next_match = position
            while len(hits) < max_hits:
                chunk = image.read(SEARCH_IMAGE_CHUNK_BYTES)
                if not chunk:
                    position = size
                    break
                data = carry + chunk
                base = position - len(carry)
                cursor = max(0, next_match - base)
                position += len(chunk)
                while len(hits) < max_hits:
                    found = data.find(needle, cursor)
                    if found < 0:
                        break
                    absolute = base + found
                    start = max(0, absolute - context_bytes)
                    end = min(size, absolute + len(needle) + context_bytes)
                    excerpts.seek(start)
                    hits.append({"offset": absolute, "context": _printable_context(excerpts.read(end - start))})
                    cursor = found + len(needle)
                    next_match = base + cursor
                carry = data[-(len(needle) - 1):] if len(needle) > 1 else b''
            if len(hits) == max_hits:
                position = next_match
    next_offset = None
    if hits and position < size and len(hits) == max_hits:
        next_offset = position
    return {
        "status": "collected",
        "target": {"pid": TARGET_PID, "create_time": TARGET_CREATE_TIME},
        "path_class": "target-exe",
        "size": size,
        "mtime": stat_result.st_mtime,
        "query_bytes": len(needle),
        "searched_range": [min(offset, size), position],
        "hits": hits,
        "next_offset": next_offset,
        "truncated": next_offset is not None,
        "uncertainty": ["literal match only; strings may be split or encoded at runtime"],
    }


def _continuation_context() -> dict[str, Any]:
    """Deterministic continuation context for get_target_context.

    Bounded saved findings, open questions and evidence ids are injected so a
    continuation does not depend on prompt compliance. stdout is never replayed.
    """
    if RESUME_RUN_DIR is None:
        return {"status": "not_requested"}
    try:
        return _saved_investigation()
    except Exception as exc:
        return {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}


def call_tool(name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == 'read_evidence':
        target_process()
        ref = args.get('evidence_id', '')
        _validate_finding_evidence([ref])
        item = json.loads((EVIDENCE_DIR / (ref + '.json')).read_text())
        value = item['result']
        pointer = args.get('select', '')
        if pointer:
            if not isinstance(pointer, str) or not pointer.startswith('/'):
                raise ValueError('select must be a JSON pointer')
            for part in pointer[1:].split('/'):
                key = part.replace('~1', '/').replace('~0', '~')
                try:
                    value = value[int(key)] if isinstance(value, list) else value[key]
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    fields = list(value) if isinstance(value, dict) else 'array indices' if isinstance(value, list) else 'scalar value'
                    raise ValueError('Unknown evidence selector; available fields: ' + str(fields)) from exc
        offset = args.get('offset', 0)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError('offset must be a non-negative integer')
        next_offset = None
        if isinstance(value, (list, str)):
            length = len(value)
            page = 20 if isinstance(value, list) else 4000
            value = value[offset:offset + page]
            next_offset = offset + page if offset + page < length else None
        if isinstance(value, dict) and len(json.dumps(value)) > 6000:
            value = {'available_keys': list(value), 'note': 'Select a /field to retrieve its preserved evidence; no data was discarded.'}
        return {'source_evidence_id': ref, 'source_tool': item['tool'], 'select': pointer,
                'value': value, 'next_offset': next_offset,
                'target': {'pid': TARGET_PID, 'create_time': TARGET_CREATE_TIME}}
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
    if name == "inspect_entry_surface":
        p = target_process()
        surface = entry_surface(p)
        surface["metadata"] = metadata_candidates(surface)
        return surface
    if name == "find_related_files":
        p = target_process()
        surface = entry_surface(p)
        return find_related_files(
            surface,
            name_pattern=args.get("name_pattern", "*"),
            scope=args.get("scope", "all"),
            limit=args.get("limit", 20),
            offset=args.get("offset", 0),
        )
    if name == "read_related_file":
        p = target_process()
        surface = entry_surface(p)
        return read_related_file(surface, args.get("path", ""))
    if name == "submit_investigation_finding":
        target_process()
        return _submit_investigation_finding(args)
    if name == "get_saved_investigation":
        target_process()
        return _saved_investigation()
    if name == "search_target_image":
        target_process()
        return search_target_image(args)
    if name == "propose_recipe":
        target_process()
        recipe = args.get("recipe", {})
        normalization = 'none'
        if isinstance(recipe, str):
            if len(recipe.encode('utf-8')) > 1024 * 1024:
                raise ValueError('encoded candidate exceeds proposal scope')
            raw = recipe.rstrip()
            try:
                recipe = json.loads(raw)
                normalization = 'decoded_json_string'
            except json.JSONDecodeError as exc:
                # Narrow provider compatibility: only a missing final outer
                # object brace. Never invent values or alter generated source.
                if exc.pos != len(raw) or not raw.startswith('{'):
                    raise
                recipe = json.loads(raw + '}')
                normalization = 'decoded_json_string_missing_final_object_brace'
        recipe = redact(recipe)
        if not isinstance(recipe, dict):
            raise ValueError("recipe must be an object")
        required = {"match_features", "observation", "hook", "fallback"}
        missing = sorted(required - set(recipe))
        if missing:
            raise ValueError("recipe missing fields: " + ", ".join(missing))
        _validate_investigation_summary(recipe)
        from runtime.recipe_validation import validate
        validate(recipe, EVIDENCE_DIR,
                 target={"pid": TARGET_PID, "create_time": TARGET_CREATE_TIME},
                 known_harness_ids=_known_harness_ids(recipe))
        RECIPE_DIR.mkdir(parents=True, exist_ok=True)
        path = RECIPE_DIR / "candidate.json"
        payload = {"status": "candidate", "created_at": now(), "recipe": recipe,
                   "transport_normalization": normalization}
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
                   "local_evidence": collect(p),
                   "launch_evidence": call_tool("inspect_entry_surface", {}),
                   "prior_memory": load_prior(),
                   "prior_experience": call_tool("get_prior_experience", {}),
                   "prior_investigation": _continuation_context(),
                   "loader_surface": call_tool("inspect_loader_surface", {})}
        if OBSERVE_URL:
            context["observation"] = call_tool("inspect_observation", {})
        return context
    if name == "observe_runtime_surface":
        from runtime.collection import collect
        return {'local_evidence': collect(p),
                'launch_evidence': call_tool("inspect_entry_surface", {}),
                'runtime': 'unknown',
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


def _known_harness_ids(recipe: dict[str, Any]) -> set[str] | None:
    """Known prior harness ids, read only when the recipe declares an evolution.

    Returns None when ``evolves_prior_harness`` is absent/null/empty, so the
    common new-family path performs no prior read at all. When the field does
    carry a value the ids come from the same isolated prior the matcher uses;
    a corrupt prior stays an explicit error instead of silently passing.
    """
    features = recipe.get("match_features") if isinstance(recipe, dict) else None
    if not isinstance(features, dict):
        return None
    target = features.get("evolves_prior_harness")
    if not isinstance(target, str) or not target.strip():
        return None
    prior = load_prior()
    value = prior.get("value") if isinstance(prior, dict) else None
    ids: set[str] = set()
    if isinstance(value, dict):
        for entry in value.get("fingerprints") or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
                ids.add(entry["id"])
    return ids


TOOLS = [
    {"name": "read_evidence", "description": "Retrieve preserved successful observation evidence by id, especially after compaction/continuation. Use select JSON pointer (e.g. /metadata or /content), and next_offset for text/list pages. Cite the original source_evidence_id. Do not repeat expensive observations just to recover their existing results.", "inputSchema": {"type": "object", "required": ["evidence_id"], "properties": {"evidence_id": {"type": "string"}, "select": {"type": "string"}, "offset": {"type": "integer", "minimum": 0}}}},
    {"name": "get_target_context", "description": "Read the supervisor-bound target dossier, process tree, stream shape, and prior memory.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_entry_surface", "description": "Read raw and resolved executable/entry paths, parent/child identities, package metadata candidates, sources, and conflicts. This is evidence only; it never chooses an Agent identity.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "find_related_files", "description": "Enumerate process-related files, at most 20 per page. Scope may be a returned root token or an evidence-derived absolute subdirectory inside a returned root (e.g. a package directory named in a manifest). Follow specific package/import leads rather than enumerating unrelated dependencies. Pass next_offset as offset for further pages. Secret-like files are excluded.", "inputSchema": {"type": "object", "properties": {"name_pattern": {"type": "string"}, "scope": {"type": "string"}, "limit": {"type": "integer"}, "offset": {"type": "integer", "minimum": 0}}}},
    {"name": "read_related_file", "description": "Read one file previously found below a process-derived root. Content is bounded, parsed when possible, and redacted; arbitrary paths and credentials are rejected.", "inputSchema": {"type": "object", "required": ["path"], "properties": {"path": {"type": "string"}}}},
    {"name": "submit_investigation_finding", "description": "Persist one evidence-backed identity or asset finding without proposing or installing a Hook. Findings are partial, versioned and bounded.", "inputSchema": {"type": "object", "required": ["kind", "status", "evidence_refs"], "properties": {"display": {"type": "object", "description": "Standard Chinese display: {version:1, summary:string, facts:[{label:string,value:string}], scope:string}"}, "kind": {"type": "string", "enum": ["identity", "asset"]}, "asset": {"type": "string", "enum": ["model_gateway", "mcp", "skills", "rules"]}, "status": {"type": "string"}, "roles": {"type": "array", "items": {"type": "string"}}, "role_reasoning": {"type": "string"}, "name": {"type": "string"}, "runtime": {"type": "string"}, "entry": {"type": "string"}, "version": {"type": "string"}, "value": {}, "summary": {}, "details": {}, "uncertainty": {"type": "array", "items": {"type": "string"}}, "open_questions": {"type": "array", "items": {"type": "string"}}, "evidence_refs": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "get_saved_investigation", "description": "On an explicit continuation, read only the previous bounded lifecycle summary, partial findings, open questions and evidence ids. Previous stdout is never replayed.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "observe_tree", "description": "Observe only the target process and descendants within the supervisor scope.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "observe_runtime_surface", "description": "Inspect generic runtime, files, network shape, children, and stream capabilities; no secrets are returned.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_config_surface", "description": "Read selected configuration names from bound target files. No raw contents, secrets, .env or arbitrary paths.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_network_peers", "description": "Inspect active local listening ports and remote model endpoint connections.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_execution_trace", "description": "Inspect sensitive files accessed and active child command lines spawned by the agent.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "probe_help", "description": "Run only --help and --version against the observed executable, without a shell or arbitrary arguments.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_stream", "description": "Inspect a supervisor-exposed output stream and return structure samples after redaction.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_loader_surface", "description": "Read bounded target app-loader, config-resolution and plugin-scan evidence; target cwd is never treated as install scope.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "inspect_observation", "description": "Read the configured loopback observation receiver health and event types, bound to the target PID+create_time.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_target_image", "description": "Literal (case-sensitive, non-regex) search inside the bound process's actual executable file. Use context_bytes up to 2048 to inspect surrounding loader code, not just a short hit. Returns bounded printable context, hit offsets, next_offset for paging, size/mtime and target identity. Never reads process memory, executes the file, or returns the whole binary.", "inputSchema": {"type": "object", "required": ["query"], "properties": {"query": {"type": "string", "maxLength": 256}, "offset": {"type": "integer", "minimum": 0}, "context_bytes": {"type": "integer", "minimum": 0, "maximum": 2048}}}},
    {"name": "get_prior_recipe", "description": "Read prior committed generic memory for candidate validation.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "get_prior_experience", "description": "Read bounded prior lifecycle outcomes for this compatible runtime; corrupt history is an explicit error and private paths are omitted.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "propose_recipe", "description": "Write a typed candidate recipe for Supervisor verification; cannot activate hooks or execute commands.", "inputSchema": {"type": "object", "required": ["recipe"], "properties": {"recipe": RECIPE_SCHEMA}}},
]


def send(message: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> None:
    selected = {name.strip() for name in os.environ.get('ASG_ANALYST_TOOL_ALLOWLIST', '').split(',') if name.strip()}
    known = {tool['name'] for tool in TOOLS}
    if selected - known:
        raise ValueError('Unknown phase tool names: ' + ', '.join(sorted(selected - known)))
    available = [tool for tool in TOOLS if not selected or tool['name'] in selected]
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
                send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": available}})
            elif method == "tools/call":
                params = req.get("params") or {}
                name = params.get("name")
                if selected and name not in selected:
                    raise ValueError('Tool not enabled in this task phase: ' + str(name))
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
