# -*- coding: utf-8 -*-
"""ASG OpenCode 观测插件事件接收/读取/健康验证 API（库对象；HTTP 接线见 server.py）。

接收端职责：不信任插件自报的 pid 或随机文件名。
- 校验 nonce 匹配预期（接收端生成并写入 .asg-observe/runs/<runid>/nonce）。
- 校验实例绑定：psutil.Process(pid).create_time() 与预期快照比对，缺 pid/PID 复用均拒。
- 健康语义（评审要求）：
  * 先按最小 schema+绑定过滤 rows，再做时间与关联判定，避免旧合法 loaded + 新非法
    nonce / 未来 ts 导致误判 healthy。
  * 时间：拒绝未来时间（可容忍过小的时钟偏差）与超过 ttl 的旧事件。
  * 撤销：由安装器持久状态驱动（manifest.active=False 或 manifest 缺失），
    而非仅靠事件内 hook.revoked 标记。
  * 空闲无事件 -> stale/unknown，不直接断言故障。
- read_raw 稳健拒绝数组/数字（只接受单行 JSON 对象）。
- api_status：当前为库对象，未接线；隔离 HTTP 服务在 server.py 提供 /health /events。
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

SCHEMA_KEYS = {"ts", "event_type", "adapter_source", "nonce", "pid"}
FUTURE_GRACE_S = 5.0  # 允许的时钟偏差


def _ts(ev):
    try:
        return datetime.fromisoformat(ev["ts"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError):
        return None


class EventVerifier:
    def __init__(self, expected_nonce: str, expected_pid: int, expected_create_time: float,
                 ttl_s: float = 60.0, active: bool = True):
        self.expected_nonce = expected_nonce
        self.expected_pid = int(expected_pid)
        self.expected_ct = float(expected_create_time)
        self.ttl_s = float(ttl_s)
        self.active = bool(active)  # 由安装器 manifest 驱动的撤销状态

    # -- 绑定与 schema ------------------------------------------------
    def minimal_schema_ok(self, ev) -> bool:
        if not isinstance(ev, dict):
            return False
        for k in SCHEMA_KEYS:
            if k not in ev:
                return False
        return True

    def verify_instance(self, ev: dict) -> None:
        if not self.minimal_schema_ok(ev):
            raise ValueError("missing required fields")
        if ev.get("nonce") != self.expected_nonce:
            raise ValueError("nonce mismatch")
        pid = ev.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            raise ValueError("invalid pid")
        try:
            live_ct = psutil.Process(pid).create_time()
        except psutil.Error as exc:
            raise ValueError("pid unavailable: %s" % exc) from exc
        if pid != self.expected_pid or abs(live_ct - self.expected_ct) > 1e-3:
            raise ValueError("instance binding mismatch")

    # -- 读取 -----------------------------------------------------------
    def read_raw(self, path) -> list:
        rows = []
        p = Path(path)
        if not p.exists():
            return rows
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):  # 稳健拒绝数组/数字/字符串
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
        return rows

    def bound_events(self, rows) -> tuple:
        """先 schema+绑定过滤。返回 (valid, invalid)。"""
        valid, invalid = [], []
        for ev in rows:
            try:
                self.verify_instance(ev)
            except ValueError:
                invalid.append(ev)
            else:
                valid.append(ev)
        return valid, invalid

    # -- 时间 ------------------------------------------------
    def valid_time(self, ev) -> bool:
        t = _ts(ev)
        if t is None:
            return False
        now = time.time()
        if t > now + FUTURE_GRACE_S:  # 未来时间（容差外）拒绝
            return False
        return True

    # -- 健康 -----------------------------------------------------------
    def loaded_observed(self, rows) -> bool:
        valid, _ = self.bound_events(rows)
        return any(e.get("event_type") == "hook.loaded" for e in valid)

    def current_health(self, rows) -> dict:
        """返回 pinned: 未接线。健康判定：active + fresh + bound + associated。"""
        # 撤销：安装器持久状态驱动（manifest 缺失/active=False）
        if not self.active:
            return {"status": "revoked", "healthy": False, "reason": "installer marked inactive"}
        if not rows:
            return {"status": "unknown", "healthy": False, "reason": "no events (idle, not fault)"}
        valid, invalid = self.bound_events(rows)  # 先 schema+绑定过滤
        if not valid:
            return {"status": "unbound", "healthy": False, "reason": "no bound events (invalid=%d)" % len(invalid)}
        # 旧合法 loaded 不能掩盖新出现的非法 nonce/绑定事件
        if invalid and any(i.get("ts") and self.valid_time(i) for i in invalid):
            return {"status": "unbound", "healthy": False, "reason": "fresh invalid event present"}
        if not any(self.valid_time(e) for e in valid):
            return {"status": "stale", "healthy": False, "reason": "no fresh events within ttl or future timestamps"}
        now = time.time()
        latest = max(_ts(e) for e in valid if self.valid_time(e))
        if now - latest > self.ttl_s + FUTURE_GRACE_S:
            return {"status": "stale", "healthy": False, "reason": "latest event older than ttl"}
        if not any(e.get("event_type") == "hook.loaded" for e in valid):
            return {"status": "nohandshake", "healthy": False, "reason": "no bound hook.loaded"}
        return {"status": "healthy", "healthy": True, "reason": "active+fresh+bound+associated"}

    def healthy(self, rows) -> bool:
        return self.current_health(rows)["healthy"]

    def correlate(self, rows, call_id: str) -> dict:
        valid, _ = self.bound_events(rows)
        b = [r for r in valid if r.get("event_type") == "tool.execute.before" and r.get("call_id") == call_id]
        a = [r for r in valid if r.get("event_type") == "tool.execute.after" and r.get("call_id") == call_id]
        return {"before": b, "after": a, "paired": bool(b and a)}

    def revoked(self, rows) -> bool:
        return not self.active

    # -- API 接线状态 ---------------------------------------------------
    def api_status(self) -> dict:
        return {"type": "library", "wired": False,
                "endpoints": {"health": "not_wired", "events": "not_wired"}}