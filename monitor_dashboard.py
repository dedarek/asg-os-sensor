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
from runtime import analyzer, matcher, onboarding, investigation_findings
from runtime.status import presentation, DISABLED_REASON
from runtime.learned_presentation import hook_state as learned_hook_state
from runtime.tool_transport_health import ToolTransportHealth
from runtime.recipe_validation import validate as validate_recipe
from runtime.identity import identify, ownership, metadata_identity
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
MAX_ANALYSTS = _env_int("ASG_MAX_ANALYSTS", 2)
GOOSE_MAX_TURNS = _env_optional_positive_int("ASG_GOOSE_MAX_TURNS")
GOOSE_MAX_TOOL_REPETITIONS = _env_optional_positive_int("ASG_GOOSE_MAX_TOOL_REPETITIONS")
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
                "automatic_continuation": False,
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
                "--recipe", str(RECIPE),
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
            env.update(ASG_TARGET_PID=str(pid), ASG_TARGET_CREATE_TIME=str(create_time),
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
            if (os.environ.get('ASG_INVESTIGATION_PHASE') == 'assets'
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
                INVESTIGATION_RETRY_AT[instance_id] = now() + GOOSE_RETRY_COOLDOWN_S
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
        if not force and previous is not None and previous.get("status") in ("succeeded", "reused", "assets_collected", "partial"):
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
        _execute_investigation(pid, struct, instance_id, task.get("create_time"),
                               force=task.get("force", False))
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


def scan_agents_once():
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
    for proc in psutil.process_iter(["pid", "ppid", "exe", "name", "cmdline", "create_time"]):
        try:
            pinfo = proc.info
            pid = pinfo.get("pid")
            name = pinfo.get("name") or ""
            cmdline = pinfo.get("cmdline") or []
            snapshot[pid] = pinfo
            identities[pid] = identify(pinfo, sensor.identity_catalog)

            if not cmdline or pid == os.getpid() or name.lower() in ["system", "registry", "smss.exe"]:
                continue

            w = sensor.wrap_pid(pid)
            score, reasons = sensor.agent_score(w)
            if score >= threshold:
                identities[pid] = identities[pid] or metadata_identity(pinfo)
                candidates.append((proc, pinfo, score, reasons))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 进程树去重 (Root Deduplication):
    # 若候选集合中存在 A 包含子进程 B (A 是 B 的父级且两者均在候选集合中)，
    # 优先由根节点/主编排进程 A 代表 Agent 实体进行纳管与逆向，避免一个 Agent 派生的子进程反复在看板盖楼
    candidate_pids = {pinfo["pid"] for _, pinfo, _, _ in candidates}
    process_groups = ownership(snapshot, identities, candidate_pids)
    sub_worker_pids = candidate_pids - set(process_groups)

    for proc, pinfo, score, reasons in candidates:
        pid = pinfo["pid"]
        # 如果当前候选只是其他已纳管 Agent 的派生子进程，将其归为子 Worker 忽略，聚焦根编排进程
        if pid in sub_worker_pids:
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
                observation_snapshot, pid, pinfo.get('create_time'))
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
                    'label': '当前实例的独立验收记录（非持续健康保证）',
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
            if OBSERVE_CONFIG and live_observation.get('status') == 'observed':
                adapter_info['assets']['child_executions'] = {
                    'status': 'collected', 'label': '持续读取事件文件',
                    'value': {'events': observation_snapshot.get('recent_events', []),
                              'paired_calls': observation_snapshot.get('paired_calls', []),
                              'live_file_source': True,
                              'last_event_time': observation_snapshot.get('last_event_time'),
                              'target_alive': observation_snapshot.get('target_alive')},
                    'source': 'external_config: target-bound JSONL',
                    'message': '动态读取真实Hook日志；接收映射由人工配置，不代表自主接入完成或阻断能力',
                }
            last_msg = get_last_semantic_message(pid, name, " ".join(cmdline))
            
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
                "adapter": adapter_info,
                "last_message": last_msg,
                "uptime_sec": int(time.time() - (pinfo.get("create_time") or time.time()))
            })

            # 自动接入闭环：若发现陌生 Agent 或尚未拥有深度治理全景档案的已匹配 Agent，立即在后台拉起 Goose 进行自主逆向！
            # exact 命中复用 Hook 配方，但资产快照属于实例而非家族：assets 阶段里
            # 已匹配但本实例尚无资产 checkpoint 时仍需调度；重复由队列去重拦截。
            needs_deep_governance = _needs_asset_followup(
                is_matched=is_matched, investigation_result=investigation_result,
                assets_phase=os.environ.get('ASG_INVESTIGATION_PHASE', '').strip() == 'assets')
            with INVESTIGATION_LOCK:
                retry_ready = now() >= INVESTIGATION_RETRY_AT.get(instance_id, 0)
            target_allowed = not os.environ.get('ASG_ANALYST_TARGET_PID') or str(pid) == os.environ['ASG_ANALYST_TARGET_PID']
            if target_allowed and AUTONOMOUS_ANALYSIS_ENABLED and (not is_matched or needs_deep_governance) and not is_investigating and retry_ready:
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
        SCAN_STATE["fingerprints_count"] = fp_count
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
        time.sleep(SCAN_INTERVAL_S)


