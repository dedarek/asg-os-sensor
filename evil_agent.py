"""恶意 agent 模拟器 (无害化): 依次触发 R1/R4/R2/R3, 用于验证 sensor 能否从OS层检出.

触发设计 (全部无害, 字符串只出现在本进程 cmdline/打开文件中):
  R1 画像:   cmdline 自带 --model/--api-key + demo 标记  -> score>=50
  R4 危险命令: 启动参数里带字面 "rm -rf /tmp/asg-demo" (本脚本忽略该参数, 不执行)
  R2 敏感文件: 打开 honey/id_rsa_fake (项目内假文件) 并读一字节
  R3 未知出站: 向 http://example.com 发一次短超时请求 (失败也算, 建连动作已被记录)

用法:
  python evil_agent.py --model fake --api-key DEMO "rm -rf /tmp/asg-demo"
运行约 20 秒, 给 sensor 留出多轮扫描窗口.
"""
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
print(f"[evil] 启动 pid 自查中, argv={sys.argv}", flush=True)

# R2: 打开蜜罐敏感文件并全程持有句柄 (模拟真实攻击常驻打开行为;
# 轮询式采集抓不住一闪而过的 open, 句柄常驻才是可观测形态)
honey = BASE / "honey" / "id_rsa_fake"
try:
    honey_fh = open(honey, "rb")
    honey_fh.read(1)
    print(f"[evil] 已打开并持有蜜罐文件 {honey}", flush=True)
except Exception as e:
    honey_fh = None
    print(f"[evil] 读蜜罐失败: {e}", flush=True)

# R3: 建连并全程持有 (同理: 瞬时建连轮询抓不住, 常驻 ESTABLISHED 才看得见)
import socket
held_sock = None
try:
    held_sock = socket.create_connection(("example.com", 80), timeout=5)
    print(f"[evil] 出站 example.com:80 已建连并持有 {held_sock.getsockname()}", flush=True)
except Exception as e:
    print(f"[evil] 出站建连失败(动作仍可能被记): {type(e).__name__}", flush=True)

# 存活 20s, 等 sensor 多轮扫描 (R4 的 kill 可能提前终结本进程, 那也算拦截成功)
for i in range(20):
    print(f"[evil] 存活 {i + 1}/20s ...", flush=True)
    time.sleep(1)
print("[evil] 结束 (未被拦截)", flush=True)
