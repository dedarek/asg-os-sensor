"""行为画像 v2: 分类先行, 证据定级. 设计依据见文件头.

设计出处 (按用户要求先查再定, 拒绝拍脑袋):
 [Sigma 3000+规则] 原子高精度组合(Image+命令行+父进程), 每条规则自带
   level 与 falsepositives, 规则按 experimental->stable 毕业. 学到:
   少做通用加分, 多做高精度组合; 每条信号标注可信度与预期误报.
 [Elastic 2300+规则] EQL 多事件序列(跨时间行为链) + LLM 给 noisy 规则做 triage.
   学到: 宽网规则只负责捞, 判定交给研判层; 序列 > 单点.
 [Sysmon 社区配置] 父子异常组合是最高保真信号(Office拉powershell, 浏览器拉cmd).
   学到: 看"谁拉起了谁", 不看"谁叫什么".
 [osquery/Palantir] 舰队离群分析、异常父进程(nginx拉bash). 学到: 稀有组合优先.
 [Beaconing 文献] 周期性+包大小一致性+jitter 感知统计. 学到: API 轮询就是 beaconing,
   用跨轮复现 + 分散度 (coefficient of variation 思想的简化版) 度量, 不用固定阈值.
 [动态分析论文群] API 名+参数序列 > 单个事件; 进程创建/终止序列本身即特征.
   学到: 采集"新子进程出现"这个事件流, 而不是子供数快照.
 [Red Canary/Picus 2025] 真实流行度: T1059 脚本执行、T1071 应用层C2、T1219 远控工具、
   T1036 伪装、T1105 工具传输. 学到: 权重按真实流行度排, T1059 类编排信号最重;
   且远控工具(T1219)在工业界是独立检测赛道, 不跟通用评分混在一起.

v2 核心改动 (相对 v1):
 1. 分类先行: 先定 lane, 再打分. ACTIVE_AGENT / DORMANT / TUNNEL_LIKE / BENIGN.
    cpolar/GameViewer 进 TUNNEL_LIKE(审计赛道), 不再跟 Hermes 挤一条线.
 2. 编排信号升级为"事件流": 子进程快照数 -> 跨轮"新子进程出现次数"(spawn churn).
    静态 Electron 树(常年不变) vs 干活中的 agent(不断拉新进程), 一刀切开.
    这是 Elastic EQL 序列思想的极简实现.
 3. 空闲 agent 有位置: 编排结构在但当前安静 -> DORMANT(可见、可查、不告警),
    而不是"没进来". 上版"漏掉 OpenCode"的真正错误是二值输出, 不是分数.
 4. 每条信号标注 level(Sigma 式), 处置按 lane 走, 不再只有一个 50 分线.
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
        return False
    return True


def depth(proc):
    best, seen, stack = 0, set(), [(proc, 0)]
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


stat = {}
for r in range(ROUNDS):
    for p in psutil.process_iter(["pid", "name"]):
        pid = p.info["pid"]
        d = stat.setdefault(pid, {"name": p.info.get("name") or "",
                                  "kidsets": [], "maxdepth": 0,
                                  "pub_total": 0, "dests": defaultdict(int),
                                  "listen": False, "age_h": 0.0,
                                  "wbytes": [], "rounds": 0, "exe": ""})
        try:
            rp = psutil.Process(pid)
            if not d["exe"]:
                try:
                    d["exe"] = (rp.exe() or "").lower()
                except Exception:
                    d["exe"] = "?"
            try:
                d["kidsets"].append(frozenset(k.pid for k in rp.children()))
            except Exception:
                pass
            try:
                d["maxdepth"] = max(d["maxdepth"], depth(rp))
            except Exception:
                pass
            try:
                for c in rp.net_connections(kind="inet"):
                    try:
                        if c.status == "LISTEN":
                            d["listen"] = True
                            continue
                        rip = c.raddr.ip if c.raddr else ""
                    except Exception:
                        continue
                    if is_public(rip):
                        d["pub_total"] += 1
                        d["dests"][(rip, r)] += 1
            except Exception:
                pass
            try:
                io = rp.io_counters()
                d["wbytes"].append(io.write_bytes)
            except Exception:
                pass
            try:
                d["age_h"] = (time.time() - rp.create_time()) / 3600.0
            except Exception:
                pass
            d["rounds"] += 1
        except psutil.NoSuchProcess:
            continue
    print(f"round {r + 1}/{ROUNDS} 采样={len(stat)}", flush=True)
    if r < ROUNDS - 1:
        time.sleep(INTERVAL)

print("=" * 104, flush=True)
lanes = defaultdict(list)
nsys = 0
for pid, d in stat.items():
    if d["rounds"] < 2:
        continue
    # 系统基线 lane: 内核与系统服务二进制默认信任 (Sigma 已知正常噪声思想).
    # 代理风险(系统目录投放)由哈希基线另行覆盖, 不在本画像内解决.
    if pid in (0, 4) or (d["exe"] and d["exe"].startswith("c:\\windows\\")):
        nsys += 1
        continue
    # --- 信号 (level 标注, Sigma 式) ---
    sig = {}
    # S1 编排规模快照 (medium)
    nkids = max((len(k) for k in d["kidsets"]), default=0)
    sig["S1_kids"] = (25 if nkids >= 3 else (10 if nkids >= 1 else 0), f"子进程{nkids}")
    # S2 spawn churn: 跨轮新子进程出现轮次数 (high, T1059 编排指纹)
    churn = 0
    seen_kids = set()
    for ks in d["kidsets"]:
        new = ks - seen_kids
        if new:
            churn += 1
        seen_kids |= ks
    sig["S2_churn"] = (20 if churn >= 2 else (10 if churn == 1 else 0), f"拉新{churn}轮")
    # S3 树深 (low)
    sig["S3_depth"] = (10 if d["maxdepth"] >= 2 else 0, f"树深{d['maxdepth']}")
    # S4 出站集中 (medium, T1071 API 形态)
    nd = len({k[0] for k in d["dests"]})
    sig["S4_conc"] = (20 if 0 < nd <= 3 and d["pub_total"] >= 3 else 0,
                      f"{nd}目标/{d['pub_total']}次")
    # S5 节律 (medium, beaconing 跨轮复现)
    byip = defaultdict(int)
    for (ip, _r) in d["dests"]:
        byip[ip] += 1
    rep = sum(1 for v in byip.values() if v >= 3)
    sig["S5_rhythm"] = (10 if rep >= 1 else 0, f"复现{rep}目标")
    # S6 常驻且有写 (low, 存活干活)
    wdelta = (max(d["wbytes"]) - min(d["wbytes"])) if len(d["wbytes"]) >= 2 else 0
    sig["S6_work"] = (10 if d["age_h"] > 2 and wdelta > 0 else 0,
                      f"存活{d['age_h']:.1f}h写+{wdelta // 1024}KB")
    # S7 服务形态 (informational)
    sig["S7_listen"] = (5 if d["listen"] else 0, "监听" if d["listen"] else "无监听")

    score = sum(v for v, _ in sig.values())
    orch = sig["S1_kids"][0] + sig["S2_churn"][0] + sig["S3_depth"][0]  # 编排分量
    net = sig["S4_conc"][0] + sig["S5_rhythm"][0]                      # 外联分量
    # --- lane (分类先行; 门槛经 v2 实测校准) ---
    # ACTIVE 要求编排分量>=30: 零散 kids1+churn1(=20) 不得入内,
    # 隧道/远控类(无持续编排)自然落到 TUNNEL 赛道.
    if score >= 50 and orch >= 30:
        lane = "ACTIVE_AGENT"     # 编排干活中 (+外联与否皆可)
    elif orch >= 30:
        lane = "DORMANT"          # 结构像 agent, 当前安静: 可见可查, 不告警
    elif score >= 45:
        lane = "TUNNEL_LIKE"      # 常驻外联+监听, 无编排: 远控/隧道审计赛道
    else:
        lane = "BENIGN"
    if lane == "BENIGN":
        continue
    try:
        cmd = " ".join(psutil.Process(pid).cmdline())[:120]
    except Exception:
        cmd = "<已退出>"
    detail = " ".join(f"{k}{v[1]}" for k, v in sig.items() if v[0] > 0)
    lanes[lane].append((score, pid, d["name"], detail, cmd))

for lane in ("ACTIVE_AGENT", "DORMANT", "TUNNEL_LIKE"):
    rows = sorted(lanes[lane], key=lambda r: (-r[0], r[1]))
    print(f"--- {lane} x{len(rows)} ---", flush=True)
    for score, pid, name, detail, cmd in rows:
        print(f"[{score}分] pid={pid:<7d} {name:<16s} {detail}\n         {cmd}", flush=True)
