"""ASG Agent Live Monitor & Autonomous Runtime Governance Engine
功能：
1. 周期实时扫描操作系统进程，通过零先验行为特征画像打分（识别 Agent，间隔 ASG_SCAN_INTERVAL）
2. 特征指纹比对：指纹匹配只提供历史配方参考
3. 陌生 Agent 自动逆向接管：自动异步调度 Goose 执行 Agent Work 受控调查（LLM 路由见 llm.yaml）
4. 自主推导 Agent 真实业务名称、通信协议与观测 Recipe，并自动沉淀至指纹库
5. 实时拦截/挂接语义事件流，展示最新脱敏消息
"""
from __future__ import annotations

import os
import sys
import json
import hashlib
import time
import shutil
import shlex
from collections import deque
from copy import deepcopy
import threading
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import psutil

# 确保根目录在 sys.path
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from asg_os_sensor import Sensor, load_policies
from runtime import analyzer, matcher, onboarding, investigation_findings, autonomous_pipeline
from runtime import hook_data
from runtime.observation_registry import Registry
from runtime.status import presentation, DISABLED_REASON
from runtime.learned_presentation import hook_state as learned_hook_state
from runtime.tool_transport_health import ToolTransportHealth
from runtime.recipe_validation import validate as validate_recipe
from runtime.identity import identify, ownership, metadata_identity, runtime_discovery_candidate
from runtime.stream_parser import redact
from runtime.llm_config import analyst_key as load_analyst_key
from runtime.llm_config import analyst_route as load_analyst_route
from runtime.llm_config import goose_env as build_goose_env
from runtime.llm_config import load_environment
from runtime.llm_config import mask_key as mask_analyst_key
from runtime.llm_config import tls_exception_enabled

load_environment()

def _resolve_goose() -> Path:
    custom = os.environ.get("ASG_GOOSE_BIN", "").strip()
    if custom:
        return Path(custom)
    for cand in [Path.home() / ".local" / "bin" / "goose", Path.home() / ".local" / "bin" / "goose.exe"]:
        if cand.exists():
            return cand
    found = shutil.which("goose") or shutil.which("goose.exe")
    if found:
        return Path(found)
    return Path("goose")
GOOSE = _resolve_goose()
RECIPE = Path(os.environ.get("ASG_ANALYST_RECIPE", str(ROOT / "recipes" / "runtime_analyst.yaml")))

# 共享状态与锁
def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, default)))
    except Exception:
        return default


def _env_optional_positive_int(name: str) -> int | None:
    value = os.environ.get(name, "").strip()
    if not value or value.lower() in {"none", "disabled", "off"}:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    if parsed < 1:
        raise ValueError(f"{name} must be a positive integer or omitted")
    return parsed


def _env_optional_positive_float(name: str) -> float | None:
    value = os.environ.get(name, "").strip()
    if not value or value.lower() in {"none", "disabled", "off"}:
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if parsed <= 0:
        raise ValueError(f"{name} must be positive or omitted")
    return parsed


SCAN_INTERVAL_S = _env_int("ASG_SCAN_INTERVAL", 30)
SCAN_TIMER_CONDITION = threading.Condition()
SCAN_TIMER_REVISION = 0


def _scan_settings_path():
    return Path(os.environ.get('ASG_RUN_DIR', str(ROOT / 'artifacts/stage1/dashboard'))) / 'scan_settings.json'


def _validate_scan_interval(value):
    if type(value) is not int or not 1 <= value <= 86400:
        raise ValueError('请输入 1 到 86400 之间的整数秒数')
    return value


try:
    SCAN_INTERVAL_S = _validate_scan_interval(json.loads(_scan_settings_path().read_text())['scan_interval'])
except (OSError, ValueError, KeyError, TypeError):
    pass


def set_scan_interval(value):
    global SCAN_INTERVAL_S, SCAN_TIMER_REVISION
    value = _validate_scan_interval(value)
    from runtime.learned_install import _atomic
    with SCAN_TIMER_CONDITION:
        path = _scan_settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic(path, json.dumps({'scan_interval': value}).encode())
        SCAN_INTERVAL_S = value
        SCAN_TIMER_REVISION += 1
        with STATE_LOCK:
            SCAN_STATE['scan_interval'] = value
        SCAN_TIMER_CONDITION.notify_all()
    return value
MAX_ANALYSTS = _env_int("ASG_MAX_ANALYSTS", 2)
GOOSE_MAX_TURNS = _env_optional_positive_int("ASG_GOOSE_MAX_TURNS") if "ASG_GOOSE_MAX_TURNS" in os.environ else 40
GOOSE_MAX_TOOL_REPETITIONS = _env_optional_positive_int("ASG_GOOSE_MAX_TOOL_REPETITIONS") if "ASG_GOOSE_MAX_TOOL_REPETITIONS" in os.environ else 3
GOOSE_TIMEOUT_S = _env_optional_positive_float("ASG_GOOSE_TIMEOUT")
GOOSE_IDLE_DIAGNOSTIC_S = _env_optional_positive_float("ASG_GOOSE_IDLE_DIAGNOSTIC") or 300.0
GOOSE_STDOUT_MAX_BYTES = _env_int("ASG_GOOSE_STDOUT_MAX_BYTES", 4 * 1024 * 1024)
GOOSE_RETRY_COOLDOWN_S = _env_int("ASG_GOOSE_RETRY_COOLDOWN", 300)
AUTONOMOUS_ANALYSIS_ENABLED = os.environ.get("ASG_AUTONOMOUS_ANALYSIS", "1").strip().lower() not in {"0", "false", "no", "off"}
ONBOARDING_AUTO_INSTALL_ENABLED = os.environ.get("ASG_ONBOARDING_AUTO_INSTALL", "0").strip() == "1"
OBSERVE_URL = os.environ.get("ASG_OBSERVE_URL", "").strip().rstrip("/")
# Generic file-event binding: an external config declares target pid+create_time,
# log path and field mapping. Mutually exclusive with the HTTP receiver so a
# scanned instance is never granted evidence from two different sources.
OBSERVE_CONFIG = os.environ.get("ASG_OBSERVE_CONFIG", "").strip()
OBSERVE_CONFIG_ERROR = ""
if OBSERVE_CONFIG and OBSERVE_URL:
    OBSERVE_CONFIG_ERROR = "ASG_OBSERVE_CONFIG 与 ASG_OBSERVE_URL 不能同时配置"
try:
    OBSERVE_TIMEOUT_S = float(os.environ.get("ASG_OBSERVE_TIMEOUT", "1.0") or "1.0")
except ValueError:
    OBSERVE_TIMEOUT_S = 1.0
OBSERVE_MAX_RESPONSE_BYTES = 256 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """观测适配只允许配置的回环地址，绝不跟随 Location 跳出回环。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


OBSERVE_OPENER = urllib.request.build_opener(_NoRedirectHandler)
STATE_LOCK = threading.Lock()
SCAN_STATE = {
    "last_scan_time": None,
    "scan_interval": SCAN_INTERVAL_S,
    "scan_count": 0,
    "autonomous_analysis": AUTONOMOUS_ANALYSIS_ENABLED,
    "agents": [],
    "fingerprints_count": 0,
    "observation_adapter": {
        "status": "pending" if (OBSERVE_URL or OBSERVE_CONFIG) else "not_configured",
        "source": OBSERVE_CONFIG or OBSERVE_URL or None,
    },
    "active_investigations": {}  # pid -> {status, started_at, turns}
}

# 记录当前已挂起正在调查的 PID，避免重复拉起多个 Goose
INVESTIGATING_INSTANCES: dict[str, float] = {}  # instance_id -> create_time
INVESTIGATION_LOCK = threading.Lock()
INVESTIGATION_SEMAPHORE = threading.Semaphore(MAX_ANALYSTS)  # 最多同时允许 N 个 Goose 并发，防止跑满 API 与进程雪崩
INVESTIGATION_RESULTS: dict[str, dict[str, Any]] = {}  # instance_id -> result
INVESTIGATION_RETRY_AT: dict[str, float] = {}  # instance_id -> retry_at
ACTIVE_ANALYST_PROCESSES: dict[str, subprocess.Popen] = {}
INVESTIGATION_CANCEL_REQUESTS: set[str] = set()
INVESTIGATION_QUEUE: deque = deque()  # FIFO: {"pid", "create_time", "instance_id", "force"}
INVESTIGATION_QUEUED: dict[str, dict[str, Any]] = {}  # instance_id -> {enqueued_at, force}
INVESTIGATION_QUEUE_MAX = _env_int("ASG_INVESTIGATION_QUEUE_MAX", 16)
# 仅用于调度线程等待/唤醒；与 INVESTIGATION_LOCK 无嵌套持有，避免锁序问题。
_QUEUE_COND = threading.Condition()


def _goose_executable() -> str | None:
    """返回可执行 Goose 路径；避免缺失时进入无限后台重试。"""
    value = str(GOOSE)
    if GOOSE.is_absolute() or GOOSE.parent != Path("."):
        return value if GOOSE.is_file() and os.access(GOOSE, os.X_OK) else None
    return shutil.which(value)


def _record_investigation_result(instance_id: str, pid: int, create_time: float | None, status: str, message: str, run_dir: Path | None = None, details: dict[str, Any] | None = None) -> None:
    # Persist investigation outcome bound to the frozen instance (pid:create_time).
    # create_time is frozen by the caller at investigation start; we never re-read
    # psutil at completion, so a reused PID cannot hijack the result. All lifecycle
    # phases (running/retry/result/API) use the same instance_id.
    result = {
        "instance_id": instance_id,
        "pid": pid,
        "create_time": create_time,
        "status": status,
        "message": message,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if run_dir is not None:
        result["log_dir"] = str(run_dir)
    if details:
        result.update(deepcopy(details))
    with INVESTIGATION_LOCK:
        if run_dir is not None:
            path = run_dir / 'result.json'
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(result, ensure_ascii=False))
            os.replace(temporary, path)
        INVESTIGATION_RESULTS[instance_id] = result


def _record_onboarding_outcome(instance_id: str, pid: int, create_time: float | None,
                               status: str, reason: str, run_dir: Path | None = None,
                               compatibility: dict[str, Any] | None = None) -> None:
    """Persist investigation failures/deferments for the next Goose context.

    This is advisory history only; an experience-store error must not hide the
    primary investigation result or change the scanner's state.
    """
    if create_time is None:
        return
    payload: dict[str, Any] = {"status": status, "reason": reason}
    if run_dir is not None:
        payload["run_dir"] = str(run_dir)
    if compatibility is not None:
        payload["compatibility"] = deepcopy(compatibility)
    try:
        onboarding.record_transition(
            {"pid": pid, "create_time": create_time},
            "investigation_failed" if status in ("failed", "blocked", "unavailable") else "investigation_deferred",
            payload,
        )
    except (OSError, ValueError, TypeError) as exc:
        print(f"[Onboarding Experience] unable to record {instance_id}: {type(exc).__name__}", file=sys.stderr)


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or now()))


def _process_identity(pid: int | None) -> dict[str, Any]:
    """Capture PID plus create_time without guessing when the process is gone."""
    result: dict[str, Any] = {"pid": int(pid) if pid is not None else None, "create_time": None}
    if pid is None:
        return result
    try:
        result["create_time"] = float(psutil.Process(int(pid)).create_time())
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess, ValueError):
        pass
    return result


def _write_investigation_lifecycle(run_dir: Path, lifecycle: dict[str, Any]) -> Path:
    """Persist wrapper lifecycle separately so a timeout cannot erase its evidence."""
    path = run_dir / "investigation_lifecycle.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(redact(deepcopy(lifecycle)), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)
    return path


class _StreamJournal:
    """Keep bounded stream metadata; tool evidence files retain the useful payloads.

    Goose's stream-json output repeats the current message snapshot for every token.
    Persisting those snapshots verbatim grows without bound and mostly duplicates
    thinking text. The journal keeps message/block types, sizes and timestamps,
    while the MCP audit/evidence files remain the source for request results.
    """

    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.rotated_path = path.with_name(path.name + ".1")
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self.stream = path.open("w", encoding="utf-8")
        self.kept_bytes = 0
        self.input_bytes = 0
        self.input_lines = 0
        self.json_lines = 0
        self.rotations = 0
        self.message_ids: set[str] = set()
        self.block_counts: dict[str, int] = {}
        self.content_bytes: dict[str, int] = {}
        self.first_created: int | None = None
        self.last_created: int | None = None
        self.last_activity_at = time.time()

    @staticmethod
    def _summary(line: str) -> dict[str, Any]:
        try:
            payload = json.loads(line)
        except (TypeError, ValueError):
            return {"type": "non_json", "input_bytes": len(line.encode("utf-8", "replace"))}
        message = payload.get("message") if isinstance(payload, dict) else None
        if not isinstance(message, dict):
            return {"type": payload.get("type", "unknown") if isinstance(payload, dict) else "unknown"}
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        compact_blocks = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            kind = str(block.get("type", "unknown"))
            item: dict[str, Any] = {"type": kind}
            for field in ("thinking", "text"):
                if isinstance(block.get(field), str):
                    item[field + "_bytes"] = len(block[field].encode("utf-8"))
            if kind == "toolRequest":
                call = block.get("toolCall")
                value = call.get("value") if isinstance(call, dict) else None
                if isinstance(value, dict) and isinstance(value.get("name"), str):
                    item["tool"] = value["name"]
            compact_blocks.append(item)
        message_id = message.get("id")
        return {
            "type": payload.get("type", "unknown"),
            "message_id_hash": hashlib.sha256(str(message_id).encode("utf-8")).hexdigest()[:12],
            "created": message.get("created"),
            "role": message.get("role"),
            "blocks": compact_blocks,
        }

    def consume(self, line: str) -> None:
        raw_size = len(line.encode("utf-8", "replace"))
        self.input_bytes += raw_size
        self.input_lines += 1
        self.last_activity_at = time.time()
        summary = self._summary(line)
        if summary.get("type") != "non_json":
            self.json_lines += 1
        if summary.get("message_id_hash"):
            self.message_ids.add(summary["message_id_hash"])
        created = summary.get("created")
        if isinstance(created, int):
            self.first_created = created if self.first_created is None else min(self.first_created, created)
            self.last_created = created if self.last_created is None else max(self.last_created, created)
        for block in summary.get("blocks", []):
            kind = block.get("type", "unknown")
            self.block_counts[kind] = self.block_counts.get(kind, 0) + 1
            for field in ("thinking_bytes", "text_bytes"):
                if field in block:
                    key = field[:-6]
                    self.content_bytes[key] = self.content_bytes.get(key, 0) + int(block[field])
        encoded = (json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if self.kept_bytes + len(encoded) > self.max_bytes:
            self.stream.flush()
            self.stream.close()
            try:
                os.replace(self.path, self.rotated_path)
            except FileNotFoundError:
                pass
            self.stream = self.path.open("w", encoding="utf-8")
            self.kept_bytes = 0
            self.rotations += 1
        self.stream.write(encoded.decode("utf-8"))
        self.stream.flush()
        self.kept_bytes += len(encoded)

    def stats(self) -> dict[str, Any]:
        return {
            "input_bytes": self.input_bytes,
            "input_lines": self.input_lines,
            "json_lines": self.json_lines,
            "journal_bytes": self.kept_bytes,
            "max_bytes": self.max_bytes,
            "rotations": self.rotations,
            "unique_message_ids": len(self.message_ids),
            "block_counts": dict(self.block_counts),
            "content_bytes": dict(self.content_bytes),
            "first_created": self.first_created,
            "last_created": self.last_created,
            "last_activity_at": self.last_activity_at,
        }

    def close(self) -> dict[str, Any]:
        self.stream.flush()
        self.stream.close()
        return self.stats()


def _drain_goose_stdout(process: subprocess.Popen, journal: _StreamJournal) -> None:
    stream = process.stdout
    if stream is None:
        return
    try:
        for line in stream:
            journal.consume(line)
    finally:
        stream.close()


def _tool_process_alive(goose_pid: int) -> bool | None:
    """Only inspect the extension launched beneath our own Goose process."""
    uncertain = False
    try:
        children = psutil.Process(goose_pid).children(recursive=True)
    except psutil.Error:
        return None
    for child in children:
        try:
            if str(ROOT / 'runtime' / 'analyst_tools.py') in child.cmdline():
                return child.is_running() and child.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            continue
        except psutil.Error:
            uncertain = True
    return None if uncertain else False


def _target_matches(value: dict[str, Any], pid: int, create_time: float | None) -> bool:
    target = value.get("target") if isinstance(value, dict) else None
    if not isinstance(target, dict) or target.get("pid") != pid or create_time is None:
        return False
    try:
        return abs(float(target.get("create_time")) - float(create_time)) <= 1e-3
    except (TypeError, ValueError):
        return False


def _latest_investigation_run(pid: int, create_time: float | None) -> Path | None:
    """Find the newest saved run for this exact process instance."""
    root = Path(os.environ.get("ASG_RUN_DIR", str(ROOT / "artifacts" / "stage1" / "dashboard")))
    candidates = sorted(root.glob(f"pid_{pid}_*"), key=lambda item: item.stat().st_mtime, reverse=True)
    for candidate in candidates[:64]:
        for name in ("investigation_lifecycle.json", "result.json", "investigation_findings.json"):
            path = candidate / name
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                continue
            if _target_matches(value, pid, create_time):
                return candidate
    return None


def _prepare_resume_evidence(previous: Path, current: Path, pid: int, create_time: float | None) -> dict[str, Any]:
    """Import only bounded evidence JSON; stdout is intentionally never copied."""
    lifecycle_path = previous / "investigation_lifecycle.json"
    if lifecycle_path.is_file():
        try:
            lifecycle = json.loads(lifecycle_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"previous lifecycle unreadable: {type(exc).__name__}") from exc
        if not _target_matches(lifecycle, pid, create_time):
            raise ValueError("previous investigation belongs to another target instance")
    findings_path = previous / "investigation_findings.json"
    if findings_path.is_file():
        # A corrupt prior is an explicit continuation failure; it must never be
        # silently interpreted as an empty history or overwritten.
        investigation_findings.load(findings_path, target={"pid": pid, "create_time": create_time})
    source = previous / "evidence"
    destination = current / "evidence"
    destination.mkdir(parents=True, exist_ok=True)
    copied = 0
    copied_bytes = 0
    for item in sorted(source.glob("ev-*.json"), key=lambda path: path.name)[-128:]:
        try:
            size = item.stat().st_size
            if size > 256 * 1024 or copied_bytes + size > 8 * 1024 * 1024:
                continue
            shutil.copyfile(item, destination / item.name)
        except OSError:
            continue
        copied += 1
        copied_bytes += size
    return {"source": str(previous), "evidence_files_copied": copied, "evidence_bytes_copied": copied_bytes}


def _load_partial_findings(run_dir: Path | None, target: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if run_dir is None:
        return None
    path = run_dir / "investigation_findings.json"
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return {"status": "failed", "reason": f"分项调查结果读取失败: {type(exc).__name__}", "path": str(path)}
    if not isinstance(value, dict):
        return {"status": "failed", "reason": "分项调查结果结构无效", "path": str(path)}
    if target is not None and not _target_matches(value, int(target["pid"]), target.get("create_time")):
        return {"status": "failed", "reason": "分项调查结果绑定了其他实例", "path": str(path)}
    value["path"] = str(path)
    return value


def _partial_result_details(run_dir: Path, target: dict[str, Any]) -> dict[str, Any]:
    partial = _load_partial_findings(run_dir, target)
    return {"partial_findings": partial} if partial is not None else {}


_PARTIAL_ASSET_FIELDS = {
    "model_gateway": "model_routing",
    "mcp": "registered_tools_and_mcp",
    "skills": "skills",
    "rules": "system_prompt_rules",
}


def _partial_asset_collections(partial: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Project Goose's evidence-backed partial findings into the shared status API."""
    if not isinstance(partial, dict):
        return {}
    findings = partial.get("findings")
    assets = findings.get("assets") if isinstance(findings, dict) else None
    if not isinstance(assets, dict):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name, field in _PARTIAL_ASSET_FIELDS.items():
        finding = assets.get(name)
        if not isinstance(finding, dict):
            continue
        refs = finding.get("evidence_refs", [])
        result[field] = {
            "status": finding.get("status", "unknown"),
            "value": finding.get("value"),
            "source": "goose finding",
            "sources": refs if isinstance(refs, list) else [],
            "evidence_refs": refs if isinstance(refs, list) else [],
            "uncertainty": finding.get("uncertainty", []),
            "message": "Goose 部分调查结果",
            "display": finding.get("display"),
        }
    return result


