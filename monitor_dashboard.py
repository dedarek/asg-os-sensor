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
import time
import shutil
import shlex
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
from runtime import analyzer, matcher
from runtime.status import presentation, DISABLED_REASON
from runtime.recipe_validation import validate as validate_recipe
from runtime.identity import identify, ownership, metadata_identity
from runtime.stream_parser import redact
from runtime.llm_config import analyst_key as load_analyst_key
from runtime.llm_config import analyst_route as load_analyst_route
from runtime.llm_config import goose_env as build_goose_env
from runtime.llm_config import load_environment
from runtime.llm_config import mask_key as mask_analyst_key

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
SCAN_INTERVAL_S = _env_int("ASG_SCAN_INTERVAL", 30)
MAX_ANALYSTS = _env_int("ASG_MAX_ANALYSTS", 2)
GOOSE_MAX_TURNS = _env_int("ASG_GOOSE_MAX_TURNS", 12)
GOOSE_TIMEOUT_S = _env_int("ASG_GOOSE_TIMEOUT", 180)
GOOSE_RETRY_COOLDOWN_S = _env_int("ASG_GOOSE_RETRY_COOLDOWN", 300)
AUTONOMOUS_ANALYSIS_ENABLED = os.environ.get("ASG_AUTONOMOUS_ANALYSIS", "1").strip().lower() not in {"0", "false", "no", "off"}
OBSERVE_URL = os.environ.get("ASG_OBSERVE_URL", "").strip().rstrip("/")
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
        "status": "not_configured" if not OBSERVE_URL else "pending",
        "source": OBSERVE_URL or None,
    },
    "active_investigations": {}  # pid -> {status, started_at, turns}
}

# 记录当前已挂起正在调查的 PID，避免重复拉起多个 Goose
INVESTIGATING_INSTANCES: dict[str, float] = {}  # instance_id -> create_time
INVESTIGATION_LOCK = threading.Lock()
INVESTIGATION_SEMAPHORE = threading.Semaphore(MAX_ANALYSTS)  # 最多同时允许 N 个 Goose 并发，防止跑满 API 与进程雪崩
INVESTIGATION_RESULTS: dict[str, dict[str, Any]] = {}  # instance_id -> result
INVESTIGATION_RETRY_AT: dict[str, float] = {}  # instance_id -> retry_at


def _goose_executable() -> str | None:
    """返回可执行 Goose 路径；避免缺失时进入无限后台重试。"""
    value = str(GOOSE)
    if GOOSE.is_absolute() or GOOSE.parent != Path("."):
        return value if GOOSE.is_file() and os.access(GOOSE, os.X_OK) else None
    return shutil.which(value)


def _record_investigation_result(instance_id: str, pid: int, create_time: float | None, status: str, message: str, run_dir: Path | None = None) -> None:
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
        path = run_dir / 'result.json'
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, ensure_ascii=False))
        os.replace(temporary, path)
    with INVESTIGATION_LOCK:
        INVESTIGATION_RESULTS[instance_id] = result


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or now()))


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


def read_observation_snapshot() -> dict[str, Any]:
    """受控读取观测契约；该适配层不改变扫描器的 Agent 列表。"""
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


