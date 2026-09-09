# -*- coding: utf-8 -*-
"""ASG OpenCode 观测插件事件接收/读取/健康验证最小 API。

接收端职责（不信任插件自报的 pid 或随机文件名）：
- 校验 nonce 匹配本次预期（由接收端生成并注入环境 ASG_OBSERVE_NONCE）。
- 校验实例绑定：用 psutil.Process(pid).create_time() 与预期 create_time 比对
  （预期值来自接收端对目标引擎段的快照），不等即拒绝。
- 事件读取：解析行分隔 JSON，按 event_type 过滤。
- 健康：hook.loaded 事件存在且绑定有效才算"插件已加载"（未握手不得显示已安装生效）。
"""
import json
import os
from pathlib import Path

import psutil


class EventVerifier:
    def __init__(self, expected_nonce: str, expected_pid: int, expected_create_time: float):
        self.expected_nonce = expected_nonce
        self.expected_pid = int(expected_pid)
        self.expected_ct = float(expected_create_time)

    def verify_instance(self, ev: dict) -> None:
        """接收端验证事件归属：自报 pid + 本地 create_time 必须同时匹配预期。"""
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

    def read_events(self, path):
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

    def healthy(self, rows: list[dict]) -> bool:
        """健康：存在 hook.loaded 且绑定校验通过。"""
        for ev in rows:
            if ev.get("event_type") == "hook.loaded":
                try:
                    self.verify_instance(ev)
                except ValueError:
                    continue
                return True
        return False