def _agent_classification(partial_identity: Any) -> dict[str, Any]:
    """角色三态分类：confirmed_agent / infrastructure / pending（候选·待确认）。

    仅依据 Goose 证据绑定的 identity.value.roles 判定；名称、依赖、品牌、端口、
    发现分数不参与。无角色调查（旧历史、未调查）一律 pending，不静默删除。
    """
    pending = {"status": "pending", "label": "候选/待确认", "roles": [],
               "source": [], "reasoning": None}
    if not isinstance(partial_identity, dict) or partial_identity.get("status") != "identified":
        return pending
    value = partial_identity.get("value")
    roles = value.get("roles") if isinstance(value, dict) else None
    if not isinstance(roles, list) or not roles or "unknown" in roles:
        return pending
    evidence = partial_identity.get("evidence_refs")
    return {
        "status": "confirmed_agent" if "agent" in roles else "infrastructure",
        "label": "确认 Agent" if "agent" in roles else "基础设施",
        "roles": roles,
        "source": evidence if isinstance(evidence, list) else [],
        "reasoning": value.get("role_reasoning") if isinstance(value, dict) else None,
    }


def _observe_request(path: str) -> tuple[int | None, dict[str, Any] | None, str | None]:
    """读取本机观测接收器的 JSON；HTTP 503 仍可能带有合法状态正文。"""
    if not OBSERVE_URL:
        return None, None, "ASG_OBSERVE_URL 未配置"
    try:
        parsed = urllib.parse.urlsplit(OBSERVE_URL)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            return None, None, "观测服务地址必须是本机 HTTP 回环地址"
    except ValueError:
        return None, None, "观测服务地址无效"
    try:
        request = urllib.request.Request(OBSERVE_URL + path, headers={"Accept": "application/json"})
        with OBSERVE_OPENER.open(request, timeout=OBSERVE_TIMEOUT_S) as response:
            status = response.status
            raw = response.read(OBSERVE_MAX_RESPONSE_BYTES + 1)
            if len(raw) > OBSERVE_MAX_RESPONSE_BYTES:
                return status, None, "观测服务响应过大"
            body = raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        try:
            raw = exc.read(OBSERVE_MAX_RESPONSE_BYTES + 1)
            if len(raw) > OBSERVE_MAX_RESPONSE_BYTES:
                return status, None, "观测服务错误响应过大"
            body = raw.decode("utf-8", "replace")
        except OSError:
            body = ""
    except (OSError, ValueError, urllib.error.URLError) as exc:
        return None, None, "%s: %s" % (type(exc).__name__, exc)
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return status, None, "观测服务返回非 JSON"
    if not isinstance(payload, dict):
        return status, None, "观测服务返回结构无效"
    return status, payload, None


def _read_file_observation_snapshot() -> dict[str, Any]:
    """通用文件事件源：外部配置绑定 pid+create_time、日志路径与字段映射。

    该源与 HTTP 接收器产出同一份快照结构，因此页面与 API 无需分支；
    它只做记录，不声明阻断，也不把历史 pid 当作持续健康。
    """
    from runtime import observation_source

    try:
        config = observation_source.load_config(OBSERVE_CONFIG)
    except ValueError as exc:
        return {"status": "invalid", "source": OBSERVE_CONFIG, "message": str(exc)}
    try:
        return observation_source.snapshot(config)
    except OSError as exc:
        return {"status": "unavailable", "source": OBSERVE_CONFIG,
                "message": "观测文件源读取失败: %s" % type(exc).__name__}


def _observation_registry():
    return Registry(Path(os.environ.get('ASG_RUN_DIR', str(ROOT / 'artifacts' / 'stage1' / 'dashboard'))))


def _auto_bind_observations(agents):
    """Re-create the observation binding for a live instance whose learned recipe
    already declares an observation source.

    A restart changes pid/create_time, which invalidates the previous binding;
    without this, a restarted instance silently stops being observable and
    controllable until someone rebinds by hand. Best effort: it never raises,
    never fabricates events, and only binds when the declared log already exists
    on disk (so a source nothing writes is never published).
    """
    try:
        from runtime import learned_onboarding
        from runtime.learned_install import _atomic
        registry = _observation_registry()
        try:
            known = set(registry._read().keys())
        except (OSError, ValueError):
            known = set()
        run_dir = Path(os.environ.get('ASG_RUN_DIR', str(ROOT / 'artifacts' / 'stage1' / 'dashboard')))
        for agent in agents:
            adapter = agent.get('adapter') or {}
            if adapter.get('match_status') != 'exact':
                continue
            recipe = adapter.get('historical_recipe')
            if not isinstance(recipe, dict) or not recipe.get('observation_source'):
                continue
            workspace = (recipe.get('hook') or {}).get('workspace')
            instance_id = str(agent.get('instance_id') or '')
            if not workspace or ':' not in instance_id or instance_id in known:
                continue
            pid_text, _, created_text = instance_id.partition(':')
            try:
                target = {'pid': int(pid_text), 'create_time': float(created_text)}
            except ValueError:
                continue
            try:
                prepared = learned_onboarding._prepare_observation(recipe, Path(workspace), target)
            except (ValueError, OSError, TypeError):
                continue
            if prepared.get('status') != 'configured':
                continue
            config = prepared['config']
            if not Path(config.get('log_path') or '').is_file():
                continue
            state_dir = run_dir / 'auto-bind' / instance_id.replace(':', '_')
            try:
                state_dir.mkdir(parents=True, exist_ok=True)
                path = state_dir / learned_onboarding.OBSERVATION_FILE
                payload = {key: config[key] for key in
                           ('version', 'mapping_mode', 'target', 'log_path', 'fields', 'event_names')}
                _atomic(path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode('utf-8'))
                registry.register(str(path), target)
                known.add(instance_id)
            except (OSError, ValueError):
                continue
    except Exception as exc:  # noqa: BLE001 - best effort; never break a scan
        print('[auto-bind] skipped:', type(exc).__name__, file=sys.stderr)


def _observation_pointer_path():
    return Path(os.environ.get('ASG_RUN_DIR', str(ROOT / 'artifacts' / 'stage1' / 'dashboard'))) / 'active_observation.json'


def _wire_observe_config(result, target, *, persist=True):
    """Shared manual/automatic consumer; persist only successful executor bindings."""
    global OBSERVE_CONFIG, OBSERVE_CONFIG_ERROR
    if result.get('status') not in ('installed', 'bound', 'rebound', 'activation_rebound') or not result.get('config_path'):
        return False
    if OBSERVE_URL:
        return False
    from runtime.observation_source import load_config, target_liveness
    config = load_config(result['config_path'])
    expected = {'pid': int(target['pid']), 'create_time': float(target['create_time'])}
    if config['target'] != expected or target_liveness(expected) is not True:
        raise ValueError('executor binding target is mismatched or no longer live')
    path = str(Path(result['config_path']).resolve(strict=True))
    if persist:
        from runtime.learned_install import _atomic
        pointer = _observation_pointer_path()
        pointer.parent.mkdir(parents=True, exist_ok=True)
        _atomic(pointer, json.dumps({'config_path': path, 'target': expected}).encode())
    _observation_registry().register(path, expected)
    OBSERVE_CONFIG = path
    OBSERVE_CONFIG_ERROR = ''
    return True


def _restore_observe_config():
    if OBSERVE_CONFIG or OBSERVE_URL:
        return
    try:
        saved = json.loads(_observation_pointer_path().read_text())
        _wire_observe_config({'status': 'bound', 'config_path': saved['config_path']}, saved['target'], persist=False)
    except FileNotFoundError:
        return
    except (ValueError, KeyError, OSError) as exc:
        global OBSERVE_CONFIG_ERROR
        OBSERVE_CONFIG_ERROR = '保存的观测绑定无法恢复: ' + str(exc)


