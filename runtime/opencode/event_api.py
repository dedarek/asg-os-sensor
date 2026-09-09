# -*- coding: utf-8 -*-
"""ASG OpenCode 观测插件事件接收/读取/健康验证最小 API。

接收端职责（不信任插件自报的 pid 或随机文件名）：
- 校验 nonce 匹配本次预期（由接收端生成并写入插件同目录 .asg-observe/nonce）。
- 校验实例绑定：psutil.Process(pid).create_time() 与预期快照比对（不等即拒绝；
  缺 pid、PID 复用均拒绝）。
- 事件读取：解析行分隔 JSON，按 event_type 过滤；返回经过验证的记录。
- 健康语义（区分 loaded vs current，评审要求）：
  * loaded_observed：历史上曾出现 hook.loaded 且绑定有效（「加载过」）。
  * current_health：事件新鲜（最近事件 ts 距今 < ttl）、绑定有效、存在事件关联
    （before/after 或 hook.loaded 自身），且未被撤销。卸载/撤销后 current 不健康。
  * revoke：events 文件被删除/替换或出现 event_type=hook.revoked 即视为撤销。
- API 接线状态：本类仅是库对象，尚未接入 HTTP/事件总线（endpoints 标注
  not_wired）。接入后由网关提供 /health 与 /events。
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil


class EventVerifier:
    def __init__(self, expected_nonce: str, expected_pid: int, expected_create_time: float,
                 ttl_s: float = 60.0):
        self.expected_nonce = expected_nonce
        self.expected_pid = int(expected_pid)
        self.expected_ct = float(expected_create_time)
        self.ttl_s = float(ttl_s)

    # -- 绑定 ---------------------------------------------------------------
    def verify_instance(self, ev: dict) -> None:
        if ev.get("nonce") != self.expected_nonce:
            raise ValueError("nonce mismatch (expected %r)" % self.expected_nonce)
        pid = ev.get("pid")
        if pid is None:
            raise ValueError("event missing pid")
        try:
            live_ct = psutil.Process(int(pid)).create_time()
        except psutil.Error as exc:
            raise ValueError("pid unavailable: %s" % exc) from exc
        if int(pid) != self.expected_pid or abs(live_ct - self.expected_ct) > 1e-3:
            raise ValueError("instance binding mismatch (pid=%s live_ct=%s expected=%s:%s)" %
                             (pid, live_ct, self.expected_pid, self.expected_ct))

    # -- 读取 ---------------------------------------------------------------
    def read_raw(self, path) -> list:
        rows = []
        p = Path(path)
        if not p.exists():
            return rows
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def read_events(self, path):
        """读取并验证每条事件；返回 (valid, invalid)。非容器方法、不静默信任。"""
        valid, invalid = [], []
        for ev in self.read_raw(path):
            try:
                self.verify_instance(ev)
            except (ValueError, KeyError):
                invalid.append(ev)
            else:
                valid.append(ev)
        return valid, invalid

    # -- 健康 ---------------------------------------------------------------
    def _ts(self, ev):
        try:
            return datetime.fromisoformat(ev["ts"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError):
            return None

    def loaded_observed(self, rows) -> bool:
        for ev in rows:
            if ev.get("event_type") == "hook.loaded":
                try:
                    self.verify_instance(ev)
                except ValueError:
                    continue
                return True
        return False

    def current_health(self, rows) -> dict:
        if not rows:
            return {"healthy": False, "reason": "no events"}
        if any(r.get("event_type") == "hook.revoked" for r in rows):
            return {"healthy": False, "reason": "revoked"}
        now = time.time()
        latest = max(((self._ts(r) or 0) for r in rows), default=0)
        if latest == 0 or now - latest > self.ttl_s:
            return {"healthy": False, "reason": "stale (no fresh events within ttl)"}
        for ev in rows:
            try:
                self.verify_instance(ev)
            except ValueError:
                continue
            return {"healthy": True, "reason": "bound+fresh+associated"}
        return {"healthy": False, "reason": "no bound event"}

    def healthy(self, rows) -> bool:
        return self.loaded_observed(rows) and self.current_health(rows)["healthy"]

    # -- 关联与撤销 ---------------------------------------------------------
    def correlate(self, rows, call_id: str):
        """按 call_id 返回 {before, after} 事件对（前提：已验证的绑定）。"""
        b = [r for r in rows if r.get("event_type") == "tool.execute.before" and r.get("call_id") == call_id]
        a = [r for r in rows if r.get("event_type") == "tool.execute.after" and r.get("call_id") == call_id]
        return {"before": b, "after": a}

    def revoked(self, rows) -> bool:
        return any(r.get("event_type") == "hook.revoked" for r in rows) or not rows

    # -- API 接线状态 -------------------------------------------------------
    def api_status(self) -> dict:
        return {"type": "library", "wired": False,
                "endpoints": {"health": "not_wired", "events": "not_wired"}}
