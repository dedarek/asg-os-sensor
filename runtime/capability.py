"""Per-instance capability ladder: what is actually proven, in order.

"The Hook is installed" and "we can see every input/output and control the
Agent" are different claims.  This module keeps them apart and reports each
Stage with the evidence that supports it, the state it is in, and the specific
thing blocking the next one.  It is a pure projection: no I/O, no target
knowledge, no invented evidence.

States
------
proven       Evidence exists for this exact instance.
pending      Configured or installed, activation not observed yet.
blocked      A named external requirement is unmet.
unsupported  The target does not expose this capability at all.
unknown      No evidence either way; cannot be claimed and cannot be denied.
not_reached  An earlier stage has not completed.
"""
from __future__ import annotations

from typing import Any

STAGES = (
    ("discovered", "发现"),
    ("investigated", "调查"),
    ("recipe_ready", "接入方案"),
    ("installed", "安装"),
    ("trusted", "原生准入"),
    ("loaded", "加载"),
    ("observing", "事件观测"),
    ("io_coverage", "输入输出"),
    ("controlling", "执行控制"),
    ("serving", "独立运行"),
)

STATE_LABELS = {
    "proven": "已验证",
    "pending": "待生效",
    "blocked": "被阻断",
    "unsupported": "不支持",
    "unknown": "无证据",
    "not_reached": "未到达",
}

_INVESTIGATED = ("succeeded", "assets_collected", "reused")
_INSTALLED = ("installed", "already_installed", "bound", "rebound",
              "activation_rebound", "installed_no_observation")


def _stage(stage_id: str, state: str, detail: str, evidence: Any = None, gap: str = "") -> dict:
    label = dict(STAGES)[stage_id]
    return {"id": stage_id, "label": label, "state": state,
            "state_label": STATE_LABELS.get(state, state), "detail": detail,
            "evidence": evidence, "gap": gap}


def _present(value: Any) -> bool:
    return isinstance(value, dict) and bool(value)


def scoped_trust(report, process):
    """A probe applies only to the same executable and configuration directory.

    Name matching is insufficient: embedded runtimes and different homes may
    load unrelated hook definitions. Unknown scope must stay unknown.
    """
    import os
    source = (report or {}).get('source') or {}
    binary, cwd = source.get('binary'), source.get('cwd')
    if (binary and cwd and process.get('exe') and process.get('cwd')
            and os.path.realpath(binary) == os.path.realpath(process['exe'])
            and os.path.realpath(cwd) == os.path.realpath(process['cwd'])):
        return report
    return {'status': 'scope_unverified', 'reason': '探测进程与目标的可执行文件或配置目录未匹配'}