def read_observation_snapshot() -> dict[str, Any]:
    """受控读取观测契约；该适配层不改变扫描器的 Agent 列表。"""
    _restore_observe_config()
    if OBSERVE_CONFIG_ERROR:
        return {"status": "invalid", "source": OBSERVE_CONFIG or OBSERVE_URL or None,
                "message": OBSERVE_CONFIG_ERROR}
    if OBSERVE_CONFIG:
        return _read_file_observation_snapshot()
    if not OBSERVE_URL:
        return {"status": "not_configured", "source": None,
                "message": "未配置隔离观测接收器；扫描结果仍来自本机进程扫描"}
    health_status, health, health_error = _observe_request("/health")
    events_status, events, events_error = _observe_request("/events")
    if health is None:
        return {"status": "unavailable", "source": OBSERVE_URL,
                "message": health_error or "观测服务不可用",
                "health_http": health_status, "events_http": events_status}
    try:
        instance_pid = int(health["instance_pid"])
        instance_create_time = float(health["instance_create_time"])
    except (KeyError, TypeError, ValueError):
        return {"status": "invalid", "source": OBSERVE_URL,
                "message": health_error or "观测服务未返回可绑定的 pid/create_time",
                "health_http": health_status, "events_http": events_status}
    health_status_name = health.get("status", "unknown")
    if health_status_name == "revoked":
        return {
            "status": "revoked",
            "source": OBSERVE_URL,
            "health_http": health_status,
            "events_http": events_status,
            "instance_pid": instance_pid,
            "instance_create_time": instance_create_time,
            "health": {
                "status": "revoked",
                "healthy": False,
                "reason": health.get("reason", "观测 manifest 已撤销或无效"),
                "loaded_observed": False,
            },
            "events": {"valid": 0, "invalid": 0},
            "capabilities": health.get("capabilities") or {},
            "message": "观测已撤销；保留已知绑定实例，未宣称 healthy",
        }
    if events is None:
        return {"status": "unavailable", "source": OBSERVE_URL,
                "message": events_error or "观测服务不可用",
                "health_http": health_status, "events_http": events_status}
    raw_valid = events.get("valid")
    raw_invalid = events.get("invalid")
    if (isinstance(raw_valid, bool) or not isinstance(raw_valid, int) or raw_valid < 0 or
            isinstance(raw_invalid, bool) or not isinstance(raw_invalid, int) or raw_invalid < 0):
        return {"status": "degraded", "source": OBSERVE_URL,
                "message": "观测服务 events 计数类型无效，拒绝授予实例观测成功",
                "health_http": health_status, "events_http": events_status,
                "instance_pid": instance_pid, "instance_create_time": instance_create_time}
    return {
        "status": "connected",
        "source": OBSERVE_URL,
        "health_http": health_status,
        "events_http": events_status,
        "instance_pid": instance_pid,
        "instance_create_time": instance_create_time,
        "health": {
            "status": health.get("status", "unknown"),
            "healthy": bool(health.get("healthy", False)),
            "reason": health.get("reason", ""),
            "loaded_observed": bool(health.get("loaded_observed", False)),
        },
        "events": {
            "valid": raw_valid,
            "invalid": raw_invalid,
        },
        "capabilities": health.get("capabilities") or {
            "observation": {"status": "supported", "label": "事件观测"},
            "blocking": {"status": "unsupported", "label": "未支持"},
        },
    }


def observation_for_instance(snapshot: dict[str, Any], pid: int,
                             create_time: float | None) -> dict[str, Any]:
    """只为 pid+create_time 均匹配的主实例授予观测证据。"""
    base = {"source": snapshot.get("source"), "status": "not_bound",
            "label": "未绑定观测证据", "loaded_observed": False,
            "recent_events": 0, "invalid_events": 0,
            "blocking": {"status": "unsupported", "label": "未支持"}}
    if snapshot.get("status") == "not_configured":
        base.update(status="not_configured", label="尚未接入观测接收器",
                    reason=snapshot.get("message", ""))
        return base
    if snapshot.get("status") == "revoked":
        observed_pid = snapshot.get("instance_pid")
        observed_ct = snapshot.get("instance_create_time")
        if pid == observed_pid and create_time is not None and observed_ct is not None and \
                abs(float(create_time) - float(observed_ct)) <= 1e-3:
            base.update(status="revoked", label="观测已撤销（未宣称 healthy）",
                        reason=snapshot.get("message", "观测 manifest 已撤销或无效"),
                        instance_id="%s:%s" % (observed_pid, observed_ct),
                        health_status="revoked")
        else:
            base["reason"] = "观测证据绑定实例已撤销；当前主 PID 未匹配"
        return base
    if snapshot.get("status") != "connected":
        base.update(status=snapshot.get("status", "unavailable"), label="观测服务不可用",
                    reason=snapshot.get("message", "观测服务未返回有效状态"))
        return base
    observed_pid = snapshot.get("instance_pid")
    observed_ct = snapshot.get("instance_create_time")
    if pid != observed_pid:
        base["reason"] = "观测证据绑定 PID %s；当前主 PID %s" % (observed_pid, pid)
        return base
    if create_time is None or observed_ct is None or abs(float(create_time) - float(observed_ct)) > 1e-3:
        base["reason"] = "观测证据的 create_time 与当前实例不匹配"
        return base
    health = snapshot.get("health", {})
    events = snapshot.get("events", {})
    loaded_observed = bool(health.get("loaded_observed", False))
    recent_events = int(events.get("valid", 0) or 0)
    # A file-event source re-checks live liveness on every read. A bound pid
    # whose process is gone keeps its recorded evidence visible but is never
    # reported as a currently healthy instance.
    if snapshot.get("target_alive") is False:
        base.update(status="target_gone", label="目标实例已退出（记录保留，不宣称健康）",
                    reason=snapshot.get("message", "目标进程已退出或被替换"),
                    instance_id="%s:%s" % (observed_pid, observed_ct),
                    health_status="stale", loaded_observed=False,
                    target_alive=False,
                    last_event_time=snapshot.get("last_event_time"),
                    recent_events=recent_events,
                    invalid_events=int(events.get("invalid", 0) or 0),
                    blocking=snapshot.get("blocking") or {"status": "unsupported", "label": "未支持"},
                    capabilities=snapshot.get("capabilities", {}))
        return base
    if not loaded_observed and recent_events == 0:
        base.update(status="bound_no_events", label="已绑定实例，但尚无观测事件",
                    reason=health.get("reason", "尚无 hook.loaded 或工具事件"),
                    instance_id="%s:%s" % (observed_pid, observed_ct),
                    health_status=health.get("status", "unknown"),
                    capabilities=snapshot.get("capabilities", {}))
        return base
    base.update(
        status="observed",
        label="已绑定观测证据（仅记录，不支持阻断）",
        instance_id="%s:%s" % (observed_pid, observed_ct),
        health_status=health.get("status", "unknown"),
        health_reason=health.get("reason", ""),
        loaded_observed=loaded_observed,
        recent_events=recent_events,
        invalid_events=int(events.get("invalid", 0) or 0),
        last_event_time=snapshot.get("last_event_time"),
        target_alive=snapshot.get("target_alive"),
        blocking=snapshot.get("blocking") or {"status": "unsupported", "label": "未支持"},
        capabilities=snapshot.get("capabilities", {}),
    )
    return base


def analyst_route() -> dict:
    return load_analyst_route()


def _freeze_instance(pid: int, struct: dict[str, Any]) -> tuple[str, float | None]:
    """调查启动时冻结实例身份。create_time 一经确定即贯穿整个生命周期。
    返回 (instance_id, create_time)。"""
    create_time = struct.get("create_time")
    if create_time is None:
        try:
            create_time = psutil.Process(pid).create_time()
        except psutil.Error:
            create_time = None
    return f"{pid}:{create_time}", create_time


def run_autonomous_investigation(pid: int, struct: dict[str, Any], force: bool = False,
                                 resume_from: Path | None = None):
    """由 Goose 执行后台非交互式受控逆向接管（模型路由见 llm.yaml）"""
    if not AUTONOMOUS_ANALYSIS_ENABLED:
        return
    instance_id, create_time = _freeze_instance(pid, struct)
    with INVESTIGATION_LOCK:
        if instance_id in INVESTIGATING_INSTANCES:
            return
        retry_at = INVESTIGATION_RETRY_AT.get(instance_id, 0)
        if not force and now() < retry_at:
            return
        INVESTIGATING_INSTANCES[instance_id] = create_time

    if not INVESTIGATION_SEMAPHORE.acquire(blocking=False):
        with INVESTIGATION_LOCK:
            INVESTIGATING_INSTANCES.pop(instance_id, None)
        if force:
            # 手动请求不丢失：插队到队首，由调度线程在槽位释放后执行。
            with INVESTIGATION_LOCK:
                INVESTIGATION_QUEUE.appendleft({"pid": pid, "create_time": create_time,
                                                "instance_id": instance_id, "force": True})
                INVESTIGATION_QUEUED[instance_id] = {"enqueued_at": iso(), "force": True}
            with _QUEUE_COND:
                _QUEUE_COND.notify_all()
        return
    try:
        _execute_investigation(pid, struct, instance_id, create_time,
                               force=force, resume_from=resume_from)
    finally:
        INVESTIGATION_SEMAPHORE.release()


