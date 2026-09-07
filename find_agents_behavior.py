"""纯行为画像(禁用名字关键词、禁用 API 域名表): 多轮采样找 agent.

只用结构与行为信号, 一个名字不查、一份域名表不带:
  C1 子进程数(编排干活):     每轮 children() 最大值 >=3 -> +20
  C2 出站集中度(API 客户端指纹): 去重后公网目标<=3 且公网连接累计>=3 -> +20
      (反复打同一几个 IP 是 API 调用形态; 浏览器目标分散, 天然排除)
  C3 跨轮复现(节律): 同一目标在>=3 轮出现 -> +15
  C4 服务形态: 持有 LISTEN 端口 -> +10
  C5 常驻且对外: 存活>2h 且有公网连接 -> +10
  C6 进程树深度: 有孙进程(>=2层) -> +10
阈值 50. 用法: python find_agents_behavior.py [轮数] [间隔秒]
"""
import sys
import time
from collections import defaultdict

import psutil

ROUNDS = int(sys.argv[1]) if len(sys.argv) > 1 else 6
INTERVAL = float(sys.argv[2]) if len(sys.argv) > 2 else 10


def is_public(ip):
    if not ip:
        return False
    if ip.startswith("127.") or ip == "::1":
        return False
    if ip.startswith("10.") or ip.startswith("192.168."):
        return False
    if ip.startswith("172."):
        try:
            if 16 <= int(ip.split(".")[1]) <= 31:
                return False
        except (ValueError, IndexError):
            pass
    if ":" in ip and not ip[0].isdigit():
        return False  # ipv6 本地/未解析略过
    return True


def depth(proc):
    """迭代版进程树深度, 异常一律吞(Windows 上子进程随时会死/无权限)."""
    best = 0
    try:
        stack = [(proc, 0)]
    except Exception:
        return 0
    seen = set()
    while stack:
        cur, lv = stack.pop()
        try:
            kids = cur.children()
        except Exception:
            continue
        if not kids:
            best = max(best, lv)
            continue
        best = max(best, lv + 1)
        if lv + 1 >= 3:
            continue
        for k in kids:
            try:
                if k.pid in seen:
                    continue
                seen.add(k.pid)
                stack.append((k, lv + 1))
            except Exception:
                continue
    return best


stat = {}  # pid -> dict
now = time.time()
for r in range(ROUNDS):
    for p in psutil.process_iter(["pid", "name"]):
        pid = p.info["pid"]
        d = stat.setdefault(pid, {"name": p.info.get("name") or "",
                                  "maxkids": 0, "depth": 0,
                                  "pub_total": 0, "dests": defaultdict(int),
                                  "listen": False, "age_h": 0.0,
                                  "rounds_seen": 0})
        try:
            rp = psutil.Process(pid)
            try:
                d["maxkids"] = max(d["maxkids"], len(rp.children()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            try:
                d["depth"] = max(d["depth"], depth(rp))
            except Exception:
                pass
            try:
                for c in rp.net_connections(kind="inet"):
                    try:
                        lport = c.laddr.port if c.laddr else None
                    except Exception:
                        lport = None
                    if c.status == "LISTEN":
                        d["listen"] = True
                        continue
                    try:
                        rip = c.raddr.ip if c.raddr else ""
                    except Exception:
                        rip = ""
                    if is_public(rip):
                        d["pub_total"] += 1
                        d["dests"][rip] += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            try:
                d["age_h"] = (now - rp.create_time()) / 3600.0
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            d["rounds_seen"] += 1
        except psutil.NoSuchProcess:
            continue
    print(f"round {r + 1}/{ROUNDS} 采样进程={len(stat)}", flush=True)
    if r < ROUNDS - 1:
        time.sleep(INTERVAL)

print("=" * 100, flush=True)
hits = []
for pid, d in stat.items():
    if d["rounds_seen"] < 2:
        continue  # 至少存活 2 轮, 瞬时进程不参评
    score, why = 0, []
    if d["maxkids"] >= 3:
        score += 20
        why.append(f"子进程{ d['maxkids']}个(+20)")
    ndest = len(d["dests"])
    if 0 < ndest <= 3 and d["pub_total"] >= 3:
        score += 20
        why.append(f"出站集中{ndest}目标/{d['pub_total']}次(+20)")
    rep = sum(1 for v in d["dests"].values() if v >= 3)
    if rep >= 1:
        score += 15
        why.append(f"跨轮复现{rep}目标(+15)")
    if d["listen"]:
        score += 10
        why.append("监听端口(+10)")
    if d["age_h"] > 2 and d["pub_total"] > 0:
        score += 10
        why.append(f"常驻{d['age_h']:.1f}h且对外(+10)")
    if d["depth"] >= 2:
        score += 10
        why.append(f"进程树{ d['depth']}层(+10)")
    if score >= 50:
        try:
            cmd = " ".join(psutil.Process(pid).cmdline())[:130]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            cmd = "<已退出>"
        hits.append((score, pid, d["name"], "; ".join(why), cmd))

hits.sort(key=lambda r: (-r[0], r[1]))
print(f"疑似agent={len(hits)} (阈值50, 参评进程={sum(1 for d in stat.values() if d['rounds_seen']>=2)})", flush=True)
for score, pid, name, why, cmd in hits:
    print(f"[{score}分] pid={pid:<7d} {name:<16s} {why}\n         {cmd}", flush=True)