def _query_pid(path: str) -> int | None:
    try:
        values = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).get("pid", [])
        return int(values[0]) if values else None
    except (TypeError, ValueError):
        return None


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
    _wire_observe_config(install_result, target)
    verification_result = None
    if install_result.get("status") in ("installed_pending_activation", "already_installed"):
        verification_result = onboarding.verify_activation(install_result, target)
    return install_result, verification_result


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ASG 运行时治理 · 实时 Agent 监控看板</title>
<style>
  :root {
    --bg: #090d16;
    --card-bg: #111827;
    --card-border: #1f293d;
    --card-hover-border: #334155;
    --text-primary: #f8fafc;
    --text-secondary: #cbd5e1;
    --text-muted: #94a3b8;
    --text-dim: #64748b;
    --accent: #38bdf8;
    --accent-glow: rgba(56, 189, 248, 0.15);
    --green: #34d399;
    --green-bg: rgba(52, 211, 153, 0.12);
    --green-border: rgba(52, 211, 153, 0.35);
    --amber: #fbbf24;
    --amber-bg: rgba(251, 191, 36, 0.12);
    --amber-border: rgba(251, 191, 36, 0.35);
    --indigo: #818cf8;
    --indigo-bg: rgba(129, 140, 248, 0.12);
    --indigo-border: rgba(129, 140, 248, 0.35);
    --code-bg: #070b12;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; }
  body { background: var(--bg); color: var(--text-primary); padding: 24px; line-height: 1.5; min-height: 100vh; }
  
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid var(--card-border); padding-bottom: 16px; flex-wrap: wrap; gap: 16px; }
  .title { font-size: 20px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; letter-spacing: -0.3px; }
  .pulse { width: 10px; height: 10px; border-radius: 50%; background: var(--green); box-shadow: 0 0 10px var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; } }
  
  .meta-bar { display: flex; align-items: center; gap: 18px; font-size: 13px; color: var(--text-muted); flex-wrap: wrap; }
  .meta-item b { color: var(--accent); font-family: monospace; font-size: 13px; }
  .refresh-btn { 
    background: linear-gradient(180deg, #1e293b 0%, #0f172a 100%); 
    border: 1px solid #334155; 
    color: #e2e8f0; 
    padding: 7px 16px; 
    border-radius: 6px; 
    cursor: pointer; 
    font-size: 12px; 
    font-weight: 600;
    box-shadow: 0 2px 4px rgba(0,0,0,0.3);
    transition: all 0.2s ease; 
  }
  .refresh-btn:hover { 
    background: linear-gradient(180deg, #334155 0%, #1e293b 100%); 
    border-color: #475569; 
    color: #fff; 
    box-shadow: 0 0 8px rgba(56, 189, 248, 0.25);
  }
  
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 22px; align-items: stretch; }
  .card { 
    background: var(--card-bg); 
    border: 1px solid var(--card-border); 
    border-radius: 12px; 
    padding: 20px; 
    display: flex; 
    flex-direction: column; 
    gap: 16px; 
    box-shadow: 0 6px 20px rgba(0,0,0,0.35); 
    transition: border-color 0.2s ease, transform 0.2s ease;
    height: 100%;
  }
  .card:hover { border-color: var(--card-hover-border); }
  
  .card-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; min-height: 56px; }
  .agent-name { font-size: 15px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; line-height: 1.3; }
  .pid-tag { font-size: 11px; background: #0f172a; color: var(--accent); padding: 2px 8px; border-radius: 4px; border: 1px solid #1e293b; font-family: monospace; font-weight: 600; }
  
  .score-badge { font-size: 12px; font-weight: 700; padding: 4px 10px; border-radius: 6px; white-space: nowrap; display: inline-flex; align-items: center; gap: 4px; }
  .score-high { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-border); }
  .score-mid { background: var(--amber-bg); color: var(--amber); border: 1px solid var(--amber-border); }
  .score-low { background: rgba(148, 163, 184, 0.12); color: var(--text-muted); border: 1px solid rgba(148, 163, 184, 0.3); }
  
  .score-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; justify-content: flex-end; max-width: 240px; }
  .score-tag { font-size: 10px; background: #0f172a; color: #94a3b8; padding: 2px 7px; border-radius: 4px; border: 1px solid #1e293b; white-space: nowrap; }
  
  .section-label { font-size: 11px; text-transform: uppercase; color: var(--text-muted); font-weight: 700; margin-bottom: 6px; letter-spacing: 0.6px; }
  
  .cmdline { 
    font-size: 11px; 
    color: #cbd5e1; 
    background: var(--code-bg); 
    padding: 10px 12px; 
    border-radius: 6px; 
    border: 1px solid #162032; 
    overflow-x: auto;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace; 
    line-height: 1.45; 
    max-height: 80px;
    scrollbar-width: thin;
    scrollbar-color: #334155 #0b0f19;
  }
  
  .adapter-box { 
    background: rgba(10, 15, 26, 0.65); 
    border: 1px solid #1c273c; 
    border-radius: 8px; 
    padding: 14px; 
    display: flex; 
    flex-direction: column; 
    gap: 9px; 
    font-size: 12px; 
    flex: 1;
  }
  .adapter-group-title { 
    font-size: 11px; 
    font-weight: 700; 
    color: #94a3b8; 
    display: flex; 
    align-items: center; 
    gap: 6px; 
    margin-top: 5px; 
    padding-bottom: 4px; 
    border-bottom: 1px solid rgba(31, 41, 61, 0.8); 
    letter-spacing: 0.3px;
  }
  .adapter-row { display: grid; grid-template-columns: 115px 1fr; gap: 8px; align-items: start; line-height: 1.45; }
  .adapter-label { color: var(--text-muted); font-size: 11px; font-weight: 500; }
  .adapter-val { color: var(--text-secondary); font-size: 11px; word-break: break-word; }
  
  .adapter-status { color: var(--green); font-weight: 600; display: inline-flex; align-items: center; gap: 4px; }
  .adapter-unmatched { color: var(--amber); }
  .adapter-working { color: var(--indigo); animation: blink 1.5s infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  
  .tool-list { display: flex; flex-direction: column; gap: 6px; }
  .tool-item { 
    background: #090e17; 
    border: 1px solid #1b263b; 
    border-radius: 5px; 
    padding: 7px 9px; 
    font-size: 11px; 
    line-height: 1.45;
  }
  .tool-badge { 
    display: inline-block; 
    background: rgba(56, 189, 248, 0.15); 
    color: #38bdf8; 
    border: 1px solid rgba(56, 189, 248, 0.35); 
    padding: 1px 6px; 
    border-radius: 4px; 
    font-family: monospace; 
    font-size: 10px; 
    font-weight: 600;
    margin-right: 5px;
  }
  .tool-detail { color: #94a3b8; font-family: monospace; font-size: 10.5px; word-break: break-word; margin-top: 4px; }
  
  .stream-badge { 
    display: flex; 
    align-items: center; 
    gap: 8px; 
    font-size: 11px; 
    color: #cbd5e1; 
    padding: 8px 12px; 
    background: rgba(30, 41, 59, 0.45); 
    border-radius: 6px; 
    border: 1px solid #334155; 
    margin-top: auto;
  }
  .stream-badge b { color: #f8fafc; font-weight: 600; }
  
  .msg-box { background: var(--code-bg); border: 1px solid #1e293b; border-radius: 6px; padding: 10px; font-family: monospace; font-size: 11px; margin-top: auto; }
  .msg-header { display: flex; justify-content: space-between; color: var(--accent); margin-bottom: 6px; font-weight: 600; border-bottom: 1px dashed #1e293b; padding-bottom: 4px; }
  .msg-content { color: #e2e8f0; white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; }
  
  .btn-reinvestigate { 
    background: linear-gradient(180deg, rgba(99, 102, 241, 0.22) 0%, rgba(79, 70, 229, 0.12) 100%); 
    border: 1px solid rgba(129, 140, 248, 0.45); 
    color: #a5b4fc; 
    font-size: 11px; 
    font-weight: 600;
    border-radius: 5px; 
    padding: 4px 10px; 
    cursor: pointer; 
    box-shadow: 0 1px 3px rgba(0,0,0,0.3);
    transition: all 0.2s ease; 
  }
  .btn-reinvestigate:hover { 
    background: linear-gradient(180deg, rgba(99, 102, 241, 0.35) 0%, rgba(79, 70, 229, 0.25) 100%); 
    border-color: rgba(165, 180, 252, 0.6);
    color: #fff;
    box-shadow: 0 0 8px rgba(99, 102, 241, 0.35);
  }
  .btn-reinvestigate:disabled { opacity: 0.5; cursor: not-allowed; box-shadow: none; }
  
  .footer { margin-top: 36px; text-align: center; font-size: 12px; color: var(--text-dim); display: flex; justify-content: center; gap: 15px; }

  /* KPI 指标卡片 */
  .kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
  .kpi-card { background: var(--card-bg); border: 1px solid var(--card-border); border-radius: 10px; padding: 14px 18px; display: flex; flex-direction: column; gap: 4px; box-shadow: 0 4px 12px rgba(0,0,0,0.25); }
  .kpi-label { font-size: 11px; text-transform: uppercase; color: var(--text-muted); font-weight: 600; letter-spacing: 0.5px; }
  .kpi-val { font-size: 22px; font-weight: 700; color: #fff; font-family: monospace; display: flex; align-items: baseline; gap: 6px; }
  .kpi-sub { font-size: 11px; color: var(--text-dim); font-weight: normal; }

  /* 抽屉与模态框 */
  .drawer-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.65); backdrop-filter: blur(4px); z-index: 1000; opacity: 0; pointer-events: none; transition: opacity 0.25s ease; }
  .drawer-overlay.active { opacity: 1; pointer-events: auto; }
  .drawer { position: fixed; top: 0; right: -640px; width: 600px; height: 100vh; background: #0f1626; border-left: 1px solid var(--card-border); z-index: 1001; box-shadow: -8px 0 30px rgba(0,0,0,0.6); display: flex; flex-direction: column; transition: transform 0.3s cubic-bezier(0.16, 1, 0.3, 1); }
  .drawer.active { right: 0 !important; transform: translateX(0) !important; }
  .drawer-header { padding: 18px 24px; border-bottom: 1px solid var(--card-border); display: flex; justify-content: space-between; align-items: center; background: rgba(10, 15, 26, 0.8); }
  .drawer-title { font-size: 15px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px; }
  .drawer-close { background: transparent; border: none; color: var(--text-muted); font-size: 20px; cursor: pointer; padding: 4px; line-height: 1; border-radius: 4px; }
  .drawer-close:hover { color: #fff; background: rgba(255,255,255,0.08); }
  .drawer-body { padding: 20px 24px; overflow-y: auto; flex: 1; display: flex; flex-direction: column; gap: 16px; font-size: 12px; }
  
  .fp-item { background: #090e18; border: 1px solid #1c273c; border-radius: 8px; padding: 14px; display: flex; flex-direction: column; gap: 8px; }
  .fp-header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #162032; padding-bottom: 6px; }
  .fp-badge { background: rgba(56, 189, 248, 0.15); color: #38bdf8; border: 1px solid rgba(56, 189, 248, 0.35); padding: 2px 7px; border-radius: 4px; font-family: monospace; font-size: 11px; font-weight: 600; }
  .fp-code { background: #05080f; border: 1px solid #131c2d; border-radius: 6px; padding: 10px; font-family: monospace; font-size: 11px; color: #cbd5e1; max-height: 180px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; }

  .inspect-trigger { cursor: pointer; border-bottom: 1px dotted rgba(56, 189, 248, 0.4); transition: color 0.2s; }
  .inspect-trigger:hover { color: #38bdf8 !important; }

/* Readable summaries keep diagnostic payloads available, but out of the overview. */
.asset-view {font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:#dbe5f0; font-size:13px; line-height:1.6; min-width:0;}
.asset-badge {display:inline-block; padding:1px 7px; border:1px solid #405064; border-radius:4px; color:#b9c7d9; font-size:11px; margin-bottom:5px;}
.asset-found {color:#7dd3c0; border-color:#28584f; background:#132e2b;}
.asset-note {color:#93a4b8; font-size:12px; margin:2px 0 7px;}
.readable-line {display:grid; grid-template-columns:66px minmax(0,1fr); gap:10px; margin:3px 0;}
.readable-line>span {color:#92a4b9; font-weight:400;}
.readable-line>strong {font-weight:500; overflow-wrap:anywhere; color:#e2eaf4;}
.readable-details {margin-top:8px; color:#91a5bf; font-weight:400;}
.readable-details>summary {cursor:pointer; font-size:12px; padding:3px 0; width:fit-content;}
.readable-details>summary:focus-visible {outline:2px solid #38bdf8; outline-offset:3px;}
.readable-details pre {white-space:pre-wrap; overflow-wrap:anywhere; max-height:260px; overflow:auto; background:#0b1320; padding:12px; border:1px solid #2b394e; border-radius:5px; color:#b9c9da; font:11px/1.7 ui-monospace,monospace;}
.adapter-row {align-items:flex-start; padding-top:7px; padding-bottom:7px;}
.adapter-val {min-width:0;}
@media(max-width:600px){.adapter-row{flex-direction:column;gap:6px}.adapter-label{width:auto;flex-basis:auto}.adapter-val{width:100%}}
</style>
</head>
<body>

<div class="header">
  <div class="title">
    <div class="pulse"></div>
    ASG · 进程发现、自主调查与接入验证
  </div>
  <div class="meta-bar">
    <div class="meta-item">扫描周期: <b id="scan-interval">-</b></div>
    <div class="meta-item">已存指纹: <b id="fp-count" style="cursor: pointer; text-decoration: underline;" onclick="openFpDrawer()">-</b></div>
    <div class="meta-item">已扫轮次: <b id="scan-count">-</b></div>
    <div class="meta-item">上次更新: <b id="last-time">-</b></div>
    <div class="meta-item">观测适配: <b id="observe-status">-</b></div>
    <button class="refresh-btn" onclick="triggerScan()">立即扫描</button>
  </div>
</div>

<!-- 全局治理指标 KPI 栏 -->
<div class="kpi-row">
  <div class="kpi-card">
    <div class="kpi-label">确认 Agent（角色证据）</div>
    <div class="kpi-val"><span id="kpi-agent-count">0</span><span class="kpi-sub" id="kpi-instance-count">0 实例活跃</span></div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">指纹匹配比例</div>
    <div class="kpi-val"><span id="kpi-hook-rate" style="color: var(--green);">100%</span><span class="kpi-sub" id="kpi-hook-detail">指纹匹配</span></div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">活跃外联与通信暴露面</div>
    <div class="kpi-val"><span id="kpi-net-count" style="color: var(--accent);">0</span><span class="kpi-sub" id="kpi-net-detail">监听/外联端点</span></div>
  </div>
  <div class="kpi-card">
    <div class="kpi-label">零先验指纹资产库</div>
    <div class="kpi-val"><span id="kpi-fp-total" style="color: var(--indigo); cursor: pointer;" onclick="openFpDrawer()">3</span><span class="kpi-sub" style="cursor: pointer;" onclick="openFpDrawer()">条可演进指纹 ↗</span></div>
  </div>
</div>

<div class="grid" id="agents-grid">
  <div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 60px;">正在进行初次扫描...</div>
</div>

<!-- 指纹库滑出抽屉 -->
<div class="drawer-overlay" id="drawer-overlay" onclick="closeAllDrawers()"></div>
<div class="drawer" id="fp-drawer">
  <div class="drawer-header">
    <div class="drawer-title">📁 零先验 Agent 指纹与候选配方库（未安装／未验证） (fingerprints.json)</div>
    <button class="drawer-close" onclick="closeAllDrawers()">✕</button>
  </div>
  <div class="drawer-body" id="fp-drawer-body">
    <div style="text-align: center; color: var(--text-muted); padding: 40px;">正在加载指纹库...</div>
  </div>
</div>

<!-- 深度透视抽屉 (Deep Inspector) -->
<div class="drawer" id="inspect-drawer">
  <div class="drawer-header">
    <div class="drawer-title" id="inspect-title">🔍 Agent 治理深度透视 (Deep Inspector)</div>
    <button class="drawer-close" onclick="closeAllDrawers()">✕</button>
  </div>
  <div class="drawer-body" id="inspect-drawer-body">
    <div style="text-align: center; color: var(--text-muted); padding: 40px;">正在读取深度全景信息...</div>
  </div>
</div>

<div class="footer">
  <div>OS-Level Zero-Prior Agent Governance</div>
  <div>·</div>
  <div>进程发现与调查状态 · Stage1 尚未闭环</div>
  <div>·</div>
  <div>每 5 秒刷新</div>
</div>

<script>
const readableOpen = new Set();
function rememberReadable(el) {
  if (!el.isConnected) return;
  if (el.open) readableOpen.add(el.dataset.readable);
  else readableOpen.delete(el.dataset.readable);
}
function readableDetails(id, title, data) {
  return `<details class="readable-details" data-readable="${escapeHtml(id)}" ${readableOpen.has(id) ? 'open' : ''} ontoggle="rememberReadable(this)"><summary>${escapeHtml(title)}</summary><pre>${escapeHtml(typeof data === 'string' ? data : JSON.stringify(data, null, 2))}</pre></details>`;
}
function readableLine(label, value) {
  if (value === undefined || value === null || value === '') return '';
  return `<div class="readable-line"><span>${escapeHtml(label)}</span><strong>${escapeHtml(String(value))}</strong></div>`;
}
function assetText(adapter, key) {
  const item = (adapter.assets || {})[key] || {status:'not_collected', label:'尚未采集'};
  const status = item.status || 'not_collected';
  const v = item.value;
  const rows = v && typeof v === 'object' && Array.isArray(v.items) ? v.items : Array.isArray(v) ? v : v && typeof v === 'object' ? [v] : [];
  const labels = {collected:'已查到', empty:'检查范围内未发现', not_collected:'尚未调查', unknown:'待进一步确认', failed:'本项采集失败', unsupported:'当前不支持'};
  let brief = '';
  if (status === 'collected') {
    if (key === 'model_routing') {
      brief = rows.slice(0,3).map(x => readableLine('模型',x.model || x.model_name) + readableLine('网关',x.base_url || x.baseURL || x.endpoint) + readableLine('提供方',x.name || x.provider)).join('');
    } else if (key === 'system_prompt_rules') {
      brief = rows.slice(0,3).map(x => {
        if (typeof x === 'string') return readableLine('规则', x);
        if (x.type === 'runtime_permission_policy') {
          let permissions = [];
          for (const m of String(x.policy || '').matchAll(/permission\\s*:\\s*"([^"]+)"[^}]*?action\\s*:\\s*"([^"]+)"/g)) {
            permissions.push((m[1] === '*' ? '其他操作' : m[1]) + '：' + ({allow:'允许',deny:'拒绝',ask:'需确认'}[m[2]] || m[2]));
          }
          return readableLine('会话权限', permissions.length ? permissions.join('；') : '已记录权限策略') + readableLine('依据','运行日志中的会话记录');
        }
        return readableLine('规则',x.name || x.file || x.summary || x.type);
      }).join('');
    } else if (key === 'child_executions') {
      const events = v && v.events || [];
      const calls = v && v.paired_calls || [];
      brief = readableLine('工具调用',calls.length ? calls.map(x=>x.tool_name || '未命名工具').join('、') + ' · ' + calls.length + ' 次前后配对' : '已记录执行事件') + readableLine('时效',v && v.live_file_source ? '自动读取新事件 · 目标进程存活' : '历史验收快照，非持续健康状态') + (v && v.live_file_source && v.last_event_time ? readableLine('末次事件',new Date(v.last_event_time*1000).toLocaleString()) : '');
    } else if (key === 'network_surface') {
      const ports = rows.filter(x=>x.status === 'LISTEN').map(x=>x.local_port).filter(Boolean);
      brief = readableLine('监听端口',ports.length ? ports.join('、') : '见端点详情') + readableLine('范围','目标进程的瞬时快照');
    } else if (key === 'parsed_config') {
      brief = rows.slice(0,3).map(x => readableLine('配置文件',x.file || x.name)).join('');
    } else {
      brief = rows.slice(0,3).map(x => readableLine('名称',typeof x === 'string' ? x : x.name || x.id || x.summary)).join('');
    }
    if (!brief) brief = readableLine('结果',typeof v === 'string' ? v : '已保存结构化信息，请展开查看');
  }
  const provenance = v && v.configuration_status;
  const note = provenance === 'configured' ? '配置中声明，未验证实际使用' : provenance === 'observed' ? '有运行记录支持，不代表持续生效' : status === 'empty' ? '不代表全局不存在；检查范围见详情' : '';
  const refs = item.evidence_refs || item.sources || [];
  const id = key + ':' + (refs.join(',') || item.source || status);
  const details = status === 'not_collected' ? '' : readableDetails(id,'查看依据与原始详情', {说明:item.message || '',数据:v,证据:refs,来源:item.source || null,范围与限制:item.uncertainty || []});
  return `<div class="asset-view"><span class="asset-badge ${status === 'collected' ? 'asset-found' : ''}">${escapeHtml(labels[status] || item.label || '未知')}</span>${note ? `<div class="asset-note">${escapeHtml(note)}</div>` : ''}${brief}${details}</div>`;
}
function findingText(finding) {
  if (!finding) return '<span class="asset-note">尚未确认身份</span>';
  const v = finding.value || {};
  const refs = finding.evidence_refs || [];
  const name = typeof v === 'object' ? v.name || '未识别' : v;
  return `<div class="asset-view">${readableLine('名称',name)}${readableLine('版本',v.version)}${readableDetails('identity:'+refs.join(','),'查看身份判断依据',finding)}</div>`;
}
function classificationText(cls) {
  if (!cls) return '候选/待确认';
  const roles = Array.isArray(cls.roles) && cls.roles.length ? ' · ' + cls.roles.join('/') : '';
  return escapeHtml((cls.label || '候选/待确认') + roles);
}
const activityCache = {};
const activityOpenPids = new Set();
const activitySelectedRuns = {};
const activityRequests = {};
function escapeActivityText(v) { return escapeHtml(typeof v === 'object' ? JSON.stringify(v) : String(v == null ? '' : v)); }
function renderActivity(pid, data) {
  const box = document.getElementById('activity-body-' + pid);
  if (!box) return;
  if (!data || data.error) {
    box.innerHTML = '<div style="color:#ef4444;padding:8px;">活动读取失败: ' + escapeActivityText(data && (data.reason || data.message || data.error) || '未知原因') + '</div>';
    return;
  }
  let html = '';
  const runs = data.runs || [];
  if (runs.length > 1) {
    html += '<div style="margin-bottom:6px;">历史 run: <select onchange="loadActivity(' + pid + ', this.value)" style="background:#111827;color:#cbd5e1;border:1px solid #1f293d;border-radius:4px;font-size:11px;">' +
      runs.map(r => '<option value="' + escapeActivityText(r.run_id) + '"' + (r.run_id === data.run_id ? ' selected' : '') + '>' + escapeActivityText(r.run_id) + ' (' + escapeActivityText(r.status) + ')</option>').join('') + '</select></div>';
  }
  html += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px;">run: ' + escapeActivityText(data.run_id || '无') +
    ' · 状态: ' + escapeActivityText(data.status) +
    (data.started_at ? ' · 开始: ' + escapeActivityText(data.started_at) : '') +
    (data.ended_at ? ' · 结束: ' + escapeActivityText(data.ended_at) : '') +
    (data.last_activity_at ? ' · 最近活动: ' + escapeActivityText(data.last_activity_at) : '') + '</div>';
  const events = data.events || [];
  if (!events.length) {
    html += '<div style="font-size:11px;color:var(--text-muted);">该 run 暂无已记录事件（等待模型响应或尚未开始）</div>';
  } else {
    html += '<div style="font-size:11px;max-height:180px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:6px;">' +
      events.map(ev => {
        if (ev.type === 'finding_saved') {
          return '<div style="margin-bottom:3px;"><span style="color:#34d399;">✓ finding</span> ' + escapeActivityText(ev.kind) + (ev.asset ? '/' + escapeActivityText(ev.asset) : '') + ' · ' + escapeActivityText(ev.status) + ' · ' + escapeActivityText(ev.ts) + '</div>';
        }
        return '<div style="margin-bottom:3px;"><span style="color:#38bdf8;">🔧</span> ' + escapeActivityText(ev.tool) +
          ' · <span style="color:' + (ev.status === 'succeeded' ? '#34d399' : '#ef4444') + ';">' + escapeActivityText(ev.status) + '</span>' +
          (ev.evidence_id ? ' · <span style="color:var(--text-muted);">' + escapeActivityText(ev.evidence_id) + '</span>' : '') +
          ' · ' + escapeActivityText(ev.ts) + '</div>';
      }).join('') + '</div>';
  }
  if (data.truncated) html += '<div style="font-size:10px;color:var(--text-muted);margin-top:3px;">仅显示最近事件（有界）</div>';
  if (Array.isArray(data.limitations) && data.limitations.length) {
    html += '<div style="font-size:10px;color:var(--text-muted);margin-top:3px;">' + data.limitations.map(l => escapeActivityText(l)).join('；') + '</div>';
  }
  box.innerHTML = html;
}
async function loadActivity(pid, runId) {
  const box = document.getElementById('activity-body-' + pid);
  if (!box) return;
  if (runId !== undefined) activitySelectedRuns[pid] = runId;
  const selectedRun = activitySelectedRuns[pid];
  const requestId = (activityRequests[pid] || 0) + 1;
  activityRequests[pid] = requestId;
  try {
    const url = '/api/investigation/activity?pid=' + pid + (selectedRun ? '&run_id=' + encodeURIComponent(selectedRun) : '');
    const res = await fetch(url);
    const data = await res.json();
    if (activityRequests[pid] !== requestId) return;
    if (!res.ok) { renderActivity(pid, data); return; }
    activityCache[pid] = {data, at: Date.now()};
    renderActivity(pid, data);
  } catch(e) {
    if (activityRequests[pid] === requestId) renderActivity(pid, {error: true, reason: String(e)});
  }
}
function onActivityToggle(pid, el) {
  if (el.open) { activityOpenPids.add(pid); loadActivity(pid); }
  else activityOpenPids.delete(pid);
}
function renderTools(tools) {
  if (!Array.isArray(tools) || tools.length === 0) {
    return '<span style="color: #64748b;">尚未采集</span>';
  }
  return '<div class="tool-list">' + tools.map(t => {
    if (typeof t !== 'string') t = JSON.stringify(t);
    // 识别 MCP 服务特征
    if (t.toLowerCase().includes('mcp')) {
      return `
        <div class="tool-item">
          <div><span class="tool-badge">MCP 服务</span><b style="color: #38bdf8;">${escapeHtml(t.split(':')[0])}</b></div>
          ${t.includes(':') ? `<div class="tool-detail">${escapeHtml(t.substring(t.indexOf(':') + 1).trim())}</div>` : ''}
        </div>
      `;
    }
    // 识别子进程或命令行特征
    if (t.toLowerCase().includes('subprocess') || t.toLowerCase().includes('worker') || t.toLowerCase().includes('child')) {
      return `
        <div class="tool-item">
          <div><span class="tool-badge" style="background: rgba(251, 191, 36, 0.15); color: #fbbf24; border-color: rgba(251, 191, 36, 0.35);">衍生执行</span><span style="color: #f1f5f9;">${escapeHtml(t)}</span></div>
        </div>
      `;
    }
    return `
      <div class="tool-item">
        <span style="color: #cbd5e1;">${escapeHtml(t)}</span>
      </div>
    `;
  }).join('') + '</div>';
}

function escapeHtml(str) {
  if (!str) return '';
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function formatUptime(sec) {
  if (sec < 60) return sec + '秒';
  if (sec < 3600) return Math.floor(sec / 60) + '分 ' + (sec % 60) + '秒';
  if (sec < 86400) {
    const h = (sec / 3600).toFixed(1);
    return h + '小时';
  }
  const d = (sec / 86400).toFixed(1);
  return d + '天';
}

const reinvestigatingPids = new Set();

let currentAgentsData = [];

function openFpDrawer() {
  document.getElementById('drawer-overlay').classList.add('active');
  document.getElementById('fp-drawer').classList.add('active');
  loadFingerprints();
}

function closeAllDrawers() {
  document.getElementById('drawer-overlay').classList.remove('active');
  document.getElementById('fp-drawer').classList.remove('active');
  document.getElementById('inspect-drawer').classList.remove('active');
}

async function loadFingerprints() {
  const container = document.getElementById('fp-drawer-body');
  container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 40px;">正在加载指纹库...</div>';
  try {
    const res = await fetch('/api/fingerprints');
    const data = await res.json();
    if (!res.ok) throw new Error(data.message || '指纹库读取失败');
    const fps = data.fingerprints || [];
    if (fps.length === 0) {
      container.innerHTML = '<div style="text-align: center; color: var(--text-muted); padding: 40px;">指纹库暂无条目</div>';
      return;
    }
    container.innerHTML = fps.map(fp => {
      const recipe = fp.hook_recipe || {};
      const matchFeat = recipe.match_features || {};
      return `
        <div class="fp-item">
          <div class="fp-header">
            <div>
              <span class="fp-badge">${fp.id}</span>
              <b style="color: #fff; margin-left: 6px;">${fp.name}</b>
            </div>
            <span style="color: var(--text-muted); font-size: 11px;">命中: ${fp.match_count || 1} 次 · ${fp.first_seen || ''}</span>
          </div>
          <div style="font-size: 11px; color: var(--text-secondary); line-height: 1.5;">
            <div><b>原生宿主:</b> <span style="color: #38bdf8; font-family: monospace;">${fp.features ? fp.features.exe : ''} (${recipe.host_platform || '未知'})</span></div>
            <div><b>演进来源:</b> <span style="color: #fbbf24; font-family: monospace;">${matchFeat.evolves_prior_harness || '零先验初次推导'}</span></div>
            <div><b>模型判定:</b> <span style="color: #34d399; font-family: monospace;">${recipe.model_routing ? (recipe.model_routing.model || '未暴露') : '未暴露'}</span></div>
          </div>
          <div class="section-label" style="margin-top: 4px; margin-bottom: 2px;">推导配方结构 (Hook Recipe)</div>
          <div class="fp-code">${escapeHtml(JSON.stringify(recipe, null, 2))}</div>
        </div>
      `;
    }).join('');
  } catch(e) {
    container.innerHTML = `<div style="color: #ef4444; padding: 20px;">加载失败: ${e.message}</div>`;
  }
}

async function openInspector(pid) {
  if (!currentAgentsData || currentAgentsData.length === 0) {
    try {
      const res = await fetch('/api/state');
      const data = await res.json();
      currentAgentsData = data.agents || [];
    } catch(e) {}
  }
  const agent = currentAgentsData.find(a => a.pid === pid) || currentAgentsData[0];
  if (!agent) return;
  document.getElementById('inspect-title').innerText = `🔍 ${agent.name} (PID: ${agent.pid}) 全景透视`;
  const container = document.getElementById('inspect-drawer-body');
  
  const adapter = agent.adapter || {};
  const parsedCfg = adapter.parsed_config || {};
  const net = adapter.network_surface || {};
  const memoryCtx = adapter.memory_context || {};
  
  container.innerHTML = `
    <div class="fp-item">
      <div class="fp-header">
        <b style="color: #38bdf8;">🏢 完整运行与宿主上下文</b>
        <span class="pid-tag">PID: ${agent.pid}</span>
      </div>
      <div style="font-size: 11px; color: var(--text-secondary); display: flex; flex-direction: column; gap: 4px;">
        <div><b>原生可执行文件:</b> <span style="color: #fff; font-family: monospace;">${agent.raw_exe}</span></div>
        <div><b>进程存活时长:</b> <span style="color: #fff;">${formatUptime(agent.uptime_sec)}</span></div>
        <div><b>工作区路径 (CWD):</b> <span style="color: #38bdf8; font-family: monospace;">${adapter.workspace_cwd || '未知'}</span></div>
        <div><b>宿主治理层级:</b> <span style="color: #cbd5e1;">${adapter.host_platform || '未知'}</span></div>
        <div><b>Goose 调查身份:</b> <span style="color: #38bdf8;">${findingText(adapter.investigated_identity)}</span></div>
      </div>
    </div>

    <div class="fp-item">
      <div class="fp-header">
        <b style="color: #34d399;">🧠 模型调用与通信路由</b>
        <span class="fp-badge">${adapter.harness_id || 'unregistered'}</span>
      </div>
      <div class="fp-code">${assetText(adapter, 'model_routing')}</div>
    </div>

    <div class="fp-item">
      <div class="fp-header">
        <b style="color: #818cf8;">⚙️ 提取与脱敏配置文件 (Parsed Config)</b>
        <span style="font-size: 10px; color: var(--green);">采集状态见下方</span>
      </div>
      <div class="fp-code">${assetText(adapter, 'parsed_config')}</div>
    </div>

    <div class="fp-item">
      <div class="fp-header">
        <b style="color: #f59e0b;">🌐 网络与通信表面 (Network Surface)</b>
      </div>
      <div class="fp-code">${assetText(adapter, 'network_surface')} · 当前观测未完成时不能判断连接情况或安全性</div>
    </div>

    <div class="fp-item">
      <div class="fp-header">
        <b style="color: #cbd5e1;">📋 挂载规则与提示词 (System Prompt Rules)</b>
      </div>
      <div style="font-size: 11px; color: #cbd5e1; line-height: 1.5;">
        ${assetText(adapter, 'system_prompt_rules')}
      </div>
    </div>
  `;
  
  document.getElementById('drawer-overlay').classList.add('active');
  document.getElementById('inspect-drawer').classList.add('active');
}

async function updateUI() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    document.getElementById('last-time').innerText = data.last_scan_time || '初始化中';
    document.getElementById('scan-count').innerText = data.scan_count;
    document.getElementById('scan-interval').innerText = `${data.scan_interval || 30}s`;
    document.getElementById('fp-count').innerText = data.fingerprints_count;
    document.getElementById('kpi-fp-total').innerText = data.fingerprints_count;
    const observe = data.observation_adapter || {};
    const observeHealth = observe.health ? ` · ${observe.health.status}` : '';
    const observePid = observe.instance_pid ? ` · 绑定 PID ${observe.instance_pid}` : '';
    document.getElementById('observe-status').innerText = (observe.status || '未知') + observeHealth + observePid;
    
    currentAgentsData = data.agents || [];
    
    // 更新全局 KPI 指标
    const totalAgents = currentAgentsData.length;
    let totalInstances = 0;
    let matchedCount = 0;
    let totalPorts = 0;
    let confirmedCount = 0, infraCount = 0, pendingCount = 0;
    currentAgentsData.forEach(a => {
      totalInstances += (a.all_pids ? a.all_pids.length : 1);
      if (a.adapter && a.adapter.matched) matchedCount++;
      const cls = (a.adapter && a.adapter.agent_classification) || {};
      if (cls.status === 'confirmed_agent') confirmedCount++;
      else if (cls.status === 'infrastructure') infraCount++;
      else pendingCount++;
      if (a.adapter && a.adapter.network_surface) {
        const lp = a.adapter.network_surface.listening_ports || [];
        const rp = a.adapter.network_surface.remote_peers || [];
        totalPorts += (lp.length + rp.length);
      }
    });
    
    document.getElementById('kpi-agent-count').innerText = confirmedCount;
    document.getElementById('kpi-instance-count').innerText = `${totalInstances} 关联进程`;
    document.getElementById('kpi-instance-count').title = `确认 Agent ${confirmedCount} · 基础设施 ${infraCount} · 待确认 ${pendingCount}`;
    const rate = totalAgents > 0 ? Math.round((matchedCount / totalAgents) * 100) : 0;
    document.getElementById('kpi-hook-rate').innerText = `${rate}%`;
    document.getElementById('kpi-hook-detail').innerText = `${matchedCount}/${totalAgents} 指纹匹配`;
    const netColor = totalPorts > 0 ? 'var(--amber)' : 'var(--green)';
    document.getElementById('kpi-net-count').style.color = netColor;
    document.getElementById('kpi-net-count').innerText = '未知';
    document.getElementById('kpi-net-detail').innerText = totalPorts > 0 ? `${totalPorts} 历史端点（未验证）` : '尚未采集，不能判断安全性';
    
    const grid = document.getElementById('agents-grid');
    if (!data.agents || data.agents.length === 0) {
      grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 80px; background: var(--card-bg); border-radius: 12px; border: 1px dashed var(--border);">未发现存活的 Agent 进程 (阈值 >= 50)</div>';
      return;
    }
    
    let html = '';
    data.agents.forEach(a => {
      const isMatched = a.adapter && a.adapter.matched;
      const backendInvestigating = !!(a.adapter && a.adapter.investigating);
      const investigation = (a.adapter && a.adapter.investigation) || {};
      if (!backendInvestigating && investigation.status && reinvestigatingPids.has(a.pid)) {
        reinvestigatingPids.delete(a.pid);
      }
      const isInvestigating = backendInvestigating;
      const observationEvidence = (a.adapter && a.adapter.observation_evidence) || {};
      const observationText = a.adapter.learned_observation && a.adapter.learned_observation.status === 'observing'
        ? '观测证据: 本实例工具前后事件已配对验证（验收快照，非持续健康保证）'
        : observationEvidence.status === 'observed'
        ? `观测证据: ${observationEvidence.label} · 当前=${observationEvidence.health_status || 'unknown'} · 有效事件=${observationEvidence.recent_events || 0} · 阻断=${(observationEvidence.blocking || {}).label || '未支持'}`
        : `观测证据: ${observationEvidence.label || '未绑定观测证据'}${observationEvidence.reason ? ' · ' + observationEvidence.reason : ''}`;
      const onboarding = (a.adapter && a.adapter.onboarding) || {};
      const plan = onboarding.plan || {};
      const install = onboarding.install || {};
      const verification = onboarding.verification || {};
      const learnedHook = (a.adapter && a.adapter.hook_state) || {};
      const onboardingText = learnedHook.status === 'observing'
        ? '接入链: Goose生成方案已安装，真实工具前后事件已验收（仅观测）'
        : learnedHook.status === 'loaded'
          ? '接入链: 生成Hook已加载，等待工具事件'
        : learnedHook.status === 'installed_pending_activation'
          ? '接入链: 生成方案已安装，待目标加载'
        : verification.status === 'events_verified'
        ? '接入链: 加载与工具事件已验证（仅观测，阻断未支持）'
        : verification.status === 'loaded_verified'
          ? '接入链: 插件已加载，尚无工具事件（未完成观测验证）'
        : install.status === 'installed_pending_activation'
          ? '接入链: 已安装，待引擎启动/重载'
          : install.status === 'pending_authorization'
            ? '接入链: 计划待授权'
            : plan.status === 'investigation_required'
              ? '接入链: 等待 Goose 调查'
              : plan.status === 'unsupported'
                ? '接入链: 不支持 · ' + (plan.reason || '')
                : plan.status === 'plan_pending_authorization'
                  ? '接入链: 计划待授权'
                  : '接入链: ' + (plan.status || '尚未生成计划');
      
      const classification = (a.adapter && a.adapter.agent_classification) || {};
      const statusHtml = '<div class="asset-view status-overview">' +
        readableLine('调查', investigation.label || '未调度') +
        readableLine('指纹', ({exact:'兼容命中',similar:'相似，待调查',miss:'未命中'}[a.adapter.match_status] || '未判断') + (a.adapter.fingerprint_revision ? ' · 版本 ' + a.adapter.fingerprint_revision : '')) +
        readableLine('Hook', observationEvidence.status === 'observed' && observationEvidence.target_alive === true ? '已加载 · 工具事件已接通' : a.adapter.hook_state.status === 'observing' ? '已有事件验收记录（非实时健康）' : a.adapter.hook_state.label || '未安装') +
        readableDetails('status:'+a.pid,'查看状态说明',{调查:investigation.message || '',分类:classification.label,观测:observationText,接入:onboardingText}) + '</div>';
      const partialIdentityHtml = findingText(a.adapter.investigated_identity);
      const instanceCount = (a.instances && a.instances.length > 1) ? ` <span class="pid-tag" style="background: rgba(16, 185, 129, 0.2); color: #34d399; border-color: rgba(16, 185, 129, 0.4);">${a.instances.length} 实例聚合</span>` : '';
      const pidsList = `主 PID: ${a.pid} · ${(a.all_pids || [a.pid]).length} 进程`;

      const reasonTags = (a.reasons && a.reasons.length) 
        ? `<div class="score-tags">${a.reasons.map(r => `<span class="score-tag">${r}</span>`).join('')}</div>`
        : '';

      const hasSemanticMsg = a.last_message && a.last_message.event_type && a.last_message.event_type !== '未监听';
      let semanticHtml = '';
      if (hasSemanticMsg) {
        let msgDetail = a.last_message.detail || '暂无内容';
        if (typeof msgDetail === 'object') msgDetail = JSON.stringify(msgDetail, null, 2);
        semanticHtml = `
          <div>
            <div class="section-label">导入事件（不证明当前 Hook 生效）</div>
            <div class="msg-box">
              <div class="msg-header">
                <span>事件: ${a.last_message.event_type}</span>
                <span style="font-size: 10px; color: var(--text-muted);">${a.last_message.ts || ''}</span>
              </div>
              <div class="msg-content">${msgDetail}</div>
            </div>
          </div>
        `;
      } else {
        semanticHtml = `
          <div>
            <div class="stream-badge">
              <span>🛡️ 语义拦截 Sink:</span>
              <span style="color: #64748b;">未接入／未验证</span>
            </div>
          </div>
        `;
      }

      const btnText = investigation.status === 'disabled' ? investigation.message : (isInvestigating ? '调查执行中' : 'Goose 深度重测');
      const btnDisabled = investigation.can_request ? '' : 'disabled';
      const continuation = investigation.can_continue
        ? `<button onclick="triggerContinue(${a.pid})" class="btn-reinvestigate" style="color: #fbbf24; border-color: rgba(251, 191, 36, 0.4);">基于已保留证据续查</button>`
        : '';
      const cancelButton = isInvestigating
        ? `<button onclick="triggerCancel(${a.pid})" class="btn-reinvestigate" style="color: #f87171; border-color: rgba(248, 113, 113, 0.4);">取消调查</button>`
        : '';

      const scoreClass = a.score >= 80 ? 'score-high' : (a.score >= 50 ? 'score-mid' : 'score-low');

      html += `
        <div class="card">
          <div class="card-top">
            <div>
              <div class="agent-name inspect-trigger" onclick="openInspector(${a.pid})" title="点击查看深度全景档案">${a.name} <span class="pid-tag">${pidsList}</span>${instanceCount} <span class="pid-tag" style="background: rgba(99, 102, 241, 0.15); color: #818cf8; border-color: rgba(99, 102, 241, 0.4);">${classificationText(classification)}</span></div>
              <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">原生程序: ${a.raw_exe} · 存活时长: <b style="color: #cbd5e1;">${formatUptime(a.uptime_sec)}</b></div>
            </div>
            <div style="text-align: right;">
              <div class="score-badge ${scoreClass}" title="身份/行为识别分，不代表安全风险或概率">识别分: ${a.score}</div>
              ${reasonTags}
            </div>
          </div>
          
          <div>
            <div class="section-label">启动命令行与参数特征</div>
            <div class="cmdline">${a.cmdline}</div>
            <details><summary>进程归属与身份依据</summary><div class="cmdline">关联进程（扫描快照）: ${(a.process_pids || [a.pid]).join(', ')}<br>实例: ${escapeHtml(a.instance_id)}<br>身份依据: ${escapeHtml(a.identity ? (a.identity.evidence || '行为推断') : '行为推断 / 指纹')}</div></details>
          </div>

          <div>
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px;">
              <div class="section-label" style="margin-bottom: 0;">Agent 调查资料（状态与来源）</div>
              <div style="display: flex; gap: 6px;">
                <button onclick="openInspector(${a.pid})" class="btn-reinvestigate" style="background: rgba(56, 189, 248, 0.15); border-color: rgba(56, 189, 248, 0.4); color: #38bdf8;">🔍 深度透视</button>
                <button id="btn-reinv-${a.pid}" onclick="triggerReinvestigate(${a.pid})" class="btn-reinvestigate" ${btnDisabled}>${btnText}</button>
                ${continuation}
                ${cancelButton}
              </div>
            </div>
            <div class="adapter-box">
              <div class="adapter-group-title">🏢 身份与运行环境</div>
              <div class="adapter-row">
                <span class="adapter-label">调查 / 指纹 / Hook:</span>
                <div class="adapter-val">${statusHtml}</div>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">Goose 部分身份:</span>
                <div class="adapter-val">${partialIdentityHtml}</div>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">宿主与工作区:</span>
                <div class="adapter-val" style="color: #38bdf8; font-family: monospace;">${a.adapter.host_platform} · ${a.adapter.workspace_cwd || '未知工作区'}</div>
              </div>

              <div class="adapter-group-title">🧠 模型与治理策略</div>
              <div class="adapter-row">
                <span class="adapter-label">模型与网关端点:</span>
                <div class="adapter-val" style="color: #34d399; font-family: monospace;">${assetText(a.adapter, 'model_routing')}</div>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">可用 Tools / MCP:</span>
                <div class="adapter-val">${assetText(a.adapter, 'registered_tools_and_mcp')}</div>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">行规/Prompt 约束:</span>
                <div class="adapter-val" style="color: #cbd5e1;">${assetText(a.adapter, 'system_prompt_rules')}</div>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">配置解析提取:</span>
                <span class="adapter-val">${assetText(a.adapter, 'parsed_config')}</span>
              </div>

              <div class="adapter-row"><span class="adapter-label">Skill:</span><div class="adapter-val">${assetText(a.adapter, 'skills')}</div></div>
              <div class="adapter-group-title">🌐 执行与通信画像（无数据不能判断安全性）</div>
              <div class="adapter-row">
                <span class="adapter-label">执行事件（${a.adapter.assets.child_executions.status === 'collected' ? '本实例验收' : '未接入采集'}）:</span>
                <div class="adapter-val" style="color: #f59e0b; font-family: monospace;">${assetText(a.adapter, 'child_executions')}</div>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">网络与监听端点:</span>
                <div class="adapter-val" style="color: #38bdf8; font-family: monospace;">${assetText(a.adapter, 'network_surface')}</div>
              </div>
            </div>
          </div>

          ${semanticHtml}
          <details class="adapter-box" id="activity-details-${a.pid}" ${activityOpenPids.has(a.pid) ? 'open' : ''} style="margin-top:8px;" onToggle="onActivityToggle(${a.pid}, this)">
            <summary style="cursor:pointer;font-size:12px;color:var(--text-muted);">🔬 调查活动（工具调用与 finding 时间线，脱敏）</summary>
            <div id="activity-body-${a.pid}" style="margin-top:6px;"></div>
          </details>
        </div>
      `;
    });
    grid.innerHTML = html;
    activityOpenPids.forEach(pid => {
      const panel = document.getElementById('activity-details-' + pid);
      if (!panel) { activityOpenPids.delete(pid); return; }
      if (activityCache[pid]) renderActivity(pid, activityCache[pid].data);
    });
  } catch (err) {
    console.error("更新失败:", err);
  }
}

async function triggerScan() {
  await fetch('/api/scan', { method: 'POST' });
  await updateUI();
}

async function triggerReinvestigate(pid) {
  const btn = document.getElementById('btn-reinv-' + pid);
  if (btn) {
    btn.disabled = true;
    btn.innerText = '⚡ 正在重推导...';
  }
  reinvestigatingPids.add(pid);
  try {
    const response = await fetch('/api/reinvestigate?pid=' + pid, { method: 'POST' });
    if (!response.ok) reinvestigatingPids.delete(pid);
  } catch(e) {}
  await updateUI();
}

async function triggerContinue(pid) {
  reinvestigatingPids.add(pid);
  try {
    await fetch('/api/reinvestigate/continue?pid=' + pid, { method: 'POST' });
  } catch(e) {}
  await updateUI();
}

async function triggerCancel(pid) {
  try {
    await fetch('/api/reinvestigate/cancel?pid=' + pid, { method: 'POST' });
  } catch(e) {}
  await updateUI();
}

function handleHashRouting() {
  const hash = window.location.hash;
  if (hash === '#fp') {
    openFpDrawer();
  } else if (hash.startsWith('#inspect:')) {
    const targetPid = parseInt(hash.split(':')[1]);
    if (targetPid) openInspector(targetPid);
  }
}

window.addEventListener('hashchange', handleHashRouting);
setInterval(updateUI, 5000);
updateUI().then(handleHashRouting);
setInterval(() => { activityOpenPids.forEach(pid => loadActivity(pid)); }, 5000);
</script>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
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
            data = json.dumps(snapshot, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
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
        if self.path == "/api/scan":
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
                if install_result.get("status") == "installed_pending_activation":
                    verification_result = onboarding.verify_activation(install_result, target)
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
                verification_result = onboarding.verify_activation(install_result or {}, target)
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
    print("[Monitor] 执行首次进程环境扫描...")
    scan_agents_once()
    
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