def _execute_investigation(pid: int, struct: dict[str, Any], instance_id: str,
                           create_time: float | None, force: bool = False,
                           resume_from: Path | None = None) -> None:
    """执行一次调查；调用方必须已持有并发槽位并登记 INVESTIGATING_INSTANCES。"""
    # A reusable protocol gets first refusal, even when Goose is unavailable.
    if ONBOARDING_AUTO_INSTALL_ENABLED and create_time is not None and not force:
        from runtime.protocol_fastpath import attempt as protocol_attempt
        fast = protocol_attempt({'pid': pid, 'create_time': create_time})
        if fast.get('handled'):
            _record_investigation_result(instance_id, pid, create_time, 'partial', fast.get('reason', '协议直接接入自检中'))
            with INVESTIGATION_LOCK:
                INVESTIGATING_INSTANCES.pop(instance_id, None)
            return
    goose_bin = _goose_executable()
    if not goose_bin:
        message = f"未找到 Goose CLI（当前解析值: {GOOSE}）。请安装 block-goose-cli 或设置 ASG_GOOSE_BIN。"
        _record_investigation_result(instance_id, pid, create_time, "unavailable", message)
        _record_onboarding_outcome(instance_id, pid, create_time, "unavailable", message, compatibility=struct.get("compatibility"))
        with INVESTIGATION_LOCK:
            INVESTIGATION_RETRY_AT[instance_id] = now() + GOOSE_RETRY_COOLDOWN_S
            INVESTIGATING_INSTANCES.pop(instance_id, None)
        print(f"[Analyst Unavailable PID={pid}] {message}", file=sys.stderr)
        return

    phase = os.environ.get('ASG_INVESTIGATION_PHASE', '')
    if autonomous_pipeline.enabled():
        exact = matcher.classify(struct).get('status') == 'exact'
        phase = autonomous_pipeline.next_phase(instance_id, exact=exact)
        if force and phase is None:
            phase = 'assets'
        if phase is None:
            with INVESTIGATION_LOCK:
                INVESTIGATING_INSTANCES.pop(instance_id, None)
            return
        # A Hook phase may be a bounded repair attempt after an installation
        # that still has no binding or failed verification.  Persist the
        # attempt on the frozen instance so the pipeline's retry budget and
        # spacing survive scanner/process restarts.
        if phase == 'hook':
            checkpoint = autonomous_pipeline.read(instance_id)
            verification = checkpoint.get('verification') or {}
            if verification.get('status') in ('awaiting_binding', 'verification_failed'):
                prior_repair = checkpoint.get('repair') or {}
                try:
                    attempts = int(prior_repair.get('attempts', 0) or 0)
                except (TypeError, ValueError):
                    attempts = 0
                autonomous_pipeline.save(instance_id, 'repair', '',
                                         attempts=attempts + 1, at=time.time())
    selected_recipe = (ROOT / 'recipes' / ('runtime_assets.yaml' if phase == 'assets' else 'runtime_hook_analyst.yaml')) if autonomous_pipeline.enabled() else RECIPE
    lifecycle: dict[str, Any] = {}
    try:
        run_dir = Path(os.environ.get("ASG_RUN_DIR", str(ROOT / "artifacts" / "stage1" / "dashboard"))) / f"pid_{pid}_{int(time.time() * 1000)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        recipes_dir = run_dir / "recipes"
        recipes_dir.mkdir(parents=True, exist_ok=True)
        resume_details = None
        if resume_from is not None:
            resume_details = _prepare_resume_evidence(resume_from, run_dir, pid, create_time)

        stream_file = run_dir / "target_stream.jsonl"
        stream_file.touch()
        lifecycle = {
            "status": "running",
            "phase": phase,
            "started_at": iso(),
            "ended_at": None,
            "target": {"pid": int(pid), "create_time": create_time},
            "goose_process": {"pid": None, "create_time": None},
            "returncode": None,
            "timed_out": False,
            "end_reason": "running",
            "timeout_seconds": GOOSE_TIMEOUT_S,
            "timeout_enabled": GOOSE_TIMEOUT_S is not None,
            "max_turns": GOOSE_MAX_TURNS,
            "max_tool_repetitions": GOOSE_MAX_TOOL_REPETITIONS,
            "resume": ({
                "status": "continuing",
                "source": str(resume_from),
                "automatic_continuation": autonomous_pipeline.enabled() and not force,
                "details": resume_details,
            } if resume_from is not None else {
                "status": "available_from_saved_evidence",
                "source": "analyst_stdout.jsonl + analyst_tool_calls.jsonl + evidence/",
                "automatic_continuation": False,
            }),
        }
        lifecycle_path = _write_investigation_lifecycle(run_dir, lifecycle)

        def finish_lifecycle(status: str, end_reason: str, **extra: Any) -> None:
            lifecycle.update({"status": status, "end_reason": end_reason, "ended_at": iso(), **extra})
            _write_investigation_lifecycle(run_dir, lifecycle)

        with STATE_LOCK:
            SCAN_STATE["active_investigations"][pid] = {
                "status": "investigating",
                "instance_id": instance_id,
                "started_at": time.strftime("%H:%M:%S"),
                "log_dir": str(run_dir),
                "lifecycle_path": str(lifecycle_path),
            }

        try:
            route = analyst_route()
            key = load_analyst_key(route)
            if not key:
                message = "LLM 凭据缺失: " + str(route.get("key_env", "?"))
                finish_lifecycle("failed", "credentials_missing")
                _record_investigation_result(instance_id, pid, create_time, "failed", message, run_dir,
                                             {"lifecycle": deepcopy(lifecycle), **_partial_result_details(run_dir, {"pid": pid, "create_time": create_time})})
                _record_onboarding_outcome(instance_id, pid, create_time, "failed", message, run_dir, struct.get("compatibility"))
                print("[Analyst] route=" + route.get("route", "?") + " model=" + str(route.get("model", "?")) + " base=" + str(route.get("base_url", "?")) + " key=" + mask_analyst_key(key) + " (" + str(route.get("key_env", "?")) + ")", file=sys.stderr)
                return
            if os.environ.get("ASG_INSECURE_SSL", "").strip() == "1" and not tls_exception_enabled(route):
                message = "ASG_INSECURE_SSL 仅允许当前路由的显式 TLS 配置使用"
                finish_lifecycle("blocked", "tls_policy_blocked")
                _record_investigation_result(instance_id, pid, create_time, "blocked", message, run_dir,
                                             {"lifecycle": deepcopy(lifecycle), **_partial_result_details(run_dir, {"pid": pid, "create_time": create_time})})
                _record_onboarding_outcome(instance_id, pid, create_time, "blocked", message, run_dir, struct.get("compatibility"))
                print(f"[Analyst Blocked PID={pid}] {message}", file=sys.stderr)
                return

            extension = 'asg-runtime-tools:' + shlex.join([sys.executable, '-B', str(ROOT / 'runtime' / 'analyst_tools.py')])
            cmd = [
                goose_bin, "run", "--no-profile", "--no-session",
                "--recipe", str(selected_recipe),
                "--params", f"target_pid={pid}",
                "--provider", route["provider"],
                "--model", route["model"],
                "--output-format", "stream-json",
                "--with-extension", extension
            ]
            if GOOSE_MAX_TURNS is not None:
                cmd[cmd.index("--output-format"):cmd.index("--output-format")] = ["--max-turns", str(GOOSE_MAX_TURNS)]
            if GOOSE_MAX_TOOL_REPETITIONS is not None:
                cmd[cmd.index("--output-format"):cmd.index("--output-format")] = ["--max-tool-repetitions", str(GOOSE_MAX_TOOL_REPETITIONS)]

            env = os.environ.copy()
            env.update(build_goose_env(route, key, pid))
            env.update(ASG_REQUIRE_STANDARD_DISPLAY="1", ASG_INVESTIGATION_PHASE=phase or "", ASG_TARGET_PID=str(pid), ASG_TARGET_CREATE_TIME=str(create_time),
                       ASG_AUDIT_DIR=str(run_dir), ASG_RECIPE_DIR=str(recipes_dir),
                       ASG_TARGET_STREAM_FILE=str(stream_file),
                       ASG_FINDINGS_FILE=str(run_dir / "investigation_findings.json"))
            if resume_from is not None:
                env.update(
                    ASG_RESUME_FROM_RUN_DIR=str(resume_from),
                    ASG_RESUME_FINDINGS_FILE=str(resume_from / "investigation_findings.json"),
                    ASG_RESUME_LIFECYCLE_FILE=str(resume_from / "investigation_lifecycle.json"),
                    ASG_RESUME_AUDIT_FILE=str(resume_from / "analyst_tool_calls.jsonl"),
                    ASG_RESUME_EVIDENCE_DIR=str(run_dir / "evidence"),
                )

            out_path = run_dir / "analyst_stdout.jsonl"
            err_path = run_dir / "analyst_stderr.log"
            lifecycle.update({
                "command": cmd,
                "stdout_path": str(out_path),
                "stderr_path": str(err_path),
                "audit_dir": str(run_dir),
                "recipe_dir": str(recipes_dir),
            })
            _write_investigation_lifecycle(run_dir, lifecycle)

            print(f"[Analyst] Goose 开始自主逆向接管 PID={pid}...")
            t0 = time.time()
            err_stream = err_path.open("w", encoding="utf-8")
            journal = _StreamJournal(out_path, GOOSE_STDOUT_MAX_BYTES)
            analyst_process = None
            cancelled = False
            timed_out = False
            tool_health = ToolTransportHealth()
            try:
                analyst_process = subprocess.Popen(
                    cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=err_stream,
                    text=True, bufsize=1,
                )
                lifecycle["goose_process"] = _process_identity(analyst_process.pid)
                lifecycle["goose_process"]["pid"] = analyst_process.pid
                with INVESTIGATION_LOCK:
                    ACTIVE_ANALYST_PROCESSES[instance_id] = analyst_process
                _write_investigation_lifecycle(run_dir, lifecycle)
                reader = threading.Thread(target=_drain_goose_stdout, args=(analyst_process, journal),
                                          name=f"analyst-stream-{pid}", daemon=True)
                reader.start()
                deadline = t0 + GOOSE_TIMEOUT_S if GOOSE_TIMEOUT_S is not None else None
                while True:
                    analyst_returncode = analyst_process.poll()
                    audit_path = run_dir / "analyst_tool_calls.jsonl"
                    try:
                        audit_mtime = audit_path.stat().st_mtime
                        with audit_path.open(encoding="utf-8") as _af:
                            audit_count = sum(1 for line in _af if line.strip())
                    except OSError:
                        audit_mtime = None
                        audit_count = 0
                    activity_times = [journal.last_activity_at]
                    if audit_mtime is not None:
                        activity_times.append(audit_mtime)
                    last_activity = max(activity_times) if activity_times else t0
                    no_activity = max(0.0, time.time() - last_activity)
                    lifecycle["progress"] = {
                        "stream": journal.stats(),
                        "audit_tool_call_count": audit_count,
                        "last_activity_at": iso(last_activity),
                        "no_activity_seconds": round(no_activity, 1),
                        "diagnostic_status": "idle_long" if no_activity >= GOOSE_IDLE_DIAGNOSTIC_S else "active",
                    }
                    transport = tool_health.observe(_tool_process_alive(analyst_process.pid))
                    lifecycle['progress']['tool_transport'] = transport
                    _write_investigation_lifecycle(run_dir, lifecycle)
                    with INVESTIGATION_LOCK:
                        cancelled = instance_id in INVESTIGATION_CANCEL_REQUESTS
                        if cancelled:
                            INVESTIGATION_CANCEL_REQUESTS.discard(instance_id)
                    if cancelled and analyst_returncode is None:
                        analyst_process.terminate()
                        lifecycle["status"] = "cancelled"
                        lifecycle["end_reason"] = "cancelled"
                        break
                    if analyst_returncode is not None:
                        lifecycle["status"] = "completed" if analyst_returncode == 0 else "failed"
                        lifecycle["end_reason"] = "completed" if analyst_returncode == 0 else "nonzero_exit"
                        break
                    if transport == 'transport_lost':
                        lifecycle['status'] = 'failed'
                        lifecycle['end_reason'] = 'tool_transport_closed'
                        analyst_process.terminate()
                        break
                    if deadline is not None and time.time() >= deadline:
                        timed_out = True
                        lifecycle["timed_out"] = True
                        lifecycle["status"] = "timeout"
                        lifecycle["end_reason"] = "timeout"
                        analyst_process.kill()
                        break
                    time.sleep(1.0)
                if analyst_process.poll() is None:
                    try:
                        analyst_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        analyst_process.kill()
                        analyst_process.wait()
                analyst_returncode = analyst_process.returncode
                reader.join(timeout=10)
                if reader.is_alive():
                    lifecycle["stream_reader_warning"] = "stdout reader did not finish within 10 seconds"
                stream_stats = journal.close()
                lifecycle["returncode"] = analyst_returncode
                lifecycle["timed_out"] = timed_out
                lifecycle["stream"] = stream_stats
                lifecycle["ended_at"] = iso()
                lifecycle["elapsed_ms"] = int((time.time() - t0) * 1000)
                lifecycle["tool_call_count"] = lifecycle.get("progress", {}).get("audit_tool_call_count", 0)
                _write_investigation_lifecycle(run_dir, lifecycle)
            except OSError as exc:
                lifecycle["status"] = "failed"
                lifecycle["end_reason"] = "spawn_error"
                lifecycle["error_type"] = type(exc).__name__
                lifecycle["ended_at"] = iso()
                _write_investigation_lifecycle(run_dir, lifecycle)
                raise
            finally:
                err_stream.close()
                if analyst_process is not None:
                    with INVESTIGATION_LOCK:
                        ACTIVE_ANALYST_PROCESSES.pop(instance_id, None)
                try:
                    journal.close()
                except (AttributeError, OSError, ValueError):
                    pass
            cp = subprocess.CompletedProcess(cmd, analyst_returncode)
            elapsed_ms = int((time.time() - t0) * 1000)

            # 检查是否成功产出 candidate.json
            if (phase == 'assets'
                    and lifecycle.get('end_reason') == 'completed'):
                from runtime.asset_pass import outcome as asset_pass_outcome
                checkpoint = asset_pass_outcome(run_dir, {'pid': pid, 'create_time': create_time}, cp.returncode)
                if checkpoint is not None:
                    _record_investigation_result(
                        instance_id, pid, create_time, checkpoint['status'], checkpoint['message'],
                        run_dir, {'lifecycle': deepcopy(lifecycle),
                                  'asset_checkpoint': checkpoint['asset_checkpoint'],
                                  'partial_findings': checkpoint['partial_findings']})
                    onboarding.record_transition(
                        {'pid': pid, 'create_time': create_time}, 'asset_checkpoint_saved',
                        {'run_dir': str(run_dir), 'status': checkpoint['status'],
                         'compatibility': struct.get('compatibility')})
                    if autonomous_pipeline.enabled():
                        identity = checkpoint['partial_findings'].get('findings', {}).get('identity')
                        autonomous_pipeline.save(instance_id, 'assets', run_dir,
                            infrastructure=_agent_classification(identity)['status'] == 'infrastructure')
                    return

            candidate_file = recipes_dir / "candidate.json"
            if candidate_file.exists():
                payload = json.loads(candidate_file.read_text(encoding="utf-8"))
                recipe = payload.get("recipe", {})
                identity = recipe.get("agent_identity_name", "")
                # 严格质量门禁：只有逆向成功观测到有效特征且非"unidentified/unknown"时，才允许写入指纹库
                if (
                    isinstance(recipe, dict)
                    and "match_features" in recipe
                    and identity not in ["", "unknown", "unknown-runtime", "unidentified-agent"]
                    and recipe.get("confidence", 0) >= 0.3
                ):
                    # 写入指纹库：证据必须绑定本次调查启动时冻结的实例
                    validated = validate_recipe(recipe, run_dir / 'evidence',
                                                target={'pid': pid, 'create_time': create_time})
                    evidence = validated['evidence']
                    hook_evidence_supported = validated['hook_evidence_supported']
                    # Re-check identity/build after the investigation, before committing.
                    current = analyzer.analyze(pid)
                    if current.get('create_time') != struct.get('create_time') or current.get('compatibility') != struct.get('compatibility'):
                        raise ValueError('Target changed during investigation')
                    entry = matcher.remember_verified(struct, recipe, evidence, elapsed_ms, source='goose')
                    target = {'pid': pid, 'create_time': create_time}
                    onboarding_plan = onboarding.record_investigation(
                        target, struct, recipe, evidence, entry, match_status='miss',
                        source='goose', run_dir=str(run_dir))
                    install_result, verification_result = _auto_execute_onboarding(
                        onboarding_plan, target)
                    hook_text = "接入点 proposed/unverified（未核对接入点证据）"
                    onboarding_details = {
                        'onboarding': {
                            'plan': onboarding_plan,
                            'install': install_result,
                            'verification': verification_result,
                        }
                    }
                    _record_investigation_result(instance_id, pid, create_time, "succeeded",
                                                 f"已保存候选配方 {entry.get('id')}（{hook_text}），未安装／未验证"
                                                 if install_result is None
                                                 else f"已保存候选配方 {entry.get('id')}（{hook_text}），接入链状态: {install_result.get('status')}",
                                                 run_dir, {**onboarding_details, "lifecycle": deepcopy(lifecycle),
                                                           **_partial_result_details(run_dir, {"pid": pid, "create_time": create_time})})
                    if autonomous_pipeline.enabled():
                        autonomous_pipeline.save(instance_id, 'hook', run_dir, fingerprint_id=entry.get('id'),
                            install_status=(install_result or {}).get('status', 'not_installed'))
                    print(f"[Analyst] 候选配方写入指纹库! Agent={entry.get('name')}, HarnessID={entry.get('id')}")
                else:
                    _record_investigation_result(instance_id, pid, create_time, "failed", f"Recipe 未通过质量门禁 (identity={identity}, confidence={recipe.get('confidence')})", run_dir,
                                                 {"lifecycle": deepcopy(lifecycle), **_partial_result_details(run_dir, {"pid": pid, "create_time": create_time})})
                    _record_onboarding_outcome(instance_id, pid, create_time, "failed", f"Recipe 未通过质量门禁 (identity={identity}, confidence={recipe.get('confidence')})", run_dir, struct.get("compatibility"))
                    print(f"[Analyst] 逆向目标在调查期间已退出或不可达 (identity={identity}, confidence={recipe.get('confidence')})，放弃生成无效指纹。")
            else:
                stderr_tail = ''
                try:
                    err_txt = err_path.read_text(encoding='utf-8', errors='replace').strip().splitlines()
                    if err_txt:
                        stderr_tail = redact('\n'.join(err_txt[-6:]))[-600:]
                except OSError:
                    pass
                if lifecycle.get('end_reason') == 'tool_transport_closed':
                    reason = 'MCP 调查工具进程已退出；已停止无效重试并保留证据，可续查'
                elif lifecycle.get("timed_out"):
                    reason = "Goose 调查超时；已保留进度，可基于隔离证据继续调查"
                elif lifecycle.get("end_reason") == "cancelled":
                    reason = "Goose 调查已取消；已保留进度，可基于隔离证据继续调查"
                else:
                    reason = f"Goose 未产生有效 Recipe (returncode={cp.returncode})"
                if stderr_tail:
                    reason += "；stderr 尾部: " + stderr_tail
                result_status = ("timeout" if lifecycle.get("timed_out") else
                                 "cancelled" if lifecycle.get("end_reason") == "cancelled" else "failed")
                _record_investigation_result(instance_id, pid, create_time, result_status, reason, run_dir,
                                             {"lifecycle": deepcopy(lifecycle), **_partial_result_details(run_dir, {"pid": pid, "create_time": create_time})})
                _record_onboarding_outcome(instance_id, pid, create_time, "failed", reason, run_dir, struct.get("compatibility"))
                print(f"[Analyst] 接管完成但未产生有效 Recipe; returncode={cp.returncode}; stderr_tail={stderr_tail[:200]}", file=sys.stderr)

        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            if lifecycle and lifecycle.get("status") == "running":
                finish_lifecycle("failed", "exception", error_type=type(exc).__name__)
            _record_investigation_result(instance_id, pid, create_time, "failed", reason, run_dir,
                                         {"lifecycle": deepcopy(lifecycle) if lifecycle else {},
                                          **_partial_result_details(run_dir, {"pid": pid, "create_time": create_time})})
            _record_onboarding_outcome(instance_id, pid, create_time, "failed", reason, run_dir, struct.get("compatibility"))
            print(f"[Analyst Error PID={pid}] {exc}", file=sys.stderr)
        finally:
            with INVESTIGATION_LOCK:
                phase_complete = autonomous_pipeline.enabled() and phase == 'assets' and autonomous_pipeline.read(instance_id).get('assets')
                INVESTIGATION_RETRY_AT[instance_id] = now() + (0 if phase_complete else GOOSE_RETRY_COOLDOWN_S)
                INVESTIGATING_INSTANCES.pop(instance_id, None)
            with STATE_LOCK:
                SCAN_STATE["active_investigations"].pop(pid, None)
            # 立即更新指纹库统计与扫描结果
            try:
                fp_path = matcher.db_path()
                if fp_path.exists():
                    fp_data = json.loads(fp_path.read_text(encoding="utf-8"))
                    with STATE_LOCK:
                        SCAN_STATE["fingerprints_count"] = len(fp_data.get("fingerprints", []))
            except Exception:
                pass
    finally:
        # 并发槽位由调用方（run_autonomous_investigation / _dispatch_worker）负责释放。
        pass


