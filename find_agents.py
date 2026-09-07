"""现场盘点: 用 sensor 同款画像规则给本机当前全量进程打分, 只读不写.

用法: python find_agents.py
输出: 按分数排序的疑似 agent 清单 (pid/进程名/分数/命中原因/命令行摘要).
不写 events.jsonl, 不做任何处置, 纯只读盘点.
"""
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

import psutil
from asg_os_sensor import Sensor, load_policies

s = Sensor(load_policies())
rows = []
total = 0
for proc in psutil.process_iter(["pid", "name", "cmdline"]):
    total += 1
    try:
        score, reasons = s.agent_score(proc)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        continue
    if score >= s.threshold:
        cmd = " ".join(proc.info.get("cmdline") or [])
        rows.append((score, proc.info.get("pid"), proc.info.get("name") or "",
                     "; ".join(reasons), cmd[:160]))

rows.sort(key=lambda r: (-r[0], r[1]))
print(f"扫描进程总数={total}  疑似agent={len(rows)}  阈值={s.threshold}", flush=True)
print("-" * 110, flush=True)
for score, pid, name, why, cmd in rows:
    print(f"[{score:3d}分] pid={pid:<7d} {name:<18s} {why}", flush=True)
    print(f"         cmd: {cmd}", flush=True)
