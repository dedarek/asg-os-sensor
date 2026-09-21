"""受控的 Agent 自动接入纵向切片。

本模块只编排已有的 matcher、ghost_install 和 EventVerifier：

    discovered instance -> exact/similar/miss plan -> validated recipe
    -> authorization-scoped install -> activation evidence -> experience

模型只能产生候选配方；可执行安装必须同时满足固定适配器契约、项目作用域、
隔离工作区和显式授权。默认不安装，也不会因为产品名称进入特殊分支。
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

import psutil

from runtime import matcher
from runtime.opencode import ghost_install
from runtime.opencode.event_api import EventVerifier
from runtime.opencode.server import load_manifest, resolve_events_file
from runtime.stream_parser import redact


ROOT = Path(__file__).resolve().parents[1]
EXPERIENCE_VERSION = 1
SUPPORTED_ADAPTER = "opencode-workspace-plugin"
ADAPTER_REGISTRY = {
    SUPPORTED_ADAPTER: {
        "adapter": SUPPORTED_ADAPTER,
        "method": "workspace-plugin",
        "scope": "project",
        "installer": "runtime.opencode.ghost_install",
        "plugin_name": "asg-observe.js",
        "activation_event": "hook.loaded",
        "observing_events": ("tool.execute.before", "tool.execute.after"),
        "install": ghost_install.install,
    },
}
# Compatibility alias for callers that only need to display the current backend.
SUPPORTED_CONTRACT = {k: v for k, v in ADAPTER_REGISTRY[SUPPORTED_ADAPTER].items()
                      if k not in ("install", "observing_events")}


def experience_path() -> Path:
    override = os.environ.get("ASG_EXPERIENCE_DB", "").strip()
    if override:
        return Path(override)
    run_root = os.environ.get("ASG_RUN_DIR", "").strip()
    if run_root:
        return Path(run_root) / "onboarding" / "experience.json"
    return ROOT / "artifacts" / "stage1" / "onboarding" / "experience.json"


def _empty_db() -> dict[str, Any]:
    return {"version": EXPERIENCE_VERSION, "events": [], "instances": {}}


def _load_unlocked(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty_db()
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("experience store unreadable/corrupt: %s" % type(exc).__name__) from exc
    if not isinstance(data, dict) or data.get("version") != EXPERIENCE_VERSION:
        raise ValueError("experience store schema unsupported")
    if not isinstance(data.get("events"), list) or not isinstance(data.get("instances"), dict):
        raise ValueError("experience store schema invalid")
    return data


def load_experience() -> dict[str, Any]:
    """严格读取经验库；损坏时抛错，绝不静默当成空历史。"""
    path = experience_path()
    with matcher._THREAD_LOCK:  # reuse the process-wide writer lock
        with matcher._FileLock(path):
            return copy.deepcopy(_load_unlocked(path))


def _update(mutator):
    path = experience_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with matcher._THREAD_LOCK:
        with matcher._FileLock(path):
            data = _load_unlocked(path)
            result = mutator(data)
            matcher._write_atomic(path, data)
            return copy.deepcopy(result)


def make_instance_id(pid: int, create_time: float) -> str:
    return "%d:%s" % (int(pid), float(create_time))


def _safe_payload(value: Any) -> Any:
    # Recipe evidence is already constrained, but redact again before persistence.
    return redact(copy.deepcopy(value))


def record_transition(instance: dict[str, Any], event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    pid = int(instance["pid"])
    create_time = float(instance["create_time"])
    iid = make_instance_id(pid, create_time)
    event = {
        "event_id": "onb-%d" % time.time_ns(),
        "event_type": event_type,
        "instance_id": iid,
        "pid": pid,
        "create_time": create_time,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "payload": _safe_payload(payload),
    }

    def mutate(data):
        data.setdefault("events", []).append(event)
        summary = data.setdefault("instances", {}).setdefault(iid, {
            "instance_id": iid, "pid": pid, "create_time": create_time,
        })
        summary.update({
            "last_event": event_type,
            "updated_at": event["ts"],
        })
        for key in ("match_status", "fingerprint_id", "fingerprint_revision", "recipe_source",
                    "recipe", "plan", "install", "verification", "compatibility"):
            if key in payload:
                summary[key] = _safe_payload(payload[key])
        return event

    return _update(mutate)


def instance_state(instance_id: str) -> dict[str, Any] | None:
    data = load_experience()
    value = data.get("instances", {}).get(instance_id)
    return copy.deepcopy(value) if isinstance(value, dict) else None


def _analyst_event(event: dict[str, Any]) -> dict[str, Any]:
    """Return only lifecycle facts useful to Goose; never expose recipe/install secrets."""
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    item = {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "ts": event.get("ts"),
        "instance_id": event.get("instance_id"),
    }
    for key in ("match_status", "fingerprint_id", "fingerprint_revision", "recipe_source", "reason"):
        if key in payload:
            item[key] = payload[key]
    for key in ("plan", "install", "verification"):
        value = payload.get(key)
        if isinstance(value, dict):
            summary = {}
            for field in ("status", "action", "match_status", "fingerprint_id", "fingerprint_revision",
                          "reason", "activation_status", "health_status", "valid_events",
                          "invalid_events", "hook_loaded", "observing", "hook_verified"):
                if field in value:
                    summary[field] = value[field]
            if summary:
                item[key] = summary
    return item


def _compatibility_equal(left: Any, right: Any) -> bool:
    """Compatibility prior is exact and opaque; private paths never leave this module."""
    return isinstance(left, dict) and isinstance(right, dict) and left == right


def load_prior_experience(instance_id: str | None = None, compatibility: dict[str, Any] | None = None,
                          limit: int = 20) -> dict[str, Any]:
    """读取调查生命周期经验的窄投影；损坏经验库向 MCP 调用方显式报错。"""
    data = load_experience()
    all_events = [event for event in data.get("events", []) if isinstance(event, dict)]
    matched_by = "none"
    if compatibility is not None:
        events = [event for event in all_events
                  if _compatibility_equal((event.get("payload") or {}).get("compatibility"), compatibility)]
        if events:
            matched_by = "compatibility"
        elif instance_id:
            # Preserve visibility for old same-instance records that predate compatibility snapshots.
            events = [event for event in all_events if event.get("instance_id") == instance_id]
            if events:
                matched_by = "instance_id_legacy"
    else:
        events = [event for event in all_events
                  if not instance_id or event.get("instance_id") == instance_id]
        if events:
            matched_by = "instance_id" if instance_id else "all"
    events = events[-max(1, min(int(limit), 50)):]
    candidate = data.get("instances", {}).get(instance_id) if instance_id else None
    summary = candidate if isinstance(candidate, dict) else None
    matched_instances = []
    for event in events:
        value = event.get("instance_id")
        if value and value not in matched_instances:
            matched_instances.append(value)
    return {
        "available": True,
        "instance_id": instance_id,
        "matched_by": matched_by,
        "matched_instances": matched_instances[:20],
        "instance": {key: summary[key] for key in ("last_event", "updated_at", "match_status",
                    "fingerprint_id", "fingerprint_revision", "recipe_source") if summary and key in summary},
        "recent": [_analyst_event(event) for event in events],
        "note": "仅生命周期状态摘要；配方、路径、凭据、兼容性原文和 nonce 不作为 prior 读取返回",
    }


def _workspace(struct: dict[str, Any]) -> str | None:
    cwd = struct.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    return cwd


def _trusted_source(source: str | None) -> bool:
    if source == "goose":
        return True
    return source == "goose-simulated" and os.environ.get("ASG_TEST_SIMULATED", "0") == "1"


def _contract_error(recipe: dict[str, Any], source: str | None = None) -> str | None:
    if not _trusted_source(source):
        return "历史配方来源为 manual/legacy，未证明来自本次 Goose 调查"
    hook = recipe.get("hook")
    if not isinstance(hook, dict):
        return "配方没有 hook 契约"
    if hook.get('method') == 'file_plan':
        from runtime.learned_install import validate_plan
        try:
            validate_plan(recipe.get('install_plan'))
            workspace = hook.get('workspace')
            if not isinstance(workspace, str) or not Path(workspace).is_absolute() or Path(workspace).resolve() == Path('/'):
                raise ValueError('file_plan requires an evidenced absolute installation directory; process cwd / is not an installation scope')
        except ValueError as exc:
            return '无效通用文件计划: ' + str(exc)
        return None
    if os.environ.get('ASG_PIPELINE') == '1':
        return '自动流程只执行 Goose 学到的通用文件计划；此机制需要继续调查'
    adapter = hook.get("adapter")
    contract = ADAPTER_REGISTRY.get(adapter)
    if not contract:
        return "配方接入适配器未登记: %s" % (adapter or "unknown")
    for key, expected in contract.items():
        if key in ("install", "observing_events"):
            continue
        if hook.get(key) != expected:
            return "配方接入契约不支持或未证实: %s" % key
    if hook.get("restart_required") not in (True, "unknown"):
        return "workspace-plugin 只能在启动/重载时激活，restart_required 必须为 true 或 unknown"
    return None


def _common_plan(struct: dict[str, Any], match_status: str, entry: dict[str, Any] | None,
                 recipe: dict[str, Any], recipe_source: str) -> dict[str, Any]:
    iid = make_instance_id(struct["pid"], struct["create_time"])
    hook = recipe.get("hook", {})
    adapter = "file_plan" if hook.get("method") == "file_plan" else hook.get("adapter")
    contract = {"installer": "runtime.learned_install"} if adapter == "file_plan" else ADAPTER_REGISTRY.get(adapter, {})
    workspace = hook.get("workspace") if adapter == "file_plan" else _workspace(struct)
    return {
        "plan_version": 1,
        "instance_id": iid,
        "pid": int(struct["pid"]),
        "create_time": float(struct["create_time"]),
        "match_status": match_status,
        "fingerprint_id": entry.get("id") if entry else None,
        "fingerprint_revision": entry.get("revision") if entry else None,
        "recipe_source": recipe_source,
        "recipe_identity": recipe.get("agent_identity_name", "unknown"),
        "adapter": adapter or "unregistered",
        "scope": hook.get("scope", "unknown"),
        "workspace": workspace,
        "install": {
            "installer": contract.get("installer"),
            "plugin_name": contract.get("plugin_name"),
        },
        "activation": {
            "status": "pending_restart",
            "event_type": contract.get("activation_event"),
            "restart_required": hook.get("restart_required"),
        },
        "verification": {
            "binding": "pid+create_time",
            "event_type": contract.get("activation_event"),
            "observing_events": list(contract.get("observing_events", ())),
            "blocking": "unsupported",
        },
        "authorization": {
            "status": "required",
            "required_scope": "project",
            "workspace_must_be_isolated": True,
        },
    }


def plan_from_match(struct: dict[str, Any], match_result: dict[str, Any]) -> dict[str, Any]:
    """从一次发现匹配产生只读计划，不调用 Goose、不写文件。"""
    status = match_result.get("status")
    entry = match_result.get("entry") if isinstance(match_result.get("entry"), dict) else None
    if status in ("similar", "miss"):
        return {
            "plan_version": 1,
            "instance_id": make_instance_id(struct["pid"], struct["create_time"]),
            "pid": int(struct["pid"]),
            "create_time": float(struct["create_time"]),
            "status": "investigation_required",
            "action": "goose_investigate",
            "match_status": status,
            "fingerprint_id": entry.get("id") if entry else None,
            "recipe_source": "historical_reference" if entry else None,
            "reason": match_result.get("reason", "需要调查"),
            "authorization": {"status": "not_yet_applicable"},
            "verification": {"blocking": "unsupported"},
        }
    if status != "exact" or not entry:
        return {
            "plan_version": 1,
            "instance_id": make_instance_id(struct["pid"], struct["create_time"]),
            "pid": int(struct["pid"]),
            "create_time": float(struct["create_time"]),
            "status": "investigation_required",
            "action": "goose_investigate",
            "match_status": "miss",
            "reason": "没有可复用的精确指纹",
            "authorization": {"status": "not_yet_applicable"},
            "verification": {"blocking": "unsupported"},
        }
    recipe = entry.get("hook_recipe")
    if not isinstance(recipe, dict):
        return {"status": "unsupported", "action": "manual_review", "match_status": "exact",
                "fingerprint_id": entry.get("id"), "reason": "精确指纹没有结构化配方"}
    reason = _contract_error(recipe, entry.get("recipe_source"))
    if reason:
        return {"status": "unsupported", "action": "manual_review", "match_status": "exact",
                "fingerprint_id": entry.get("id"), "fingerprint_revision": entry.get("revision"),
                "recipe_source": "fingerprint_reuse", "reason": reason,
                "verification": {"blocking": "unsupported"}}
    plan = _common_plan(struct, "exact", entry, recipe, "fingerprint_reuse")
    plan["status"] = "plan_pending_authorization"
    plan["action"] = "install_reused_recipe"
    plan["reason"] = "精确兼容指纹复用；仍需授权、激活和事件验证"
    if plan.get("adapter") == "file_plan":
        # Exact reuse must carry the ORIGINAL provenance: the reused recipe, the
        # original build snapshot and the original evidence references. The new
        # instance is never allowed to inherit the old evidence binding, and a
        # missing source is reported instead of being silently skipped.
        def same_build(left, right):
            if not isinstance(left, dict) or not isinstance(right, dict):
                return False
            return ({k: v for k, v in left.items() if k != 'launch'} ==
                    {k: v for k, v in right.items() if k != 'launch'})
        matched = next((r for r in reversed(entry.get("revisions") or [])
                        if isinstance(r, dict) and r.get("revision") == entry.get("revision")
                        and (r.get("compatibility") == struct.get("compatibility")
                             or same_build(r.get("compatibility"), struct.get("compatibility")))),
                       None)
        refs = [e.get("evidence_id") for e in ((matched or {}).get("evidence") or [])
                if isinstance(e, dict) and e.get("evidence_id")]
        plan["reuse_recipe"] = copy.deepcopy(recipe)
        plan["reuse_evidence_refs"] = refs
        plan["reuse_source"] = "fingerprint_revision"
        plan["observed_compatibility"] = copy.deepcopy((matched or {}).get("compatibility"))
    return plan


def plan_from_recipe(struct: dict[str, Any], recipe: dict[str, Any], match_status: str,
                     entry: dict[str, Any] | None = None, recipe_source: str = "goose") -> dict[str, Any]:
    """将已通过 recipe_validation 的候选配方转换为受控安装计划。"""
    reason = _contract_error(recipe, recipe_source)
    if reason:
        return {
            "plan_version": 1,
            "instance_id": make_instance_id(struct["pid"], struct["create_time"]),
            "pid": int(struct["pid"]),
            "create_time": float(struct["create_time"]),
            "status": "unsupported",
            "action": "manual_review",
            "match_status": match_status,
            "fingerprint_id": entry.get("id") if entry else None,
            "recipe_source": recipe_source,
            "reason": reason,
            "verification": {"blocking": "unsupported"},
        }
    plan = _common_plan(struct, match_status, entry, recipe, recipe_source)
    plan["status"] = "plan_pending_authorization"
    plan["action"] = "install_candidate_recipe"
    plan["reason"] = "配方结构已校验；等待授权范围和激活事件验证"
    return plan


def authorization_from_environment(workspace: str | None) -> dict[str, Any]:
    roots = os.environ.get('ASG_ONBOARDING_WORKSPACE_ROOTS', '').strip()
    in_scope = True
    if roots:
        in_scope = False
        if workspace:
            resolved = Path(workspace).resolve()
            for root in roots.split(os.pathsep):
                if root and (resolved == Path(root).resolve() or Path(root).resolve() in resolved.parents):
                    in_scope = True
    return {
        "approved": in_scope and os.environ.get("ASG_ONBOARDING_AUTHORIZED", "0").strip() == "1"
        and os.environ.get("ASG_ONBOARDING_AUTO_INSTALL", "0").strip() == "1",
        "scope": os.environ.get("ASG_ONBOARDING_SCOPE", "").strip(),
        "workspace": workspace,
        "source": "supervisor_environment",
    }


def _target_is_live(target: dict[str, Any]) -> None:
    pid = int(target["pid"])
    expected = float(target["create_time"])
    proc = psutil.Process(pid)
    actual = proc.create_time()
    if abs(actual - expected) > 1e-3:
        raise ValueError("target PID reused during onboarding")


def execute_install(plan: dict[str, Any], target: dict[str, Any],
                    authorization: dict[str, Any] | None = None) -> dict[str, Any]:
    """执行唯一登记的安装器；失败关闭且不运行模型提供的命令。"""
    if plan.get("status") != "plan_pending_authorization":
        return {"status": plan.get("status", "invalid_plan"), "reason": plan.get("reason", "计划不可执行")}
    if plan.get('adapter') == 'file_plan':
        return _execute_file_plan(plan, target, authorization or {})
    contract = ADAPTER_REGISTRY.get(plan.get("adapter"))
    if not contract:
        result = {"status": "unsupported", "reason": "计划使用了未登记的接入适配器"}
        record_transition(target, "install_rejected", {"plan": plan, "install": result})
        return result
    auth = authorization or {}
    if not auth.get("approved"):
        result = {"status": "pending_authorization", "reason": "等待一次授权范围内的自动安装授权"}
        record_transition(target, "install_waiting_authorization", {"plan": plan, "install": result})
        return result
    if auth.get("scope") != "project":
        result = {"status": "unsupported", "reason": "当前安装器只支持 project 作用域，未获用户/宿主级授权"}
        record_transition(target, "install_rejected", {"plan": plan, "install": result})
        return result
    workspace = plan.get("workspace")
    if not workspace or auth.get("workspace") not in (None, workspace):
        result = {"status": "unsupported", "reason": "计划工作区缺失或与授权范围不一致"}
        record_transition(target, "install_rejected", {"plan": plan, "install": result})
        return result
    try:
        ws = ghost_install.ensure_workspace(Path(workspace))
        _target_is_live(target)
    except (OSError, ValueError, psutil.Error) as exc:
        result = {"status": "failed", "reason": "安装前检查失败: %s" % type(exc).__name__}
        record_transition(target, "install_failed", {"plan": plan, "install": result})
        return result

    name = plan.get("install", {}).get("plugin_name")
    if name != contract["plugin_name"] or plan.get("install", {}).get("installer") != contract["installer"]:
        result = {"status": "unsupported", "reason": "安装器名称不在登记契约中"}
        record_transition(target, "install_rejected", {"plan": plan, "install": result})
        return result
    existing, existing_error = load_manifest(ws, expected_workspace=ws)
    dest = ws / ".opencode" / "plugins" / name
    if existing and existing.get("name") == name and dest.is_file():
        try:
            existing_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
        except OSError:
            existing_hash = ""
        if existing_hash == existing.get("sha256"):
            events_path = resolve_events_file(ws, existing["runid"])
            result = {
                "status": "already_installed",
                "adapter": plan.get("adapter"),
                "workspace": str(ws),
                "plugin_path": str(dest),
                "runid": existing["runid"],
                "events_path": str(events_path),
                "activation_status": "verify_existing",
                "restart_required": plan.get("activation", {}).get("restart_required"),
                "hook_verified": False,
                "blocking": "unsupported",
            }
            record_transition(target, "install_reused", {"plan": plan, "install": result})
            return result
        result = {"status": "failed", "reason": "已有插件与 manifest hash 不一致，拒绝覆盖"}
        record_transition(target, "install_failed", {"plan": plan, "install": result})
        return result
    if existing_error is None and dest.exists():
        result = {"status": "failed", "reason": "工作区已有未登记插件文件，拒绝覆盖"}
        record_transition(target, "install_failed", {"plan": plan, "install": result})
        return result
    nonce = secrets.token_hex(16)
    try:
        # ghost_install 的 CLI 输出包含 nonce；直接调用并吞掉 stdout，避免进入日志/API。
        with contextlib.redirect_stdout(io.StringIO()):
            contract["install"](ws, name, nonce)
        manifest, error = load_manifest(ws, expected_workspace=ws)
        if error or not manifest or manifest.get("name") != name:
            raise ValueError(error or "installed manifest invalid")
        runid = manifest["runid"]
        events_path = resolve_events_file(ws, runid)
        result = {
            "status": "installed_pending_activation",
            "adapter": plan.get("adapter"),
            "workspace": str(ws),
            "plugin_path": str(ws / ".opencode" / "plugins" / name),
            "runid": runid,
            "events_path": str(events_path),
            "activation_status": "pending_restart",
            "restart_required": plan.get("activation", {}).get("restart_required"),
            "hook_verified": False,
            "blocking": "unsupported",
        }
    except (OSError, ValueError) as exc:
        result = {"status": "failed", "reason": "安装事务失败: %s" % type(exc).__name__}
    record_transition(target, "install_result", {"plan": plan, "install": result})
    return result


def verify_activation(install_result: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """使用已有事件校验器确认激活；没有事件只返回 pending_restart。"""
    if install_result.get("status") not in ("installed_pending_activation", "already_installed", "loaded_verified", "events_verified"):
        return {"status": install_result.get("status", "not_installed"),
                "reason": install_result.get("reason", "没有可验证的安装结果")}
    contract = ADAPTER_REGISTRY.get(install_result.get("adapter", SUPPORTED_ADAPTER))
    if not contract:
        result = {"status": "unsupported", "reason": "安装结果使用了未登记的接入适配器",
                  "hook_verified": False, "blocking": "unsupported"}
        record_transition(target, "activation_verification", {"install": install_result, "verification": result})
        return result
    try:
        _target_is_live(target)
        ws = Path(install_result["workspace"])
        manifest, error = load_manifest(ws, expected_workspace=ws)
        if error or not manifest:
            raise ValueError(error or "manifest unavailable")
        if manifest.get("runid") != install_result.get("runid"):
            raise ValueError("active manifest runid differs from the install plan")
        if manifest.get("active") is not True:
            result = {
                "status": "revoked",
                "health_status": "revoked",
                "healthy": False,
                "reason": "原安装 runid 的 manifest 已撤销；历史事件不再证明当前 Hook 生效",
                "valid_events": 0,
                "invalid_events": 0,
                "hook_loaded": False,
                "loaded_verified": False,
                "observing": False,
                "observing_verified": False,
                "hook_verified": False,
                "blocking": "unsupported",
                "instance_id": make_instance_id(target["pid"], target["create_time"]),
            }
            record_transition(target, "activation_verification", {"install": install_result, "verification": result})
            return result
        events_path = resolve_events_file(ws, manifest["runid"])
        verifier = EventVerifier(manifest["nonce"], target["pid"], target["create_time"],
                                 ttl_s=60.0, active=manifest.get("active") is True)
        rows = verifier.read_raw(events_path)
        valid, invalid = verifier.bound_events(rows)
        health = verifier.current_health(rows)
        has_loaded = any(e.get("event_type") == contract["activation_event"] for e in valid)
        has_observing = any(e.get("event_type") in contract["observing_events"] for e in valid)
        if has_loaded:
            status = "events_verified" if has_observing else "loaded_verified"
            reason = ("已收到绑定实例 hook.loaded 和真实工具事件；阻断仍未支持"
                      if has_observing else "已收到绑定实例 hook.loaded，但尚无真实工具事件")
        elif not valid:
            status = "pending_restart"
            reason = "安装完成但尚无绑定实例事件，需要引擎重载/下次启动"
        else:
            status = "verification_failed"
            reason = "收到事件但没有绑定的 hook.loaded 激活握手"
        result = {
            "status": status,
            "health_status": health.get("status"),
            "healthy": bool(health.get("healthy")),
            "reason": reason,
            "valid_events": len(valid),
            "invalid_events": len(invalid),
            "hook_loaded": has_loaded,
            "loaded_verified": has_loaded,
            "observing": has_observing,
            "observing_verified": has_loaded and has_observing,
            "hook_verified": has_loaded and has_observing,
            "blocking": "unsupported",
            "instance_id": make_instance_id(target["pid"], target["create_time"]),
        }
    except (OSError, ValueError, KeyError, psutil.Error) as exc:
        result = {"status": "verification_failed", "reason": "事件验证失败: %s" % type(exc).__name__,
                  "hook_verified": False, "blocking": "unsupported"}
    record_transition(target, "activation_verification", {"install": install_result, "verification": result})
    return result


def record_investigation(instance: dict[str, Any], struct: dict[str, Any], recipe: dict[str, Any],
                         evidence: list[dict[str, Any]], entry: dict[str, Any],
                         match_status: str, source: str = "goose", run_dir: str | None = None) -> dict[str, Any]:
    plan = plan_from_recipe(struct, recipe, match_status, entry=entry, recipe_source=source)
    payload = {
        "match_status": match_status,
        "fingerprint_id": entry.get("id"),
        "fingerprint_revision": entry.get("revision"),
        "recipe_source": source,
        "recipe": recipe,
        "evidence_refs": [e.get("evidence_id") for e in evidence if e.get("evidence_id")],
        "compatibility": copy.deepcopy(struct.get("compatibility")),
        "plan": plan,
    }
    if run_dir:
        plan['investigation_run_dir'] = str(Path(run_dir).resolve())
        plan['observed_compatibility'] = copy.deepcopy(struct.get('compatibility'))
        payload["run_dir"] = run_dir
    record_transition(instance, "investigation_recipe_saved", payload)
    return plan


def record_reuse(instance: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    record_transition(instance, "fingerprint_reused", {
        "match_status": plan.get("match_status"),
        "fingerprint_id": plan.get("fingerprint_id"),
        "fingerprint_revision": plan.get("fingerprint_revision"),
        "recipe_source": "fingerprint_reuse",
        "plan": plan,
    })
    return plan


def view_for_instance(instance_id: str, struct: dict[str, Any], match_result: dict[str, Any]) -> dict[str, Any]:
    """为 API 提供当前计划和历史摘要；读取异常明确显示，不阻断扫描。"""
    try:
        prior = instance_state(instance_id)
    except (OSError, ValueError) as exc:
        return {"status": "experience_unavailable", "reason": str(exc),
                "match_status": match_result.get("status")}
    if prior and isinstance(prior.get("plan"), dict):
        if match_result.get('status') == 'exact':
            # Recompute capability from the current matched recipe/code; retain
            # independent install/verification history, not a stale unsupported plan.
            prior = copy.deepcopy(prior)
            refreshed = plan_from_match(struct, match_result)
            original = prior['plan']
            # Same-instance candidate installation retains its bound source run.
            if (original.get('action') == 'install_candidate_recipe'
                    and original.get('investigation_run_dir')
                    and original.get('fingerprint_id') == refreshed.get('fingerprint_id')
                    and original.get('fingerprint_revision') == refreshed.get('fingerprint_revision')):
                refreshed.update(action='install_candidate_recipe',
                    investigation_run_dir=original['investigation_run_dir'])
            prior['plan'] = refreshed
        return {"status": prior.get("plan", {}).get("status", "known"),
                "plan": prior.get("plan"), "last_event": prior.get("last_event"),
                "verification": prior.get("verification"), "install": prior.get("install")}
    return {"status": "ready", "plan": plan_from_match(struct, match_result),
            "last_event": None}


def _execute_file_plan(plan, target, auth):
    """Generic learned plan dispatch; no product template and no process restart."""
    from runtime import learned_onboarding
    if not auth.get('approved'):
        return {'status': 'pending_authorization', 'reason': '接入目录不在部署授权范围内，尚未安装：' + str(plan.get('workspace') or '未查明目录')}
    workspace = plan.get('workspace')
    if auth.get('scope') != 'project' or not workspace or auth.get('workspace') != workspace:
        return {'status': 'failed', 'reason': 'file_plan authorization scope/workspace mismatch'}
    if plan.get('action') == 'install_reused_recipe':
        return _execute_reused_file_plan(plan, target, auth)
    try:
        _target_is_live(target)
        run = Path(plan['investigation_run_dir']).resolve(strict=True)
        recipe = json.loads((run / 'recipes' / 'candidate.json').read_text())['recipe']
        state = run / 'coordinator-state'
        state.mkdir(exist_ok=True)
        result = learned_onboarding.coordinate(
            recipe, run / 'evidence', target, workspace=Path(workspace), state_dir=state,
            authorization=learned_onboarding.authorization_scope(
                approved_workspace=workspace, allow_install=True,
                allow_rebind=os.environ.get('ASG_ONBOARDING_REBIND', '0') == '1'),
            observed_compatibility=plan.get('observed_compatibility'),
            evidence=plan.get('activation_evidence'), reason='authorized dashboard file_plan dispatch')
        record_transition(target, 'learned_file_plan_execution', {'plan': plan, 'install': result})
        return result
    except (OSError, ValueError, KeyError, psutil.Error) as exc:
        result = {'status': 'failed', 'reason': str(exc)}
        record_transition(target, 'install_failed', {'plan': plan, 'install': result})
        return result


def _reuse_run_root() -> Path:
    """Run root that holds investigation run directories (state and evidence)."""
    override = os.environ.get("ASG_RUN_DIR", "").strip()
    return Path(override) if override else ROOT / "artifacts" / "stage1" / "dashboard"


def _reuse_run_roots() -> "list[Path]":
    """All known state roots where investigation runs may have been persisted.

    Investigations have been written under the ASG_RUN_DIR override, the
    dashboard default root, and the autonomous-service directory. Exact reuse
    must locate the ORIGINAL record wherever it was written; each root is
    searched in order and nothing is synthesised when none matches.
    """
    roots = []
    override = os.environ.get("ASG_RUN_DIR", "").strip()
    if override:
        roots.append(Path(override))
    roots.append(ROOT / "artifacts" / "stage1" / "dashboard")
    roots.append(ROOT / "artifacts" / "autonomous-service")
    unique = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def _locate_reuse_material(plan_digest: str, workspace: str, refs: list[str]) -> dict[str, str] | None:
    """Find the original install record and the original evidence files.

    Exact reuse must be validated against the ORIGINAL instance, so both the
    installer record (it keeps the original target and build snapshot) and the
    original evidence files have to still exist. Nothing is synthesised when
    either is absent: the caller reports the gap instead of guessing.
    """
    for root in _reuse_run_roots():
      if not root.is_dir():
        continue
      try:
        runs = sorted(item for item in root.iterdir() if item.is_dir())
      except OSError:
        continue
      for run in runs:
        state = run / "coordinator-state"
        if not (state / "prepared_install.json").is_file():
            state = run / "installer-state"  # Earlier generic installer state layout.
        record = state / "prepared_install.json"
        if not record.is_file():
            continue
        try:
            prepared = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(prepared, dict):
            continue
        if prepared.get("plan_digest") != plan_digest or prepared.get("workspace") != workspace:
            continue
        evidence_dir = run / "evidence"
        if not all((evidence_dir / (ref + ".json")).is_file() for ref in refs):
            continue
        return {"run_dir": str(run), "state_dir": str(state), "evidence_dir": str(evidence_dir)}
    return None


def _reuse_blocked(missing: str, reason: str) -> dict[str, Any]:
    return {"status": "reuse_requires_validation", "action": "install_reused_recipe",
            "missing": missing, "reason": reason, "restart_required": None,
            "hook_verified": False, "blocking": "unsupported",
            "activation_status": "not_started"}


def _execute_reused_file_plan(plan: dict[str, Any], target: dict[str, Any],
                              auth: dict[str, Any]) -> dict[str, Any]:
    """Install an exact-reuse file plan from the ORIGINAL investigation record.

    The original evidence is validated against the original instance and build;
    the new target is used only for liveness and for the rebind that coordinate
    performs against the stored record. A missing original record or a missing
    original evidence file is reported as reuse_requires_validation rather than
    raising, and the old evidence is never re-bound to the new pid.
    """
    from runtime import learned_install, learned_onboarding
    workspace = plan.get("workspace")
    recipe = plan.get("reuse_recipe")
    if not isinstance(recipe, dict) or not recipe:
        return _reuse_blocked("original_recipe_missing", "缺少原调查配方；需重新调查")
    refs = [ref for ref in (plan.get("reuse_evidence_refs") or []) if isinstance(ref, str) and ref]
    if not refs:
        return _reuse_blocked("original_evidence_missing", "缺少原调查证据引用；需重新调查")
    try:
        _target_is_live(target)
        expected_plan = learned_install.plan_digest(recipe.get("install_plan"))
    except (OSError, ValueError, psutil.Error) as exc:
        result = {"status": "failed", "reason": "复用前置检查失败: %s" % type(exc).__name__}
        record_transition(target, "install_failed", {"plan": plan, "install": result})
        return result
    located = _locate_reuse_material(expected_plan, workspace, refs)
    if not located:
        result = _reuse_blocked(
            "original_install_record_or_evidence",
            "未找到原安装记录或原调查证据文件；需要迁移或重新调查，不能把旧证据绑定到新实例")
        record_transition(target, "reuse_requires_validation", {"plan": plan, "install": result})
        return result
    # The exact fingerprint match itself is the program's build verification:
    # the observed build was already proven equal to the recorded revision.
    reuse_evidence = ["exact reuse: fingerprint=%s revision=%s; observed build equals recorded build"
                      % (plan.get("fingerprint_id"), plan.get("fingerprint_revision"))]
    try:
        result = learned_onboarding.coordinate(
            recipe, Path(located["evidence_dir"]), target, workspace=Path(workspace),
            state_dir=Path(located["state_dir"]),
            authorization=learned_onboarding.authorization_scope(
                approved_workspace=workspace, allow_install=True,
                allow_rebind=os.environ.get("ASG_ONBOARDING_REBIND", "0") == "1"),
            observed_compatibility=plan.get("observed_compatibility"),
            evidence=reuse_evidence,
            reason="authorized exact file_plan reuse against the original record")
    except (OSError, ValueError, KeyError, psutil.Error) as exc:
        result = {"status": "failed", "reason": str(exc),
                  "action": "install_reused_recipe", "reuse_requires_validation": True}
    record_transition(target, "learned_file_plan_reuse", {"plan": plan, "install": result})
    return result