def _enqueue_investigation(pid: int, instance_id: str, create_time: float | None,
                           force: bool = False) -> str:
    """入队一次调查请求。返回 enqueued/duplicate/full。"""
    with INVESTIGATION_LOCK:
        if instance_id in INVESTIGATING_INSTANCES or instance_id in INVESTIGATION_QUEUED:
            return "duplicate"
        previous = INVESTIGATION_RESULTS.get(instance_id)
        if not autonomous_pipeline.enabled() and not force and previous is not None and previous.get("status") in ("succeeded", "reused", "assets_collected", "partial"):
            return "duplicate"  # 已有成功调查，防止重复历史调查
        if not force and now() < INVESTIGATION_RETRY_AT.get(instance_id, 0):
            return "duplicate"
        task = {"pid": pid, "create_time": create_time, "instance_id": instance_id, "force": force}
        if force:
            INVESTIGATION_QUEUE.appendleft(task)
        elif len(INVESTIGATION_QUEUE) >= INVESTIGATION_QUEUE_MAX:
            return "full"
        else:
            INVESTIGATION_QUEUE.append(task)
        INVESTIGATION_QUEUED[instance_id] = {"enqueued_at": iso(), "force": force}
    with _QUEUE_COND:
        _QUEUE_COND.notify_all()
    return "enqueued"


def _needs_asset_followup(*, is_matched: bool, investigation_result: dict[str, Any] | None,
                         assets_phase: bool) -> bool:
    """Whether an exact-matched instance still needs its own asset snapshot.

    An exact fingerprint legitimately reuses the Hook recipe, so it must not
    trigger a fresh Hook investigation. It does not follow that a *new* instance
    already has assets: the checkpoint belongs to an instance, not to a family.
    This only declares the intent to schedule; `_enqueue_investigation` still
    refuses an active/queued run, an existing assets_collected/partial result and
    the retry cooldown, so a repeat investigation cannot actually start.
    """
    if not assets_phase or not is_matched:
        return False
    return (investigation_result or {}).get("status") not in (
        "assets_collected", "partial", "succeeded", "reused")


def _schedule_investigation(pid: int, struct: dict[str, Any], force: bool = False) -> str:
    """扫描循环的公平调度入口：统一进入 FIFO 队列，由调度线程按槽位启动。"""
    if not AUTONOMOUS_ANALYSIS_ENABLED:
        return "disabled"
    instance_id, create_time = _freeze_instance(pid, struct)
    outcome = _enqueue_investigation(pid, instance_id, create_time, force=force)
    if outcome == "full":
        _record_investigation_result(instance_id, pid, create_time, "deferred",
                                     "调查队列已满，稍后自动重试")
        with INVESTIGATION_LOCK:
            INVESTIGATION_RETRY_AT[instance_id] = now() + GOOSE_RETRY_COOLDOWN_S
    return outcome


def _investigation_target_alive(task: dict[str, Any]) -> bool:
    pid, ct = task.get("pid"), task.get("create_time")
    if pid is None:
        return False
    try:
        return abs(float(psutil.Process(int(pid)).create_time()) - float(ct)) <= 1e-3
    except (psutil.Error, TypeError, ValueError):
        return False


def _dispatch_next_investigation(block: bool = True) -> bool:
    """弹出队首任务；有槽位即启动，无槽位按 FIFO 等待，不丢弃不标失败。"""
    with _QUEUE_COND:
        if block:
            while not INVESTIGATION_QUEUE:
                _QUEUE_COND.wait()
        elif not INVESTIGATION_QUEUE:
            return False
    with INVESTIGATION_LOCK:
        task = INVESTIGATION_QUEUE.popleft() if INVESTIGATION_QUEUE else None
        if task is not None:
            INVESTIGATION_QUEUED.pop(task["instance_id"], None)
    if task is None:
        return False
    if not _investigation_target_alive(task):
        _record_investigation_result(task["instance_id"], task["pid"], task.get("create_time"),
                                     "failed", "实例已退出，排队调查取消")
        return True
    if block:
        INVESTIGATION_SEMAPHORE.acquire()
    elif not INVESTIGATION_SEMAPHORE.acquire(blocking=False):
        # 无槽位：任务放回队首等待，保持可见排队状态。
        with INVESTIGATION_LOCK:
            INVESTIGATION_QUEUE.appendleft(task)
            INVESTIGATION_QUEUED[task["instance_id"]] = {"enqueued_at": iso(),
                                                         "force": task.get("force", False)}
        return False
    threading.Thread(target=_dispatch_worker, args=(task,), daemon=True,
                     name=f"analyst-dispatch-{task['pid']}").start()
    return True


