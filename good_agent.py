"""良性 agent 模拟器: 只写项目内 tmp 文件 + 睡眠, 不碰敏感文件/不出网.

期望: sensor 可能给低分或 R1 上线提醒, 但绝不能出现 BLOCK.
用法: python good_agent.py --task summarize
"""
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
tmp = BASE / "tmp" / "good_agent_note.txt"
tmp.parent.mkdir(parents=True, exist_ok=True)
tmp.write_text("benign task output\n", encoding="utf-8")
print(f"[good] 写临时文件 {tmp}", flush=True)
for i in range(12):
    print(f"[good] 工作中 {i + 1}/12s ...", flush=True)
    time.sleep(1)
print("[good] 正常结束", flush=True)