def run_autonomous_investigation(pid: int, struct: dict[str, Any], force: bool = False):
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

    goose_bin = _goose_executable()
    if not goose_bin:
        message = f"未找到 Goose CLI（当前解析值: {GOOSE}）。请安装 block-goose-cli 或设置 ASG_GOOSE_BIN。"
        _record_investigation_result(instance_id, pid, create_time, "unavailable", message)
        with INVESTIGATION_LOCK:
            INVESTIGATION_RETRY_AT[instance_id] = now() + GOOSE_RETRY_COOLDOWN_S
            INVESTIGATING_INSTANCES.pop(instance_id, None)
        print(f"[Analyst Unavailable PID={pid}] {message}", file=sys.stderr)
        return

    # 不把所有候选都排进一个长队：只有拿到并发槽位的 PID 才进入“正在接管”。
    # 否则几十个候选会在 semaphore 后等待数十分钟，看板就像永远卡住。
    if not INVESTIGATION_SEMAPHORE.acquire(blocking=False):
        with INVESTIGATION_LOCK:
            INVESTIGATING_INSTANCES.pop(instance_id, None)
        if force:
            _record_investigation_result(instance_id, pid, create_time, "busy", "分析器并发槽位已满，请稍后重试")
        return

    try:
        run_dir = Path(os.environ.get("ASG_RUN_DIR", str(ROOT / "artifacts" / "stage1" / "dashboard"))) / f"pid_{pid}_{int(time.time())}"
        run_dir.mkdir(parents=True, exist_ok=True)
        recipes_dir = run_dir / "recipes"
        recipes_dir.mkdir(parents=True, exist_ok=True)

        stream_file = run_dir / "target_stream.jsonl"
        stream_file.touch()

        with STATE_LOCK:
            SCAN_STATE["active_investigations"][pid] = {
                "status": "investigating",
                "instance_id": instance_id,
                "started_at": time.strftime("%H:%M:%S"),
                "log_dir": str(run_dir)
            }

        try:
            route = analyst_route()
            key = load_analyst_key(route)
            if not key:
                message = "LLM 凭据缺失: " + str(route.get("key_env", "?"))
                _record_investigation_result(instance_id, pid, create_time, "failed", message, run_dir)
                print("[Analyst] route=" + route.get("route", "?") + " model=" + str(route.get("model", "?")) + " base=" + str(route.get("base_url", "?")) + " key=" + mask_analyst_key(key) + " (" + str(route.get("key_env", "?")) + ")", file=sys.stderr)
                return
            if os.environ.get("ASG_INSECURE_SSL", "").strip() == "1" and os.environ.get("ASG_ALLOW_INSECURE_ANALYST", "").strip() != "1":
                message = "上游 TLS 证书无效；已阻止深度数据外发。修复证书，或明确设置 ASG_ALLOW_INSECURE_ANALYST=1"
                _record_investigation_result(instance_id, pid, create_time, "blocked", message, run_dir)
                print(f"[Analyst Blocked PID={pid}] {message}", file=sys.stderr)
                return

            extension = 'asg-runtime-tools:' + shlex.join([sys.executable, '-B', str(ROOT / 'runtime' / 'analyst_tools.py')])
            cmd = [
                goose_bin, "run", "--no-profile", "--no-session",
                "--recipe", str(RECIPE),
                "--params", f"target_pid={pid}",
                "--provider", route["provider"],
                "--model", route["model"],
                "--max-turns", str(GOOSE_MAX_TURNS),
                "--max-tool-repetitions", "2",
                "--output-format", "stream-json",
                "--with-extension", extension
            ]

            env = os.environ.copy()
            env.update(build_goose_env(route, key, pid))
            env.update(ASG_TARGET_PID=str(pid), ASG_TARGET_CREATE_TIME=str(create_time),
                       ASG_AUDIT_DIR=str(run_dir), ASG_RECIPE_DIR=str(recipes_dir),
                       ASG_TARGET_STREAM_FILE=str(stream_file))

            out_path = run_dir / "analyst_stdout.jsonl"
            err_path = run_dir / "analyst_stderr.log"

            print(f"[Analyst] Goose 开始自主逆向接管 PID={pid}...")
            t0 = time.time()
            cp = subprocess.run(cmd, cwd=ROOT, env=env, stdout=out_path.open("w", encoding="utf-8"), stderr=err_path.open("w", encoding="utf-8"), text=True, timeout=GOOSE_TIMEOUT_S)
            elapsed_ms = int((time.time() - t0) * 1000)

            # 检查是否成功产出 candidate.json
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
                    entry = matcher.remember_verified(struct, recipe, evidence, elapsed_ms)
                    hook_text = "接入点 proposed/unverified（未核对接入点证据）"
                    _record_investigation_result(instance_id, pid, create_time, "succeeded",
                                                 f"已保存候选配方 {entry.get('id')}（{hook_text}），未安装／未验证", run_dir)
                    print(f"[Analyst] 候选配方写入指纹库! Agent={entry.get('name')}, HarnessID={entry.get('id')}")
                else:
                    _record_investigation_result(instance_id, pid, create_time, "failed", f"Recipe 未通过质量门禁 (identity={identity}, confidence={recipe.get('confidence')})", run_dir)
                    print(f"[Analyst] 逆向目标在调查期间已退出或不可达 (identity={identity}, confidence={recipe.get('confidence')})，放弃生成无效指纹。")
            else:
                stderr_tail = ''
                try:
                    err_txt = err_path.read_text(encoding='utf-8', errors='replace').strip().splitlines()
                    if err_txt:
                        stderr_tail = redact('\n'.join(err_txt[-6:]))[-600:]
                except OSError:
                    pass
                reason = f"Goose 未产生有效 Recipe (returncode={cp.returncode})"
                if stderr_tail:
                    reason += "；stderr 尾部: " + stderr_tail
                _record_investigation_result(instance_id, pid, create_time, "failed", reason, run_dir)
                print(f"[Analyst] 接管完成但未产生有效 Recipe; returncode={cp.returncode}; stderr_tail={stderr_tail[:200]}", file=sys.stderr)

        except subprocess.TimeoutExpired:
            _record_investigation_result(instance_id, pid, create_time, "failed", f"Goose 执行超时 ({GOOSE_TIMEOUT_S}s)", run_dir)
            print(f"[Analyst Error PID={pid}] Goose timeout after {GOOSE_TIMEOUT_S}s", file=sys.stderr)
        except Exception as exc:
            _record_investigation_result(instance_id, pid, create_time, "failed", f"{type(exc).__name__}: {exc}", run_dir)
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
        INVESTIGATION_SEMAPHORE.release()


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
            print(f"[Scan Match] PID={pid}, name={name}, matched={bool(matched_fp)}, harness={(matched_fp.get('id') if matched_fp else None)}")
            
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

            recipe_obj = matched_fp.get("hook_recipe", {}) if matched_fp else {}
            adapter_info = {
                "matched": is_matched,
                "investigating": is_investigating,
                "investigation": investigation_result,
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
            adapter_info['observation_evidence'] = observation_for_instance(
                observation_snapshot, pid, pinfo.get('create_time'))
            for field in adapter_info['assets']:
                adapter_info[field] = None
            with STATE_LOCK:
                active_run = dict(SCAN_STATE['active_investigations'].get(pid, {}))
            if active_run.get('instance_id') and active_run['instance_id'] != instance_id:
                active_run = {}  # 复用 PID 的旧运行态不进入新实例
            log_dir = active_run.get('log_dir') or investigation_result.get('log_dir')
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
            adapter_info['hook'] = '未安装' 
            adapter_info['observation'] = '未接入／未验证'
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
            needs_deep_governance = False  # exact compatibility reuses investigation, even when model is unknown
            with INVESTIGATION_LOCK:
                retry_ready = now() >= INVESTIGATION_RETRY_AT.get(instance_id, 0)
            target_allowed = not os.environ.get('ASG_ANALYST_TARGET_PID') or str(pid) == os.environ['ASG_ANALYST_TARGET_PID']
            if target_allowed and AUTONOMOUS_ANALYSIS_ENABLED and (not is_matched or needs_deep_governance) and not is_investigating and retry_ready:
                threading.Thread(
                    target=run_autonomous_investigation,
                    args=(pid, struct),
                    name=f"analyst-worker-{pid}",
                    daemon=True
                ).start()

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
</style>
</head>
<body>

<div class="header">
  <div class="title">
    <div class="pulse"></div>
    ASG · 进程发现与调查状态（未安装 Hook）
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
    <div class="kpi-label">在线治理 Agent 实体</div>
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
function assetText(adapter, key) {
  const item = (adapter.assets || {})[key] || {label: '尚未采集'};
  return escapeHtml(item.label + (item.message ? '：' + item.message : '') + (item.source ? ' [来源: ' + item.source + ']' : '') +
    (item.value ? ' · ' + JSON.stringify(item.value) : ''));
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
    currentAgentsData.forEach(a => {
      totalInstances += (a.all_pids ? a.all_pids.length : 1);
      if (a.adapter && a.adapter.matched) matchedCount++;
      if (a.adapter && a.adapter.network_surface) {
        const lp = a.adapter.network_surface.listening_ports || [];
        const rp = a.adapter.network_surface.remote_peers || [];
        totalPorts += (lp.length + rp.length);
      }
    });
    
    document.getElementById('kpi-agent-count').innerText = totalAgents;
    document.getElementById('kpi-instance-count').innerText = `${totalInstances} 关联进程`;
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
      const observationText = observationEvidence.status === 'observed'
        ? `观测证据: ${observationEvidence.label} · 当前=${observationEvidence.health_status || 'unknown'} · 有效事件=${observationEvidence.recent_events || 0} · 阻断=${(observationEvidence.blocking || {}).label || '未支持'}`
        : `观测证据: ${observationEvidence.label || '未绑定观测证据'}${observationEvidence.reason ? ' · ' + observationEvidence.reason : ''}`;
      
      const statusHtml = escapeHtml(investigation.label + ' · ' + (investigation.message || '') +
        ' | ' + ({exact:'精确匹配（Hook 待验证）', similar:'相似匹配（需差异调查）', miss:'未命中指纹'}[a.adapter.match_status] || '未命中指纹') + ' | 配方 revision: ' + (a.adapter.fingerprint_revision || '无') + ' | Hook: ' + a.adapter.hook_state.label + ' | ' + observationText);
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

      const scoreClass = a.score >= 80 ? 'score-high' : (a.score >= 50 ? 'score-mid' : 'score-low');

      html += `
        <div class="card">
          <div class="card-top">
            <div>
              <div class="agent-name inspect-trigger" onclick="openInspector(${a.pid})" title="点击查看深度全景档案">${a.name} <span class="pid-tag">${pidsList}</span>${instanceCount}</div>
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
              </div>
            </div>
            <div class="adapter-box">
              <div class="adapter-group-title">🏢 身份与运行环境</div>
              <div class="adapter-row">
                <span class="adapter-label">调查 / 指纹 / Hook:</span>
                <span class="adapter-val">${statusHtml}</span>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">宿主与工作区:</span>
                <span class="adapter-val" style="color: #38bdf8; font-family: monospace;">${a.adapter.host_platform} · ${a.adapter.workspace_cwd || '未知工作区'}</span>
              </div>

              <div class="adapter-group-title">🧠 模型与治理策略</div>
              <div class="adapter-row">
                <span class="adapter-label">模型与网关端点:</span>
                <span class="adapter-val" style="color: #34d399; font-family: monospace;">${assetText(a.adapter, 'model_routing')}</span>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">可用 Tools / MCP:</span>
                <div class="adapter-val">${assetText(a.adapter, 'registered_tools_and_mcp')}</div>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">行规/Prompt 约束:</span>
                <span class="adapter-val" style="color: #cbd5e1;">${assetText(a.adapter, 'system_prompt_rules')}</span>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">配置解析提取:</span>
                <span class="adapter-val inspect-trigger" onclick="openInspector(${a.pid})" style="color: #cbd5e1;" title="点击展开完整配置">${assetText(a.adapter, 'parsed_config')}</span>
              </div>

              <div class="adapter-row"><span class="adapter-label">Skill:</span><span class="adapter-val">${assetText(a.adapter, 'skills')}</span></div>
              <div class="adapter-group-title">🌐 执行与通信画像（无数据不能判断安全性）</div>
              <div class="adapter-row">
                <span class="adapter-label">执行事件（未接入采集）:</span>
                <span class="adapter-val" style="color: #f59e0b; font-family: monospace;">${assetText(a.adapter, 'child_executions')}</span>
              </div>
              <div class="adapter-row">
                <span class="adapter-label">网络与监听端点:</span>
                <span class="adapter-val" style="color: #38bdf8; font-family: monospace;">${assetText(a.adapter, 'network_surface')}</span>
              </div>
            </div>
          </div>

          ${semanticHtml}
        </div>
      `;
    });
    grid.innerHTML = html;
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
</script>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
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
    print(f"[Monitor] {SCAN_INTERVAL_S}s 扫描与调查引擎已启动")
    if not AUTONOMOUS_ANALYSIS_ENABLED:
        print("[Monitor] 自动深度分析已禁用 (ASG_AUTONOMOUS_ANALYSIS=0)")
    
    server = ThreadingHTTPServer((host, port), MonitorHandler)
    print(f"[Monitor] Web 界面已就绪: http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