def _dispatch_worker(task: dict[str, Any]) -> None:
    try:
        pid = task["pid"]
        instance_id = task["instance_id"]
        with INVESTIGATION_LOCK:
            if instance_id in INVESTIGATING_INSTANCES:
                return
            INVESTIGATING_INSTANCES[instance_id] = task.get("create_time")
        try:
            struct = analyzer.analyze(pid)
        except Exception as exc:
            print(f"[Analyze Error PID={pid}] {exc}", file=sys.stderr)
            struct = {"pid": pid, "create_time": task.get("create_time")}
        resume_from = None
        if autonomous_pipeline.enabled() and not task.get('force'):
            previous = INVESTIGATION_RESULTS.get(instance_id, {})
            expected_phase = autonomous_pipeline.next_phase(instance_id,
                exact=matcher.classify(struct).get('status') == 'exact')
            if previous.get('status') in ('failed', 'timeout', 'succeeded') and previous.get('lifecycle', {}).get('phase') == expected_phase:
                saved_run = previous.get('log_dir')
                if saved_run and Path(saved_run).is_dir():
                    resume_from = Path(saved_run)
        _execute_investigation(pid, struct, instance_id, task.get("create_time"),
                               force=task.get("force", False), resume_from=resume_from)
    except Exception as exc:
        print(f"[Investigation Dispatcher] {type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        INVESTIGATION_SEMAPHORE.release()


def _investigation_dispatcher_loop() -> None:
    while True:
        try:
            _dispatch_next_investigation(block=True)
        except Exception as exc:
            print(f"[Investigation Dispatcher Loop] {type(exc).__name__}: {exc}", file=sys.stderr)
            time.sleep(1)


def get_last_semantic_message(pid: int, exe_name: str, cmdline: str) -> dict:
    """获取该 Agent 进程真实关联的语义消息，绝不把其他进程或历史残留瞎挂上去"""
    # 查找专属绑定到该 PID 的会话流
    pid_stream = Path(os.environ.get("ASG_EVENT_DIR", str(ROOT / "artifacts" / "stage1" / "dashboard" / "events"))) / f"stream_{pid}.jsonl"
    if pid_stream.exists():
        try:
            lines = [l.strip() for l in pid_stream.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
            if lines:
                last_obj = json.loads(lines[-1])
                return {
                    "source": f"pid_{pid}",
                    "event_type": last_obj.get("event_type", "unknown"),
                    "ts": last_obj.get("ts", ""),
                    "detail": last_obj.get("detail") or last_obj.get("blocks") or last_obj.get("prompt") or last_obj.get("call") or "N/A"
                }
        except Exception:
            pass

    return {
        "source": "none",
        "event_type": "未监听",
        "ts": time.strftime("%H:%M:%S"),
        "detail": "当前进程尚未挂接流式 Sink 或暂无新消息"
    }


SCAN_EXECUTION_LOCK = threading.Lock()


def scan_agents_once():
    # Button and periodic scans share one run; never overlap installation work.
    if not SCAN_EXECUTION_LOCK.acquire(blocking=False):
        return
    with STATE_LOCK:
        SCAN_STATE['scanning'] = True
        SCAN_STATE.pop('scan_error', None)
    try:
        _scan_agents_once()
    except Exception as exc:
        with STATE_LOCK:
            SCAN_STATE['scan_error'] = str(exc)
        raise
    finally:
        with STATE_LOCK:
            SCAN_STATE['scanning'] = False
        SCAN_EXECUTION_LOCK.release()


def _scan_agents_once():
    """执行一次完整的 OS 级扫描；调用频率由 ASG_SCAN_INTERVAL 控制。"""
    global SCAN_STATE
    policies = load_policies()
    sensor = Sensor(policies)
    threshold = int(policies.get("agent_score_threshold", 50))
    observation_snapshot = read_observation_snapshot()
    
    found_agents = []
    
    # 遍历进程并收集初筛候选
    candidates = []
    snapshot = {}
    identities = {}
    discovery_audit = {}
    for proc in psutil.process_iter(["pid", "ppid", "exe", "name", "cmdline", "create_time"]):
        try:
            pinfo = proc.info
            pid = pinfo.get("pid")
            name = pinfo.get("name") or ""
            cmdline = pinfo.get("cmdline") or []
            discovery_audit[pid] = {"pid": pid, "name": name, "status": "excluded", "reason": "缺少可读取的启动信息或系统进程"}
            snapshot[pid] = pinfo
            identities[pid] = identify(pinfo, sensor.identity_catalog)

            if not cmdline or pid == os.getpid() or name.lower() in ["system", "registry", "smss.exe"]:
                continue

            w = sensor.wrap_pid(pid)
            score, reasons = sensor.agent_score(w)
            discovery = runtime_discovery_candidate(pinfo, proc) if 0 <= score < threshold else {}
            pinfo['discovery_evidence'] = discovery
            discovery_audit[pid] = {'pid': pid, 'name': name, 'status': 'candidate' if score >= threshold or discovery else 'below_threshold', 'score': score, 'reason': '; '.join(reasons) or '当前快照没有足够 Agent 线索', 'evidence': discovery}
            if score >= threshold or discovery:
                identities[pid] = identities[pid] or metadata_identity(pinfo)
                if discovery:
                    reasons = [*reasons, discovery['reason']]
                candidates.append((proc, pinfo, score, reasons))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 进程树去重 (Root Deduplication):
    # 若候选集合中存在 A 包含子进程 B (A 是 B 的父级且两者均在候选集合中)，
    # 优先由根节点/主编排进程 A 代表 Agent 实体进行纳管与逆向，避免一个 Agent 派生的子进程反复在看板盖楼
    # Strong behavior leads first; newly opened opaque apps precede old desktop
    # residents without changing their role or confidence score.
    candidates.sort(key=lambda row: (-row[2], -(row[1].get('create_time') or 0)))
    candidate_pids = {pinfo["pid"] for _, pinfo, _, _ in candidates}
    process_groups = ownership(snapshot, identities, candidate_pids)
    sub_worker_pids = candidate_pids - set(process_groups)

    # Historical bindings are kept for audit, but a scan only needs live
    # candidate instances.  Limiting the read prevents old multi-megabyte logs
    # from delaying every manual scan and every page refresh.
    current_instance_ids = {
        f"{pinfo['pid']}:{pinfo.get('create_time')}"
        for _, pinfo, _, _ in candidates
    }
    observation_snapshots = _observation_registry().snapshots(current_instance_ids)
    # Preserve the legacy receiver until it has been migrated to file bindings.
    legacy_id = observation_snapshot.get('instance_id')
    if legacy_id and legacy_id not in observation_snapshots:
        observation_snapshots[legacy_id] = observation_snapshot

    for proc, pinfo, score, reasons in candidates:
        pid = pinfo["pid"]
        # 如果当前候选只是其他已纳管 Agent 的派生子进程，将其归为子 Worker 忽略，聚焦根编排进程
        if pid in sub_worker_pids:
            discovery_audit[pid].update(status='associated', reason='归入候选父进程', owner=next((root for root, members in process_groups.items() if pid in members), None))
            continue

        try:
            name = pinfo.get("name") or ""
            cmdline = pinfo.get("cmdline") or []
            struct = {}
            try:
                struct = analyzer.analyze(pid)
            except Exception as e:
                print(f"[Analyze Error PID={pid}] {e}", file=sys.stderr)
                struct = {
                    "pid": pid,
                    "exe": name,
                    "runtime": "native",
                    "argv_shape": [(x if str(x).startswith("-") else "<value>") for x in cmdline],
                    "config_dirs": []
                }
            
            match_result = matcher.classify(struct)
            matched_fp = match_result['entry'] if match_result['status'] == 'exact' else None
            match_ms = match_result['match_ms']
            onboarding_view = onboarding.view_for_instance(
                f"{pid}:{pinfo.get('create_time')}", struct, match_result)
            if match_result['status'] == 'exact' and matched_fp:
                # classify() remains pure; the scanner explicitly records a reusable exact hit.
                try:
                    matcher.record_hit(matched_fp.get('id', ''))
                    if onboarding_view.get('last_event') is None:
                        onboarding.record_reuse(
                            {'pid': pid, 'create_time': pinfo.get('create_time')},
                            onboarding_view.get('plan', {}))
                        onboarding_view = onboarding.view_for_instance(
                            f"{pid}:{pinfo.get('create_time')}", struct, match_result)
                    install_result, verification_result = _auto_execute_onboarding(
                        onboarding_view.get("plan", {}),
                        {"pid": pid, "create_time": pinfo.get("create_time")},
                        onboarding_view,
                    )
                    if install_result is not None:
                        onboarding_view["install"] = install_result
                    if verification_result is not None:
                        onboarding_view["verification"] = verification_result
                except (OSError, ValueError, TypeError):
                    onboarding_view = {
                        'status': 'experience_unavailable',
                        'reason': '经验持久化失败，未改变匹配结果',
                        'match_status': match_result.get('status'),
                        'plan': onboarding.plan_from_match(struct, match_result),
                    }
            # A detached service may lose its stdout reader. Diagnostic output
            # must not make the outer exception handler silently drop a candidate.
            try:
                print(f"[Scan Match] PID={pid}, name={name}, matched={bool(matched_fp)}, harness={(matched_fp.get('id') if matched_fp else None)}")
            except OSError:
                pass
            
            # 计算真实的展示名称 (如果已经识别/适配过，展示 Agent 真实身份，而非 .exe)
            display_name = name
            is_matched = bool(matched_fp)
            local_identity = identities.get(pid, {})
            if local_identity:
                display_name = local_identity['name']
            elif matched_fp:
                fp_name = matched_fp.get("name")
                if fp_name and fp_name != "unknown-runtime":
                    display_name = f"{fp_name} ({name})"
            else:
                # 尚未命中指纹库：未逆向接管前展示为待调查状态
                display_name = f"未知 Agent ({name})"

            instance_id = f"{pid}:{pinfo.get('create_time')}"
            instance_observation = observation_snapshots.get(instance_id, {'status': 'not_configured'})
            is_investigating = False
            with INVESTIGATION_LOCK:
                is_investigating = (instance_id in INVESTIGATING_INSTANCES)
                investigation_result = dict(INVESTIGATION_RESULTS.get(instance_id, {}))
            if investigation_result.get('create_time') != pinfo.get('create_time'):
                investigation_result = {}
                run_root = Path(os.environ.get('ASG_RUN_DIR', str(ROOT / 'artifacts' / 'stage1' / 'dashboard')))
                for prior in sorted(run_root.glob(f'pid_{pid}_*/result.json'), reverse=True)[:32]:
                    try:
                        saved = json.loads(prior.read_text())
                        if saved.get('instance_id') == instance_id or                            (saved.get('create_time') is not None and saved.get('create_time') == pinfo.get('create_time')):
                            investigation_result = saved
                            with INVESTIGATION_LOCK: INVESTIGATION_RESULTS[instance_id] = saved
                            break
                    except (OSError, ValueError):
                        continue
            if not AUTONOMOUS_ANALYSIS_ENABLED and not investigation_result:
                investigation_result = {"status": "disabled", "message": "自动深度分析已禁用 (ASG_AUTONOMOUS_ANALYSIS=0)"}

            with STATE_LOCK:
                active_run = dict(SCAN_STATE['active_investigations'].get(pid, {}))
            if active_run.get('instance_id') and active_run['instance_id'] != instance_id:
                active_run = {}  # 复用 PID 的旧运行态不进入新实例
            log_dir = active_run.get('log_dir') or investigation_result.get('log_dir')
            partial_findings = _load_partial_findings(
                Path(log_dir) if log_dir else None,
                {"pid": pid, "create_time": pinfo.get('create_time')},
            )
            # An empty new investigation must not hide same-instance saved findings.
            if partial_findings is None:
                findings_root = Path(os.environ.get("ASG_RUN_DIR", str(ROOT / "artifacts" / "stage1" / "dashboard")))
                saved_paths = sorted(findings_root.glob(f"pid_{pid}_*/investigation_findings.json"),
                                     key=lambda path: path.stat().st_mtime, reverse=True)
                for saved_path in saved_paths[:64]:
                    saved_findings = _load_partial_findings(saved_path.parent,
                        {"pid": pid, "create_time": pinfo.get('create_time')})
                    if saved_findings and saved_findings.get("findings"):
                        partial_findings = saved_findings
                        break
            if autonomous_pipeline.enabled():
                asset_run = autonomous_pipeline.read(instance_id).get('assets', {}).get('run_dir')
                baseline = _load_partial_findings(Path(asset_run), {'pid': pid, 'create_time': pinfo.get('create_time')}) if asset_run else None
                if baseline:
                    merged = deepcopy(baseline)
                    current_findings = (partial_findings or {}).get('findings', {})
                    if current_findings.get('identity'):
                        merged.setdefault('findings', {})['identity'] = current_findings['identity']
                    assets = merged.setdefault('findings', {}).setdefault('assets', {})
                    for key, value in current_findings.get('assets', {}).items():
                        if key not in assets or value.get('status') in ('collected', 'empty'):
                            assets[key] = value
                    partial_findings = merged
            partial_identity = ((partial_findings or {}).get("findings") or {}).get("identity") \
                if isinstance(partial_findings, dict) else None
            if (not local_identity and not matched_fp and isinstance(partial_identity, dict)
                    and partial_identity.get("status") == "identified"):
                partial_value = partial_identity.get("value") or {}
                partial_name = partial_value.get("name") if isinstance(partial_value, dict) else None
                if partial_name and partial_name != "unidentified-agent":
                    display_name = f"Goose 调查: {partial_name} ({name})"
            # 角色三态分类：候选/待确认 ≠ 确认 Agent；基础设施不进入 Agent Hook 计划。
            agent_classification = _agent_classification(partial_identity)
            if agent_classification['status'] == 'infrastructure':
                onboarding_view['plan'] = {
                    'status': 'not_applicable_infrastructure',
                    'reason': '角色调查判定为基础设施（roles=%s），不生成 Agent Hook 计划'
                              % ','.join(agent_classification['roles']),
                }
                onboarding_view['install'] = None
                onboarding_view['verification'] = None

            recipe_obj = matched_fp.get("hook_recipe", {}) if matched_fp else {}
            adapter_info = {
                "matched": is_matched,
                "investigating": is_investigating,
                "investigation": investigation_result,
                "partial_findings": partial_findings,
                "investigated_identity": partial_identity,
                "agent_classification": agent_classification,
                "match_ms": match_ms,
                "harness_id": matched_fp.get("id") if matched_fp else "unregistered",
                "behavioral_class": (recipe_obj.get("match_features", {}).get("behavioral_class")) if matched_fp else "unknown-runtime",
                "observation": (recipe_obj.get("observation")) if matched_fp else ("⚡ Goose 正在非交互式自主逆向接管中..." if is_investigating else "未挂接 (需要首次逆向)"),
                "hook": (recipe_obj.get("hook")) if matched_fp else "未挂接",
                # Goose 深度逆向推导的治理全景档案
                "host_platform": recipe_obj.get("host_platform") or "未知",
                "workspace_cwd": recipe_obj.get("workspace_cwd") or struct.get("cwd", ""),
                "parsed_config": recipe_obj.get("parsed_config") or {},
                "model_routing": recipe_obj.get("model_routing") or {},
                "registered_tools_and_mcp": recipe_obj.get("registered_tools_and_mcp") or [],
                "system_prompt_rules": recipe_obj.get("system_prompt_rules") or [],
                "network_surface": recipe_obj.get("network_surface") or {},
                "child_executions": recipe_obj.get("child_executions") or [],
                "memory_context": recipe_obj.get("memory_context") or {},
            }
            
            adapter_info.update(presentation(AUTONOMOUS_ANALYSIS_ENABLED,
                is_investigating, investigation_result, metadata_identity(pinfo) or local_identity))
            adapter_info['investigating'] = adapter_info['investigation']['status'] == 'running'
            adapter_info['match_status'] = match_result['status']
            adapter_info['match_reason'] = match_result['reason']
            adapter_info['fingerprint_revision'] = matched_fp.get('revision') if matched_fp else None
            adapter_info['recipe_status'] = 'structure_validated_hook_unverified' if matched_fp else 'not_available'
            adapter_info['historical_recipe'] = recipe_obj
            adapter_info['onboarding'] = onboarding_view
            adapter_info['observation_evidence'] = observation_for_instance(
                instance_observation, pid, pinfo.get('create_time'))
            for field in adapter_info['assets']:
                adapter_info[field] = None
            if log_dir:
                for ev_path in sorted((Path(log_dir) / 'evidence').glob('ev-*.json')):
                    try:
                        ev = json.loads(ev_path.read_text())
                        result = ev.get('result', {})
                        if ev.get('tool') == 'get_target_context': result = result.get('local_evidence', {})
                        if ev.get('tool') in ('get_target_context', 'inspect_config_surface') and not ev.get('error'):
                            collections = result.get('assets', {})
                            for value in collections.values(): value['source'] = ev['evidence_id'] + ': ' + value.get('source', '')
                            adapter_info['assets'].update({k: v for k, v in presentation(True, collections=collections)['assets'].items() if k in collections})
                    except (OSError, ValueError):
                        continue
            partial_assets = _partial_asset_collections(partial_findings)
            for field, record in partial_assets.items():
                current = adapter_info['assets'].get(field) or {}
                if current.get('status') not in ('collected', 'empty') or record.get('status') in ('collected', 'empty'):
                    adapter_info['assets'][field] = presentation(True, collections={field: record})['assets'][field]
            adapter_info['hook'] = '未安装' 
            adapter_info['observation'] = '未接入／未验证'
            learned_installation = onboarding_view.get('install') or {}
            if learned_installation.get('source') == 'supervisor_approved_candidate':
                learned_verification = onboarding_view.get('verification') or {}
                adapter_info['hook_state'] = learned_hook_state(
                    {'pid': pid, 'create_time': pinfo.get('create_time')},
                    learned_installation, learned_verification)
                adapter_info['hook'] = adapter_info['hook_state']['label']
                # This is a bounded acceptance snapshot, not a live health or
                # blocking claim. No raw generated payloads/nonces are exposed.
                adapter_info['learned_observation'] = {
                    'status': adapter_info['hook_state']['status'],
                    'events': (learned_verification.get('events', [])[-30:]
                               if learned_verification.get('target') == {'pid': pid, 'create_time': pinfo.get('create_time')}
                               else []),
                    'paired_calls': adapter_info['hook_state'].get('paired_calls', []),
                    'label': '当前实例的工具事件记录',
                    'blocking': 'not_implemented',
                }
                if adapter_info['hook_state']['status'] == 'observing':
                    adapter_info['assets']['child_executions'] = {
                        'status': 'collected', 'label': '已采集',
                        'value': {'events': adapter_info['learned_observation']['events'],
                                  'paired_calls': adapter_info['learned_observation']['paired_calls']},
                        'source': 'learned_events.verify: PID/create_time + fresh nonce + paired callbacks',
                        'message': '当前实例真实工具调用验收快照；不是历史全量记录或持续健康保证',
                    }
                    adapter_info['sink_state'] = {
                        'status': 'observation_verified',
                        'label': '工具事件观测已验收；语义阻断未实现',
                    }
            live_observation = adapter_info.get('observation_evidence', {})
            if live_observation.get('status') == 'observed' and instance_observation.get('paired_calls'):
                adapter_info['assets']['child_executions'] = {
                    'status': 'collected', 'label': '持续读取事件文件',
                    'value': {'events': instance_observation.get('recent_events', []),
                              'paired_calls': instance_observation.get('paired_calls', []),
                              'live_file_source': True,
                              'last_event_time': instance_observation.get('last_event_time'),
                              'target_alive': instance_observation.get('target_alive')},
                    'source': 'external_config: target-bound JSONL',
                    'message': '动态读取 Hook 日志',
                }
            if instance_observation.get('target_alive') is True and instance_observation.get('health', {}).get('loaded_observed'):
                health = instance_observation['health']
                observing = health.get('status') == 'observing'
                adapter_info['hook_state'] = {'status': 'observing' if observing else 'loaded',
                    'label': '工具事件已接通' if observing else '已加载，等待工具活动',
                    'verified': True, 'source': 'instance_event_file'}
            elif learned_installation.get('installed') or learned_installation.get('status') in ('installed', 'bound', 'rebound', 'activation_rebound', 'installed_no_observation'):
                adapter_info['hook_state'] = {'status': 'installed_pending_activation',
                    'label': '已安装，等待加载事件（可能需下次启动）', 'verified': False}
            # Card summary must agree with the instance-bound hook_state below it:
            # an observed live load event outranks an unregistered-adapter plan label,
            # otherwise the UI shows "已收到当前实例事件" next to "未安装" (GAP 状态一致项).
            _hs = adapter_info.get('hook_state') or {}
            if _hs.get('verified') and _hs.get('status') in ('loaded', 'observing'):
                adapter_info['hook'] = _hs['label']
                if adapter_info.get('observation') in (None, '未接入／未验证'):
                    adapter_info['observation'] = ('已观测（当前实例事件流）' if _hs['status'] == 'observing'
                                                   else '已加载（当前实例事件流）')
            if autonomous_pipeline.enabled():
                adapter_info['pipeline'] = {'next_phase': autonomous_pipeline.next_phase(instance_id, exact=is_matched),
                    'checkpoints': autonomous_pipeline.read(instance_id)}
            last_msg = get_last_semantic_message(pid, name, " ".join(cmdline))
            if instance_observation.get('target_alive') is True and instance_observation.get('recent_events'):
                latest = instance_observation['recent_events'][-1]
                last_msg = {'source': 'live_hook', 'event_type': latest['event_type'],
                    'ts': time.strftime('%H:%M:%S', time.localtime(latest['timestamp'])),
                    'detail': {'pid': pid, 'paired_tools': [x['tool_name'] for x in instance_observation.get('paired_calls', [])[-10:]]}}
                adapter_info['sink_state'] = {'status': 'connected', 'label': '本实例 Hook 事件流已接通'}
            
            found_agents.append({
                "pid": pid,
                "instance_id": f"{pid}:{pinfo.get('create_time')}",
                "identity": local_identity,
                "process_pids": sorted(process_groups.get(pid, [pid])),
                "name": display_name,
                "raw_exe": name,
                "score": score,
                "reasons": reasons,
                "cmdline": name + " · 参数内容未展示",
                "discovery_evidence": pinfo.get('discovery_evidence', {}),
                "adapter": adapter_info,
                "last_message": last_msg,
                "uptime_sec": int(time.time() - (pinfo.get("create_time") or time.time()))
            })

            # Exact builds use deterministic reuse. Missing instance assets do not
            # justify another model call; failures/upgrades use the pipeline below.
            needs_deep_governance = _needs_asset_followup(
                is_matched=is_matched, investigation_result=investigation_result,
                assets_phase=os.environ.get('ASG_INVESTIGATION_PHASE', '').strip() == 'assets')
            with INVESTIGATION_LOCK:
                retry_ready = now() >= INVESTIGATION_RETRY_AT.get(instance_id, 0)
            target_allowed = not os.environ.get('ASG_ANALYST_TARGET_PID') or str(pid) == os.environ['ASG_ANALYST_TARGET_PID']
            if autonomous_pipeline.enabled():
                needs_deep_governance = autonomous_pipeline.next_phase(instance_id, exact=is_matched) is not None
                should_investigate = needs_deep_governance
            else:
                should_investigate = not is_matched
            if target_allowed and AUTONOMOUS_ANALYSIS_ENABLED and should_investigate and not is_investigating and retry_ready:
                _schedule_investigation(pid, struct)

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    # 同类型 Agent 聚合 (Group by Agent Identity / Harness):
    # 将属于同一 Harness 或同名 Agent 的多个运行实例合并为一个治理卡片，避免同类型进程分散刷屏
    grouped_agents = {}
    for a in found_agents:
        # 聚类 Key：优先以命中的 harness_id 聚合；若未命中则以推导身份或执行入口聚合
        hid = a.get("adapter", {}).get("harness_id")
        if a.get('identity'):
            group_key = 'identity:' + a['identity']['id']
        elif hid and hid != "unregistered":
            group_key = f"harness:{hid}"
        else:
            group_key = f"unidentified:{a['pid']}"

        group_key = a['instance_id']  # Do not borrow another instance's investigation state.
        if group_key not in grouped_agents:
            # 建立主卡片，记录实例集合
            a_copy = dict(a)
            a_copy["instances"] = [a["pid"]]
            a_copy["all_pids"] = list(a['process_pids'])
            grouped_agents[group_key] = a_copy
        else:
            # 聚合到已有同类卡片中
            main_card = grouped_agents[group_key]
            main_card["instances"].append(a["pid"])
            main_card["all_pids"] = sorted(set(main_card['all_pids']) | set(a['process_pids']))
            # 保留更高的画像分与最新的消息
            if a["score"] > main_card["score"]:
                main_card["score"] = a["score"]
                main_card["reasons"] = a["reasons"]
            if a.get("adapter", {}).get("matched") and not main_card.get("adapter", {}).get("matched"):
                main_card["adapter"] = a["adapter"]
                main_card["name"] = a["name"]

    final_agents = list(grouped_agents.values())
    final_agents.sort(key=lambda item: item.get('adapter', {}).get('hook_state', {}).get('status') != 'observing')
    _auto_bind_observations(final_agents)

    # 更新指纹库统计
    fp_count = 0
    fp_path = matcher.db_path()
    if fp_path.exists():
        try:
            fp_data = json.loads(fp_path.read_text(encoding="utf-8"))
            fp_count = len(fp_data.get("fingerprints", []))
        except Exception:
            pass

    with STATE_LOCK:
        SCAN_STATE["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        SCAN_STATE["scan_count"] += 1
        SCAN_STATE["agents"] = final_agents
        SCAN_STATE["discovery_audit"] = list(discovery_audit.values())
        SCAN_STATE["fingerprints_count"] = fp_count
        SCAN_STATE['observation_instances'] = observation_snapshots
        SCAN_STATE["observation_adapter"] = {
            key: value for key, value in observation_snapshot.items()
            if key != "capabilities"
        }


def background_scanner_loop():
    while True:
        try:
            scan_agents_once()
        except Exception as e:
            print(f"[Scanner Error] {e}", file=sys.stderr)
        with SCAN_TIMER_CONDITION:
            revision = SCAN_TIMER_REVISION
            deadline = time.monotonic() + SCAN_INTERVAL_S
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                SCAN_TIMER_CONDITION.wait(remaining)
                if revision != SCAN_TIMER_REVISION:
                    revision = SCAN_TIMER_REVISION
                    deadline = time.monotonic() + SCAN_INTERVAL_S


def _query_pid(path: str) -> int | None:
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("pid", [])
        return int(values[0]) if values else None
    except (TypeError, ValueError):
        return None


def _handle_hook_control_request(handler: BaseHTTPRequestHandler) -> bool:
    """Give the generic local Hook control API first refusal on its routes.

    The control module is optional for older isolated runs.  Importing it here
    keeps the dashboard usable with those runs while allowing the sibling
    control implementation to own authentication and response details.
    """
    try:
        from runtime.hook_control import handle_request
    except ImportError:
        return False
    return bool(handle_request(handler))


def _onboarding_target(pid: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    with STATE_LOCK:
        agent = next((deepcopy(a) for a in SCAN_STATE.get("agents", []) if a.get("pid") == pid), None)
    if not agent:
        return None, None
    instance = agent.get("instance_id", "")
    try:
        _, ct = instance.split(":", 1)
        target = {"pid": int(pid), "create_time": float(ct)}
    except (AttributeError, TypeError, ValueError):
        return agent, None
    return agent, target


def _update_onboarding_view(pid: int, install_result: dict[str, Any] | None = None,
                            verification_result: dict[str, Any] | None = None) -> None:
    with STATE_LOCK:
        for agent in SCAN_STATE.get("agents", []):
            if agent.get("pid") != pid:
                continue
            view = agent.setdefault("adapter", {}).setdefault("onboarding", {})
            if install_result is not None:
                view["install"] = deepcopy(install_result)
            if verification_result is not None:
                view["verification"] = deepcopy(verification_result)
            return


_GENERIC_INSTALL_STATUSES = {
    'installed', 'bound', 'rebound', 'activation_rebound',
    'installed_no_observation', 'installed_pending_activation', 'already_installed',
}


def _verify_installation(install_result: dict[str, Any] | None,
                         target: dict[str, Any]) -> dict[str, Any]:
    """Use the generic persisted binding verifier for generated installs."""
    result = install_result or {}
    if autonomous_pipeline.enabled() and result.get('status') in _GENERIC_INSTALL_STATUSES:
        instance_id = f"{target['pid']}:{target['create_time']}"
        return autonomous_pipeline.verify_installed(instance_id)
    return onboarding.verify_activation(result, target)


def _auto_execute_onboarding(plan: dict[str, Any], target: dict[str, Any],
                             current: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Run the same authorization/idempotency/verification path for exact and new plans."""
    if not ONBOARDING_AUTO_INSTALL_ENABLED or plan.get("status") != "plan_pending_authorization":
        return None, None
    existing = current.get("install") if isinstance(current, dict) else None
    if isinstance(existing, dict) and existing.get("status") != "pending_authorization":
        # A later scan must not reinstall or repeatedly append verification events for
        # the same instance. A new PID+create_time has a new view and is checked anew.
        return None, None
    authorization = onboarding.authorization_from_environment(plan.get("workspace"))
    if isinstance(existing, dict) and not authorization.get("approved"):
        return None, None
    install_result = onboarding.execute_install(plan, target, authorization)
    onboarding.record_transition(target, 'automatic_install_result', {'plan': plan, 'install': install_result})
    _wire_observe_config(install_result, target)
    verification_result = None
    if install_result.get("status") in _GENERIC_INSTALL_STATUSES:
        verification_result = _verify_installation(install_result, target)
    return install_result, verification_result


HTML_PAGE = (ROOT / "web" / "dashboard.html").read_text(encoding="utf-8")


_INSTALLED_ONBOARDING = ('installed', 'bound', 'rebound', 'activation_rebound', 'installed_no_observation')
_ACTIVE_HOOK_STATE = ('observing', 'loaded', 'installed_pending_activation')


def _native_trust_view():
    """Cached native trust report; the probe itself runs in a background thread."""
    from runtime import native_trust
    try:
        native_trust.ensure_async()
        return native_trust.current()
    except Exception as exc:  # a probe failure must not break the dashboard
        return {'status': 'unavailable', 'reason': type(exc).__name__}


def _serving_view():
    from runtime import hook_service
    try:
        return hook_service.servings()
    except Exception as exc:
        return {'status': 'not_running', 'reason': type(exc).__name__}


def _io_hint(observation):
    """Project cheap partial IO evidence from the already-read live window."""
    from runtime import event_vocabulary
    from runtime.io_acceptance import KINDS
    observed = {
        event_vocabulary.canonical(item.get('event_type')) or item.get('event_type')
        for item in (observation or {}).get('recent_events', []) if isinstance(item, dict)
    }
    if not observed.intersection(KINDS):
        return None
    return {
        'status': 'gaps',
        'capabilities': [
            {'event': kind, 'observed': kind in observed, 'count': None}
            for kind in KINDS
        ],
        'end_to_end_verified': False,
        'source': 'current_observation_window',
    }


def attach_capability(snapshot, *, io_by_instance=None, trust=None, serving=None):
    """Attach the per-instance capability ladder.

    ``io_by_instance`` lets a single-instance call add input/output coverage that
    would be too expensive to read for every agent on each five-second refresh.
    """
    from runtime import capability, hook_service
    trust = trust if trust is not None else _native_trust_view()
    serving = serving if serving is not None else _serving_view()
    registry = snapshot.get('observation_instances') or {}
    # refresh_hook_observations already read these bindings. Re-reading every
    # historical log here doubles I/O for each browser poll during recovery.
    covered = set(registry) if serving.get('status') == 'running' else set()
    for agent in snapshot.get('agents', []):
        adapter = agent.get('adapter') or {}
        agent['adapter'] = adapter
        instance_id = agent.get('instance_id')
        observation = registry.get(instance_id) if instance_id else None
        observation = observation if isinstance(observation, dict) else {}
        onboarding = adapter.get('onboarding') if isinstance(adapter.get('onboarding'), dict) else {}
        hook_state = adapter.get('hook_state') if isinstance(adapter.get('hook_state'), dict) else {}
        plan = onboarding.get('plan') if isinstance(onboarding.get('plan'), dict) else {}
        installed = (onboarding.get('installed') is True
                     or onboarding.get('status') in _INSTALLED_ONBOARDING
                     or hook_state.get('status') in _ACTIVE_HOOK_STATE)
        recipe = adapter.get('historical_recipe')
        fingerprint = ({'id': adapter.get('harness_id'), 'revision': adapter.get('fingerprint_revision'),
                        'hook_recipe': recipe} if isinstance(recipe, dict) and recipe else None)
        acceptance = adapter.get('control_acceptance')
        create_time = adapter.get('create_time')
        if create_time is None and isinstance(instance_id, str) and ':' in instance_id:
            try:
                create_time = float(instance_id.split(':', 1)[1])
            except ValueError:
                create_time = None
        serving_view = dict(serving)
        if instance_id and serving_view.get('status') == 'running':
            serving_view['covers_instance'] = instance_id in covered
        # 每个通过状态附带其证据被记录的时间，页面据此显示“验证时间”。
        evidence_times = {}
        investigation_view = adapter.get('investigation')
        if isinstance(investigation_view, dict) and investigation_view.get('updated_at'):
            evidence_times['investigated'] = investigation_view.get('updated_at')
        if observation.get('last_event_time'):
            evidence_times['loaded'] = observation.get('last_event_time')
            evidence_times['observing'] = observation.get('last_event_time')
        if isinstance(trust, dict) and trust.get('probed_at'):
            evidence_times['trusted'] = trust.get('probed_at')
        if isinstance(acceptance, dict):
            ctl_at = acceptance.get('verified_at') or acceptance.get('updated_at')
            if ctl_at:
                evidence_times['controlling'] = ctl_at
        if isinstance(serving_view, dict) and serving_view.get('started_at'):
            evidence_times['serving'] = serving_view.get('started_at')
        for _key, _value in list(evidence_times.items()):
            if isinstance(_value, (int, float)):
                evidence_times[_key] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(_value))
        try:
            proc = psutil.Process(agent.get('pid'))
            process_scope = {'exe': proc.exe(), 'cwd': proc.cwd()}
        except (psutil.Error, OSError):
            process_scope = {}
        instance_trust = capability.scoped_trust(trust, process_scope)
        explicit_io = (io_by_instance or {}).get(instance_id)
        adapter['capability'] = capability.build(
            {'pid': agent.get('pid'), 'create_time': create_time},
            investigation=adapter.get('investigation'),
            fingerprint=fingerprint,
            install={'status': onboarding.get('status'), 'installed': installed,
                     'plan': plan or None,
                     'files': plan.get('files') if isinstance(plan.get('files'), list) else None},
            verify={'hook_loaded': hook_state.get('status') in ('loaded', 'observing'),
                    'target_alive': observation.get('target_alive')},
            observation=observation,
            io=explicit_io if isinstance(explicit_io, dict) else _io_hint(observation),
            control={'verifications': [acceptance]} if isinstance(acceptance, dict) else None,
            native_trust=instance_trust,
            serving=serving_view, times=evidence_times)


def _enriched_snapshot():
    """The same view /api/state serves, reused by the single-instance endpoints."""
    with STATE_LOCK:
        snapshot = deepcopy(SCAN_STATE)
    with INVESTIGATION_LOCK:
        for agent in snapshot['agents']:
            adapter = agent['adapter']
            inst = agent.get('instance_id') or f"{agent['pid']}:{adapter.get('create_time')}"
            adapter['investigation'] = presentation(AUTONOMOUS_ANALYSIS_ENABLED,
                inst in INVESTIGATING_INSTANCES, INVESTIGATION_RESULTS.get(inst))['investigation']
            adapter['investigating'] = adapter['investigation']['status'] == 'running'
            queued = INVESTIGATION_QUEUED.get(inst)
            if queued is not None and adapter['investigation'].get('status') not in ('running',):
                position = None
                for index, task in enumerate(INVESTIGATION_QUEUE):
                    if task.get("instance_id") == inst:
                        position = index + 1
                        break
                adapter['investigation'] = {
                    'status': 'queued', 'label': '排队中',
                    'message': '已加入调查队列' + (f'，第 {position} 位' if position else ''),
                    'source': 'investigation_scheduler', 'can_request': False,
                    'enqueued_at': queued.get('enqueued_at'), 'queue_position': position,
                }
                adapter['investigating'] = False
    # Investigation completion and asset refresh must not wait for process scans.
    from runtime import local_assets
    for agent in snapshot['agents']:
        adapter = agent['adapter']
        iid = agent.get('instance_id', '')
        try:
            created = float(iid.split(':', 1)[1])
        except (ValueError, IndexError):
            continue
        result = adapter.get('investigation') or {}
        directory = result.get('log_dir')
        partial = _load_partial_findings(Path(directory), {'pid': agent['pid'], 'create_time': created}) if directory else None
        if partial and partial.get('findings'):
            identity = partial['findings'].get('identity')
            adapter.update(partial_findings=partial, investigated_identity=identity,
                           agent_classification=_agent_classification(identity))
            adapter.setdefault('assets', {}).update(_partial_asset_collections(partial))
        local = local_assets.current(agent['pid'], created, ((adapter.get('onboarding') or {}).get('plan') or {}).get('workspace'))
        adapter['asset_refresh'] = {k: v for k, v in local.items() if k != 'assets'}
        for field, value in local.get('assets', {}).items():
            if value.get('status') == 'collected':
                adapter.setdefault('assets', {})[field] = value
    refresh_hook_observations(snapshot)
    trust, serving = _native_trust_view(), _serving_view()
    attach_capability(snapshot, trust=trust, serving=serving)
    from runtime import hook_approval
    hook_approval.attach(snapshot)
    from integrations.soc_inventory import codex_trust
    codex_trust.attach(snapshot)
    snapshot['native_trust'] = trust
    snapshot['hook_runtime'] = serving
    return snapshot


def refresh_hook_observations(snapshot):
    """Refresh events independently of the user's process discovery interval."""
    current_instance_ids = {
        agent.get('instance_id') for agent in snapshot.get('agents', [])
        if agent.get('instance_id')
    }
    observations = _observation_registry().snapshots(current_instance_ids)
    snapshot['observation_instances'] = observations
    for agent in snapshot.get('agents', []):
        iid=agent.get('instance_id'); obs=observations.get(iid)
        if not obs: continue
        adapter=agent['adapter']
        from runtime.hook_acceptance import snapshots as acceptance_snapshots
        acceptance=next((v for v in acceptance_snapshots() if v.get('current') and v['target']['pid']==agent['pid']),None)
        if acceptance: adapter['control_acceptance']=acceptance
        adapter['observation_evidence']=observation_for_instance(obs,agent['pid'],float(iid.split(':',1)[1]))
        if obs.get('target_alive') and (obs.get('health') or {}).get('loaded_observed'):
            observing=bool(obs.get('paired_calls'))
            adapter['hook_state']={'status':'observing' if observing else 'loaded',
                'label':'工具事件已接通' if observing else '已加载，等待工具活动','verified':True,'source':'instance_event_file'}
            adapter['hook'] = adapter['hook_state']['label']
            if adapter.get('observation') == '未接入／未验证':
                adapter['observation'] = ('已观测（当前实例事件流）' if observing else '已加载（当前实例事件流）')
            adapter.setdefault('assets',{})['child_executions']={'status':'collected','label':'持续读取事件文件',
                'value':{'events':obs.get('recent_events',[]),'paired_calls':obs.get('paired_calls',[]),
                         'live_file_source':True,'last_event_time':obs.get('last_event_time')},'source':'instance_event_file'}
        if obs.get('target_alive') and obs.get('recent_events'):
            event=obs['recent_events'][-1]
            agent['last_message']={'source':'live_hook','event_type':event['event_type'],
                'ts':time.strftime('%H:%M:%S',time.localtime(event['timestamp'])),'detail':event}
        if autonomous_pipeline.enabled():
            autonomous_pipeline.verify_installed(iid)
            adapter.setdefault('pipeline',{})['checkpoints']=autonomous_pipeline.read(iid)


class MonitorHandler(BaseHTTPRequestHandler):
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError):
            # Browsers cancel stale polling requests during reload/navigation.
            # That is normal client behaviour and must not flood service logs.
            return
    def _serve_hook_data(self, parsed: urllib.parse.SplitResult, *, download: bool = False) -> None:
        """Serve the bounded raw Hook view or its JSON download."""
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        kwargs = {
            "run_dir": os.environ.get("ASG_RUN_DIR", str(ROOT / "artifacts" / "stage1" / "dashboard")),
            "pid": query.get("pid", [None])[0],
            "create_time": query.get("create_time", [None])[0],
            "instance_id": query.get("instance_id", [None])[0] or None,
            "since": query.get("since", [None])[0] or None,
            "until": query.get("until", [None])[0] or None,
            "limit": query.get("limit", [None])[0],
            "max_bytes": query.get("max_bytes", [None])[0],
        }
        payload = hook_data.snapshot(**kwargs)
        code = 400 if payload.get("status") == "invalid_request" else 200
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", "attachment; filename=hook-data.json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if _handle_hook_control_request(self):
            return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path in ("/api/hook-data", "/api/hook-events"):
            self._serve_hook_data(parsed)
            return
        if parsed.path in ("/api/hook-data/download", "/api/hook-events/download"):
            self._serve_hook_data(parsed, download=True)
            return
        if self.path == '/api/model-settings':
            from runtime.model_settings import public
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(json.dumps(public(), ensure_ascii=False).encode())
            return
        if urllib.parse.urlparse(self.path).path == "/api/investigation/activity":
            from runtime.investigation_activity import snapshot as activity_snapshot
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                pid = int(query.get("pid", [""])[0])
                _, target = _onboarding_target(pid)
                if target is None:
                    raise LookupError("target_not_in_scan")
                payload = activity_snapshot(
                    Path(os.environ.get("ASG_RUN_DIR", str(ROOT / "artifacts" / "stage1" / "dashboard"))),
                    pid, target["create_time"], query.get("run_id", [None])[0],
                    int(query.get("limit", ["40"])[0]))
                code = 200
            except (ValueError, TypeError):
                code, payload = 400, {"error": "invalid_activity_request"}
            except LookupError:
                code, payload = 404, {"error": "activity_target_or_run_not_found"}
            except OSError:
                code, payload = 503, {"error": "activity_store_unavailable"}
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            return
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif self.path == "/api/state":
            data = json.dumps(_enriched_snapshot(), ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
        elif self.path == "/api/native-trust":
            payload = _native_trust_view()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif urllib.parse.urlparse(self.path).path == "/api/capability":
            from runtime import hook_data as _hook_data
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                pid = int(query.get("pid", [""])[0])
            except ValueError:
                pid = None
            snapshot = _enriched_snapshot()
            agent = next((a for a in snapshot.get('agents', []) if a.get('pid') == pid), None)
            io_map = {}
            if agent is not None:
                instance_id = agent.get('instance_id')
                target = (agent.get('adapter') or {}).get('capability', {}).get('target') or {}
                raw = _hook_data.snapshot(run_dir=os.environ.get('ASG_RUN_DIR'),
                                          pid=pid, create_time=target.get('create_time'),
                                          limit=200, max_bytes=2 * 1024 * 1024)
                io_map[instance_id] = (raw.get('coverage') or {}).get('io_acceptance')
                attach_capability(snapshot, io_by_instance=io_map)
                agent = next((a for a in snapshot.get('agents', []) if a.get('pid') == pid), None)
            if agent is None:
                code, payload = 404, {"error": "target_not_in_scan"}
            else:
                code = 200
                payload = {"pid": pid, "instance_id": agent.get('instance_id'),
                           "capability": (agent.get('adapter') or {}).get('capability'),
                           "native_trust": snapshot.get('native_trust'),
                           "hook_runtime": snapshot.get('hook_runtime')}
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif urllib.parse.urlparse(self.path).path == "/api/recipe-bundle/export":
            from runtime import recipe_bundle as _bundles
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fingerprint_id = (query.get("fingerprint_id", [""])[0] or "").strip()
            try:
                bundle = _bundles.export_bundle(fingerprint_id, note=query.get("note", [None])[0])
                code = 200
            except LookupError as exc:
                code, bundle = 404, {"error": str(exc)}
            except (ValueError, OSError) as exc:
                code, bundle = 400, {"error": str(exc)}
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Disposition",
                             "attachment; filename=recipe-bundle-%s.json" % (fingerprint_id or "bundle"))
            self.end_headers()
            self.wfile.write(json.dumps(bundle, ensure_ascii=False, indent=2).encode("utf-8"))
        elif self.path == "/api/fingerprints":
            try:
                payload = {"fingerprints": matcher.load().get("fingerprints", [])}
                code = 200
            except (OSError, ValueError) as exc:
                payload = {"status": "failed", "message": "指纹库读取失败", "error_type": type(exc).__name__}
                code = 500
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/onboarding"):
            pid = _query_pid(self.path)
            agent, target = _onboarding_target(pid) if pid else (None, None)
            if not agent or not target:
                payload = {"status": "invalid_request", "reason": "需要当前扫描中的有效 pid"}
                code = 400
            else:
                payload = {
                    "status": "ok",
                    "target": target,
                    "onboarding": agent.get("adapter", {}).get("onboarding", {}),
                    "source": "current_scan_and_experience_store",
                }
                code = 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        from runtime.otlp_ingest import handle as handle_otlp
        if handle_otlp(self): return
        if _handle_hook_control_request(self):
            return
        if self.path == '/api/native-trust/refresh':
            from runtime import native_trust
            started = native_trust.refresh_async()
            payload = {'started': started, 'report': native_trust.current()}
            self.send_response(200)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
            return
        if self.path == '/api/recipe-bundle/import':
            from runtime import recipe_bundle as _bundles
            try:
                length = int(self.headers.get('Content-Length', '0'))
                body = json.loads(self.rfile.read(length) or b'{}')
                if not isinstance(body, dict):
                    raise ValueError('object required')
                bundle = body.get('bundle')
                observed = body.get('observed_build')
                if observed is None and body.get('exe'):
                    from runtime import compatibility as _build
                    observed = _build.observe(str(body['exe']),
                                              [str(body['exe'])] + [str(a) for a in (body.get('argv') or [])],
                                              str(body.get('cwd') or os.getcwd()))
                payload = _bundles.import_bundle(bundle, observed_build=observed,
                                                 workspace=body.get('workspace'))
                code = 200
            except (ValueError, TypeError, OSError) as exc:
                code, payload = 400, {'error': str(exc)}
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
            return
        if self.path == '/api/model-settings':
            from runtime.model_settings import start
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384:
                    raise ValueError('无效配置请求')
                payload = start(json.loads(self.rfile.read(length)))
                code = 202
            except (ValueError, TypeError) as exc:
                code, payload = 400, {'error': str(exc)}
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())
        elif self.path == '/api/scan-interval':
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024:
                    raise ValueError('无效请求')
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError('无效请求')
                value = set_scan_interval(data.get('scan_interval'))
                code, payload = 200, {'scan_interval': value}
            except (ValueError, TypeError) as exc:
                code, payload = 400, {'error': str(exc)}
            except OSError:
                code, payload = 500, {'error': '保存失败，请重试'}
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode())
        elif self.path == "/api/scan":
            threading.Thread(target=scan_agents_once, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "scanning"}')
        elif self.path.startswith("/api/onboarding/execute"):
            pid = _query_pid(self.path)
            agent, target = _onboarding_target(pid) if pid else (None, None)
            if not agent or not target:
                payload = {"status": "invalid_request", "reason": "需要当前扫描中的有效 pid"}
                code = 400
            else:
                plan = agent.get("adapter", {}).get("onboarding", {}).get("plan", {})
                auth = onboarding.authorization_from_environment(plan.get("workspace"))
                install_result = onboarding.execute_install(plan, target, auth)
                _wire_observe_config(install_result, target)
                verification_result = None
                if install_result.get("status") in _GENERIC_INSTALL_STATUSES:
                    verification_result = _verify_installation(install_result, target)
                _update_onboarding_view(pid, install_result, verification_result)
                payload = {"status": "ok", "plan": plan, "install": install_result,
                           "verification": verification_result}
                code = 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/onboarding/verify"):
            pid = _query_pid(self.path)
            agent, target = _onboarding_target(pid) if pid else (None, None)
            if not agent or not target:
                payload = {"status": "invalid_request", "reason": "需要当前扫描中的有效 pid"}
                code = 400
            else:
                install_result = agent.get("adapter", {}).get("onboarding", {}).get("install")
                if not isinstance(install_result, dict):
                    try:
                        saved = onboarding.instance_state(
                            onboarding.make_instance_id(target["pid"], target["create_time"])) or {}
                        install_result = saved.get("install")
                    except (OSError, ValueError):
                        install_result = None
                verification_result = _verify_installation(install_result, target)
                _update_onboarding_view(pid, verification_result=verification_result)
                payload = {"status": "ok", "verification": verification_result}
                code = 200
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/reinvestigate/continue"):
            pid = _query_pid(self.path)
            if not AUTONOMOUS_ANALYSIS_ENABLED:
                payload = presentation(False)['investigation']
                code = 503
            else:
                agent, target = _onboarding_target(pid) if pid else (None, None)
                previous = _latest_investigation_run(pid, target.get("create_time") if target else None) \
                    if target else None
                if not agent or not target:
                    payload = {"status": "invalid_request", "reason": "需要当前扫描中的有效 pid"}
                    code = 400
                elif previous is None:
                    payload = {"status": "no_resume_source", "reason": "没有同一实例的隔离调查结果可续查"}
                    code = 409
                else:
                    struct = {}
                    try:
                        struct = analyzer.analyze(pid)
                    except Exception as exc:
                        payload = {"status": "analysis_context_failed", "reason": type(exc).__name__}
                        code = 500
                    else:
                        threading.Thread(
                            target=run_autonomous_investigation,
                            args=(pid, struct, True, previous),
                            name=f"analyst-worker-continue-{pid}",
                            daemon=True,
                        ).start()
                        payload = {"status": "continuing", "target": target, "resume_from": str(previous)}
                        code = 202
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/reinvestigate/cancel"):
            pid = _query_pid(self.path)
            agent, target = _onboarding_target(pid) if pid else (None, None)
            instance_id = f"{pid}:{target.get('create_time')}" if target else None
            with INVESTIGATION_LOCK:
                active = bool(instance_id and instance_id in ACTIVE_ANALYST_PROCESSES)
                if active:
                    INVESTIGATION_CANCEL_REQUESTS.add(instance_id)
            payload = {"status": "cancelling", "instance_id": instance_id} if active else {
                "status": "not_running", "reason": "当前实例没有可取消的 Goose 调查"
            }
            self.send_response(202 if active else 409)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        elif self.path.startswith("/api/reinvestigate"):
            # 允许手动触发重新让 Goose 深度逆向某个 PID
            import urllib.parse
            query = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(query)
            pid = int(params.get("pid", [0])[0])
            if pid and AUTONOMOUS_ANALYSIS_ENABLED:
                struct = {}
                try:
                    struct = analyzer.analyze(pid)
                except Exception:
                    pass
                threading.Thread(
                    target=run_autonomous_investigation,
                    args=(pid, struct, True),
                    name=f"analyst-worker-manual-{pid}",
                    daemon=True
                ).start()
            self.send_response(200 if AUTONOMOUS_ANALYSIS_ENABLED else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            status = json.dumps({'status': 'reinvestigating'} if AUTONOMOUS_ANALYSIS_ENABLED else
                                presentation(False)['investigation'], ensure_ascii=False).encode('utf-8')
            self.wfile.write(status)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    host = os.environ.get("ASG_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = _env_int("ASG_PORT", 8080)
    print("[Monitor] 后台执行首次进程环境扫描...")
    
    t = threading.Thread(target=background_scanner_loop, name="scanner-thread", daemon=True)
    t.start()
    threading.Thread(target=_investigation_dispatcher_loop, name="investigation-dispatcher", daemon=True).start()
    print(f"[Monitor] {SCAN_INTERVAL_S}s 扫描与调查引擎已启动")
    if not AUTONOMOUS_ANALYSIS_ENABLED:
        print("[Monitor] 自动深度分析已禁用 (ASG_AUTONOMOUS_ANALYSIS=0)")
    
    server = ThreadingHTTPServer((host, port), MonitorHandler)
    print(f"[Monitor] Web 界面已就绪: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