def build(target: Any = None, *, investigation: Any = None, fingerprint: Any = None,
          install: Any = None, verify: Any = None, observation: Any = None,
          io: Any = None, control: Any = None, native_trust: Any = None,
          serving: Any = None, times: Any = None) -> dict:
    """Build the ladder for one instance from already-collected facts."""
    times = times if isinstance(times, dict) else {}
    target = target if isinstance(target, dict) else {}
    install = install if isinstance(install, dict) else {}
    verify = verify if isinstance(verify, dict) else {}
    observation = observation if isinstance(observation, dict) else {}
    io = io if isinstance(io, dict) else {}

    stages: list[dict] = []

    has_target = bool(target.get("pid"))
    stages.append(_stage(
        "discovered", "proven" if has_target else "unknown",
        ("实例 %s:%s" % (target.get("pid"), target.get("create_time"))) if has_target
        else "尚无绑定实例",
        gap="" if has_target else "需要 pid 与 create_time 才能做实例级判断"))

    inv_status = (investigation or {}).get("status") if _present(investigation) else None
    if inv_status in _INVESTIGATED:
        inv_state, inv_detail = "proven", (investigation or {}).get("label") or "资产调查已保存"
    elif inv_status in ("running", "queued", "deferred"):
        inv_state, inv_detail = "pending", (investigation or {}).get("label") or "调查执行中"
    elif inv_status:
        inv_state, inv_detail = "not_reached", (investigation or {}).get("label") or str(inv_status)
    else:
        inv_state, inv_detail = "not_reached", "尚未调查"
    stages.append(_stage("investigated", inv_state, inv_detail,
                         gap="" if inv_state == "proven" else "调查未完成时，后续阶段无法评估"))

    recipe = (fingerprint or {}).get("hook_recipe") if _present(fingerprint) else None
    plan = install.get("plan") if _present(install.get("plan")) else None
    has_recipe = bool(recipe) or bool(plan)
    stages.append(_stage(
        "recipe_ready", "proven" if has_recipe else "not_reached",
        ("指纹 %s rev%s" % (fingerprint.get("id"), fingerprint.get("revision")))
        if _present(fingerprint) else ("安装方案已生成" if plan else "尚未生成接入方案"),
        gap="" if has_recipe else "需要先完成调查并生成接入方案"))

    install_status = install.get("status")
    files = install.get("files") if isinstance(install.get("files"), list) else []
    if install_status in _INSTALLED or install.get("installed") is True:
        install_state, install_detail = "proven", "已写入 %d 个 Hook 文件" % len(files) if files else "已安装"
    elif install_status in ("plan_pending_authorization", "pending"):
        install_state, install_detail = "pending", "方案待授权"
    elif has_recipe:
        install_state, install_detail = "not_reached", "有方案，尚未安装"
    else:
        install_state, install_detail = "not_reached", "尚未安装"
    stages.append(_stage("installed", install_state, install_detail,
                         evidence=[f.get("path") for f in files if isinstance(f, dict)][:16] or None,
                         gap="" if install_state == "proven" else "安装是文件写入，不代表已生效"))

    trust = native_trust if isinstance(native_trust, dict) else {}
    owned = trust.get("owned") if isinstance(trust.get("owned"), dict) else None
    trust_state, trust_detail, trust_gap = "unknown", "尚未探测目标原生信任状态", "需要运行一次目标原生 hooks/list 探测"
    if trust.get("status") == "probed" and owned:
        if owned.get("total", 0) == 0:
            trust_state, trust_detail = "not_reached", "目标原生配置中未注册我们的 Hook"
            trust_gap = "需要先完成安装并让目标读到该定义"
        elif owned.get("trusted", 0) == owned.get("total"):
            trust_state, trust_detail, trust_gap = "proven", "全部 %d 个 Hook 已 trusted" % owned["total"], ""
        elif owned.get("untrusted", 0):
            trust_state = "blocked"
            trust_detail = "%d/%d 个 Hook untrusted；重启或重新加载同一配置仍会被跳过" % (
                owned.get("untrusted", 0), owned.get("total", 0))
            trust_gap = "需要由目标自身完成信任审阅（我们不会代替用户批准）"
        else:
            trust_state, trust_detail = "unknown", "探测到定义但信任状态未知"
    elif trust.get("status") == "scope_unverified":
        trust_detail, trust_gap = "现有信任探测不适用于该实例", "需要匹配目标运行时与配置范围的原生准入证据"
    elif trust.get("status") and trust.get("status") != "probed":
        trust_detail, trust_gap = "无法探测（%s）" % trust.get("reason"), "目标未安装或探测失败"
    dupes = (owned or {}).get("duplicate_registrations") or []
    stages.append(_stage("trusted", trust_state, trust_detail,
                         evidence={"by_trust": (owned or {}).get("by_trust"),
                                   "duplicate_registrations": dupes} if owned else None,
                         gap=trust_gap))

    health = observation.get("health") if isinstance(observation.get("health"), dict) else {}
    loaded = bool(health.get("loaded_observed")) or bool(verify.get("hook_loaded"))
    alive = observation.get("target_alive", verify.get("target_alive"))
    if loaded:
        loaded_state, loaded_detail = "proven", "已观察到 Hook 加载事件"
    elif trust_state == "blocked":
        loaded_state, loaded_detail = "blocked", "信任准入未通过，加载事件不可能出现"
    elif install_state == "proven":
        loaded_state, loaded_detail = "pending", "已安装，等待目标下次加载触发"
    else:
        loaded_state, loaded_detail = "not_reached", "尚未安装"
    stages.append(_stage("loaded", loaded_state, loaded_detail,
                         gap="" if loaded_state == "proven" else "重启或新会话后才会读取 Hook 配置"))

    events = observation.get("events") if isinstance(observation.get("events"), dict) else {}
    valid_events = int(events.get("valid") or verify.get("valid_events") or 0)
    if valid_events and alive is True:
        obs_state, obs_detail = "proven", "%d 条有效事件（实例存活）" % valid_events
    elif valid_events:
        obs_state, obs_detail = "unknown", "窗口内有 %d 条事件，但目标当前不可确认存活" % valid_events
    elif loaded_state == "proven":
        obs_state, obs_detail = "pending", "已加载，尚无工具活动"
    else:
        obs_state, obs_detail = "not_reached", "尚无事件"
    stages.append(_stage("observing", obs_state, obs_detail,
                         evidence={"valid": valid_events, "invalid": int(events.get("invalid") or 0)}
                         if events else None,
                         gap="" if obs_state == "proven" else "需要目标真实执行一次产生事件的操作"))

    io_status = io.get("status")
    io_caps = io.get("capabilities") if isinstance(io.get("capabilities"), list) else []
    observed_kinds = [c.get("event") for c in io_caps if c.get("observed")]
    if io_status == "checkpoint_verified":
        io_state, io_detail = "proven", "已上报轮次的计数与配对通过"
    elif io_status == "gaps":
        io_state = "pending"
        io_detail = "已捕获 %d/%d 类事件，仍存在缺口" % (len(observed_kinds), len(io_caps) or 6)
    elif io_status == "not_observed":
        io_state, io_detail = "pending", "尚无输入输出证据"
    else:
        io_state, io_detail = "unknown", "未评估"
    stages.append(_stage("io_coverage", io_state, io_detail,
                         evidence={"observed_kinds": observed_kinds,
                                   "end_to_end_verified": bool(io.get("end_to_end_verified"))} if io else None,
                         gap="端到端完整性需要独立入口对账，当前不宣称已证明"
                             if io_state != "proven" else ""))

    control = control if isinstance(control, dict) else {}
    verification = None
    for item in (control.get("verifications") or []):
        if isinstance(item, dict) and item.get("current") is True:
            verification = item
            break
    from runtime.hook_acceptance import verified_checks
    checks = verified_checks((verification or {}).get("checks"))
    blocking_supported = (observation.get("blocking") or {}).get("status")
    if {"allow_effect", "deny_effect", "decision_wait"}.issubset(checks):
        ctl_state, ctl_detail = "proven", "该实例已验证执行前等待决定、放行和拒绝的实际效果"
    elif blocking_supported == "unsupported":
        ctl_state, ctl_detail = "unsupported", "该目标当前挂接位置不支持阻断"
    elif verification is not None:
        ctl_state, ctl_detail = "pending", "缺少执行前等待决定或放行/拒绝的实际效果验证"
    else:
        ctl_state, ctl_detail = "not_reached", "尚无该实例的控制验证"
    stages.append(_stage("controlling", ctl_state, ctl_detail,
                         evidence={"checks": sorted(checks)} if checks else None,
                         gap="" if ctl_state == "proven" else "决策下发不等于执行生效，需要实际效果验证"))

    serving = serving if isinstance(serving, dict) else {}
    if (serving.get("status") == "running" and serving.get("covers_instance") is True
            and serving.get("independent_acceptance_verified") is True):
        srv_state, srv_detail = "proven", "独立运行服务正在为该实例提供数据与控制"
    elif serving.get("status") == "running":
        srv_state, srv_detail = "pending", "独立服务已运行；该实例脱离发现程序后的采集与控制尚未验收"
    else:
        srv_state, srv_detail = "not_reached", "独立运行服务未运行"
    stages.append(_stage("serving", srv_state, srv_detail,
                         evidence={"url": serving.get("url"), "endpoints": serving.get("endpoints")}
                         if serving else None,
                         gap="" if srv_state == "proven" else "发现程序退出后，需要独立服务继续提供事件与控制"))

    proven = [s["id"] for s in stages if s["state"] == "proven"]
    # Each passing status carries the time its evidence was recorded, so the
    # page can show when a state was verified rather than only that it passed.
    for stage in stages:
        stage["at"] = times.get(stage["id"])
    blockers = [{"stage": s["id"], "label": s["label"], "state": s["state"],
                 "reason": s["detail"], "gap": s["gap"]}
                for s in stages if s["state"] in ("blocked", "unknown", "pending")]
    unresolved = [s for s in stages if s["state"] not in ("proven", "unsupported")]
    next_step = unresolved[0] if unresolved else None
    reached = [s["label"] for s in stages if s["state"] == "proven"]
    return {
        "version": 1,
        "scope": "single_instance",
        "target": {"pid": target.get("pid"), "create_time": target.get("create_time")},
        "stages": stages,
        "proven": proven,
        "blockers": blockers,
        "next_step": {"stage": next_step["id"], "label": next_step["label"],
                       "gap": next_step["gap"] or next_step["detail"]} if next_step else None,
        "summary": ("已验证：%s" % "、".join(reached)) if reached else "尚无已验证阶段",
        "limitations": [
            "阶梯按实例计算；一个实例的验证不能继承给另一个实例",
            "每一级只代表该级证据，不代表更高级能力",
            "unknown 既不能当作通过，也不能当作失败",
        ],
    }


__all__ = ["STAGES", "STATE_LABELS", "build"]
