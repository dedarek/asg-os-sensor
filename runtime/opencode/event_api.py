# -*- coding: utf-8 -*-
"""ASG OpenCode 观测插件事件接收/读取/健康验证 API（库对象；HTTP 接线见 server.py）。

规则：接收端不信任插件自报 pid/随机文件名——校验 nonce + psutil(pid).create_time() 与
可信快照比对；先 schema+绑定过滤再做时间与关联判定；撤销由安装器 manifest 驱动；
空闲无事件为 unknown 而非故障。"""
import json, time
from datetime import datetime, timezone
from pathlib import Path
import psutil

SCHEMA_KEYS = {"ts", "event_type", "adapter_source", "nonce", "pid"}
FUTURE_GRACE_S = 5.0  # 允许的时钟偏差
MAX_READ_BYTES = 4 * 1024 * 1024


def _ts(ev):
    try:
        return datetime.fromisoformat(ev["ts"].replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError, TypeError):
        return None


class EventVerifier:
    def __init__(self, expected_nonce, expected_pid, expected_create_time, ttl_s=60.0, active=True):
        self.expected_nonce = expected_nonce
        self.expected_pid = int(expected_pid)
        self.expected_ct = float(expected_create_time)
        self.ttl_s = float(ttl_s)
        self.active = bool(active)

    # -- schema 与绑定 ------------
    def minimal_schema_ok(self, ev):
        return isinstance(ev, dict) and all(k in ev for k in SCHEMA_KEYS)

    def verify_instance(self, ev):
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
            raise ValueError("pid unavailable") from exc
        if pid != self.expected_pid or abs(live_ct - self.expected_ct) > 1e-3:
            raise ValueError("instance binding mismatch")

    # -- 读取 ------------
    def read_raw(self, path):
        rows = []
        p = Path(path)
        if not p.exists():
            return rows
        try:
            if p.stat().st_size > MAX_READ_BYTES:
                return rows
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return rows
        for line in text.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
        return rows

    def bound_events(self, rows):
        valid, invalid = [], []
        for ev in rows:
            try:
                self.verify_instance(ev)
            except ValueError:
                invalid.append(ev)
            else:
                valid.append(ev)
        return valid, invalid

    def valid_time(self, ev):
        t = _ts(ev)
        if t is None:
            return False
        return t <= time.time() + FUTURE_GRACE_S  # 未来容差外拒绝

    # -- 健康 ------------
    def loaded_observed(self, rows):
        valid, _ = self.bound_events(rows)
        return any(e.get("event_type") == "hook.loaded" for e in valid)

    def current_health(self, rows):
        if not self.active:
            return {"status": "revoked", "healthy": False, "reason": "installer marked inactive"}
        if not rows:
            return {"status": "unknown", "healthy": False, "reason": "no events (idle, not fault)"}
        valid, invalid = self.bound_events(rows)
        if not valid:
            return {"status": "unbound", "healthy": False, "reason": "no bound events"}
        # 旧合法 loaded 不能掩盖新非法 nonce/绑定/未来时间戳
        if invalid and any(self.valid_time(i) for i in invalid):
            return {"status": "unbound", "healthy": False, "reason": "fresh invalid event present"}
        fresh = [e for e in valid if self.valid_time(e)]
        if not fresh:
            return {"status": "stale", "healthy": False, "reason": "no valid-time events"}
        if time.time() - max(_ts(e) for e in fresh) > self.ttl_s + FUTURE_GRACE_S:
            return {"status": "stale", "healthy": False, "reason": "latest event older than ttl"}
        if not any(e.get("event_type") == "hook.loaded" for e in fresh):
            return {"status": "nohandshake", "healthy": False, "reason": "no fresh bound hook.loaded"}
        return {"status": "healthy", "healthy": True, "reason": "active+fresh+bound+handshake"}

    def healthy(self, rows):
        return self.current_health(rows)["healthy"]

    def correlate(self, rows, call_id):
        valid, _ = self.bound_events(rows)
        b = [r for r in valid if r.get("event_type") == "tool.execute.before" and r.get("call_id") == call_id]
        a = [r for r in valid if r.get("event_type") == "tool.execute.after" and r.get("call_id") == call_id]
        return {"before": len(b), "after": len(a), "paired": bool(b and a)}

    def revoked(self, rows):
        return not self.active

    def api_status(self):
        return {"type": "library", "wired": False, "endpoints": {"health": "not_wired", "events": "not_wired"}}