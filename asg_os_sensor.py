"""ASG OS级治理 PoC: asg-os-sensor daemon (用户态演示版).

思路 = EDR 平替: 不给每个 agent 写 adapter, 不改 agent 代码,
从 OS 层面枚举进程行为 (进程画像/打开文件/网络连接), 按 policies.yaml 判定.

生产级对应关系 (现场分享用):
  本PoC psutil 进程枚举      -> Linux eBPF / Windows ETW 进程事件
  本PoC open_files() 轮询    -> Linux fanotify / Windows USN Journal+minifilter
  本PoC net_connections 轮询 -> Linux netfilter/NFLOG / Windows WFP 过滤驱动
  本PoC kill demo 进程       -> seccomp-bpf 拦截 syscall / WFP 拒绝连接
  本PoC events.jsonl         -> ASG 网关 B(血缘)/D(证据链)/E(叙事) 三层本体

用法:
  python asg_os_sensor.py --interval 1            # 前台运行, Ctrl+C 停
  python asg_os_sensor.py --interval 1 --once     # 只扫一轮 (排障用)
"""
import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

try:
    import psutil
except ImportError:
    sys.exit("缺 psutil: pip install psutil")

try:
    import yaml
except ImportError:
    sys.exit("缺 pyyaml: pip install pyyaml")

try:
    import urllib.request as urlrequest
except Exception:  # pragma: no cover
    urlrequest = None


def load_policies():
    with open(BASE / "policies.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    # Supervisor 可把一次运行的事件隔离到独立证据目录；不改变默认策略。
    if os.environ.get("ASG_EVENTS_FILE"):
        cfg["events_file"] = os.environ["ASG_EVENTS_FILE"]
    if os.environ.get("ASG_AGENT_SCORE_THRESHOLD"):
        cfg["agent_score_threshold"] = int(os.environ["ASG_AGENT_SCORE_THRESHOLD"])
    return cfg


def now_iso():
    return datetime.now(timezone.utc).isoformat()


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)((?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|cookie)\s*[:=]\s*[\"']?)([^\s,;\"']+)"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(?:sk|rk|ghp|xoxb|xoxp)-[A-Za-z0-9._-]+\b")


def redact_text(value):
    text = str(value)
    text = _SECRET_TOKEN.sub("[REDACTED]", text)
    return _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", text)


class Sensor:
    def __init__(self, cfg):
        self.cfg = cfg
        self.self_pid = psutil.Process().pid
        self.events_path = BASE / cfg.get("events_file", "events.jsonl")
        self.sensitive_res = [re.compile(re.escape(p), re.I) for p in cfg.get("sensitive_patterns", [])]
        self.danger_res = [re.compile(re.escape(p), re.I) for p in cfg.get("dangerous_cmdline", [])]
        self.llm_hosts = [h.lower() for h in cfg.get("llm_endpoints", [])]
        self.allow = set(cfg.get("egress_allowlist", []))
        self.threshold = int(cfg.get("agent_score_threshold", 50))
        self.kill_marker = cfg.get("demo_kill_marker", "")
        self.excludes = [s.lower() for s in cfg.get("exclude_cmdline_substr", [])]
        self.asg_url = cfg.get("asg_ingest_url", "")
        self._asg_down_until = 0.0
        self.seen_agent_pids = set()
        self.fired = set()   # (pid,rule) 同一条件只告警一次, 避免常驻连接刷屏
        self.killed = set()  # pid 只 kill 一次
        self.n_scans = 0
        # 增量扫描状态: baseline 全量一次, 之后只看新 pid + 已画像 agent
        self.known_pids = set()
        self.agent_pids = {}  # pid -> score
        self.baselined = False

    # ---------- 事件落盘 (+可选转发ASG) ----------
    def emit(self, etype, proc, score, rule, disp, details):
        try:
            cmd = redact_text(" ".join(proc.info.get("cmdline") or []))
        except Exception:
            cmd = ""
        ev = {
            "ts": now_iso(),
            "event_type": etype,
            "pid": proc.info.get("pid"),
            "pname": proc.info.get("name"),
            "cmdline": cmd[:500],
            "agent_score": score,
            "rule": rule,
            "disposition": disp,
            "details": redact_text(details),
        }
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        print(f"[{disp:7s}] {etype:22s} pid={ev['pid']} {ev['pname']} score={score} rule={rule} :: {details}", flush=True)
        self.forward_asg(ev)

    def emit_once(self, proc, score, rule, etype, disp, details):
        """同一 (pid,rule) 只落盘/打印一次. 返回 True 表示条件成立."""
        pid = proc.info.get("pid")
        if (pid, rule) not in self.fired:
            self.fired.add((pid, rule))
            self.emit(etype, proc, score, rule, disp, details)
        return True

    def forward_asg(self, ev):
        # 网关不在是常态(演示机8090未必在跑): 0.4s超时 + 60s熔断,
        # 否则每事件1.5s会把扫描周期拖到30s, 瞬时进程全漏掉. 教训已记.
        if not self.asg_url or urlrequest is None:
            return
        if time.time() < self._asg_down_until:
            return
        try:
            req = urlrequest.Request(
                self.asg_url,
                data=json.dumps({"title": "asg-os-sensor", "event": ev}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlrequest.urlopen(req, timeout=0.4) as r:
                r.read(1)
        except Exception:
            self._asg_down_until = time.time() + 60.0

    # ---------- L1: 行为画像（禁止产品名/进程名先验） ----------
    def agent_score(self, proc):
        """只用可观测行为判断是否像主动编排运行时。

        这里故意不检查任何产品名、厂商名或框架名。命令行中的通用协议/输出
        参数可以作为结构信号，但不会把某个名字映射成 agent。这样一个此前
        从未见过的 harness 只能凭进程行为进入后续调查。
        """
        info = proc.info
        cmdline = info.get("cmdline") or []
        cmd = " ".join(cmdline).lower()
        for ex in self.excludes:
            if ex in cmd:
                return -1, [f"排除列表命中{ex}"]
        score, reasons = 0, []

        # 运行时编排的通用结构信号：多参数、结构化输出/协议、子进程、外联。
        # 每项来自当前进程状态，不来自先验名称表。
        if len(cmdline) >= 3:
            score += 10
            reasons.append("参数化启动(+10)")
        if any(x in cmd for x in ("--model", "--output-format", "--system-prompt",
                                  "--verbose", "--permission-mode", "--print",
                                  "--json", "--jsonl", "mcp", "prompt")):
            score += 20
            reasons.append("结构化编排参数(+20)")
        if any(x in cmd for x in ("--output-format", "--stream", "--json", "--jsonl")):
            score += 20
            reasons.append("结构化流协议(+20)")
        try:
            children = proc.children(recursive=True)
            if children:
                score += 20
                reasons.append(f"子进程树={len(children)}(+20)")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        try:
            conns = proc.net_connections(kind="inet")
            if conns:
                score += 20
                reasons.append(f"网络连接={len(conns)}(+20)")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        for h in self.llm_hosts:
            if h in cmd:
                score += 15
                reasons.append(f"声明外部模型端点(+15)")
                break
        return min(score, 100), reasons

    # ---------- L2: 文件 ----------
    def check_files(self, proc, score):
        try:
            cmd = " ".join(proc.info.get("cmdline") or [])
            targets = []
            try:
                for f in proc.open_files():
                    targets.append(f.path)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            if cmd:
                targets.append("CMD::" + cmd)
            for t in targets:
                for rx in self.sensitive_res:
                    if rx.search(t):
                        return self.emit_once(proc, score, "R2", "file.sensitive_access",
                                              "BLOCK", f"敏感路径命中 {rx.pattern!r} :: {t[:200]}")
                        # kill 由 scan 统一执行
        except psutil.NoSuchProcess:
            pass
        return False

    def is_internal(self, ip):
        if not ip or ip in self.allow or ip.startswith("127.") or ip == "::1":
            return True
        if ip.startswith("10.") or ip.startswith("192.168."):
            return True
        if ip.startswith("172."):
            try:
                second = int(ip.split(".")[1])
                if 16 <= second <= 31:
                    return True
            except (ValueError, IndexError):
                pass
        return False

    # ---------- L3: 网络 ----------
    def check_net(self, proc, score):
        try:
            conns = proc.net_connections(kind="inet")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        hit = False
        for c in conns:
            # ESTABLISHED/SYN_SENT = 正在进行; CLOSE_WAIT = 对端已关、本地未关,
            # 对"请求-响应-即关"的 API 轮询形态, 快照时刻大多只剩 CLOSE_WAIT.
            # PoC-1 的常驻 raw socket 全程 ESTABLISHED 所以能抓到, 真实 HTTPS 客户端抓不到
            # —— e2e 实测全场 R3 为 0 逼出此 bug. CLOSE_WAIT 计入, TIME_WAIT(pid 归零)除外.
            if c.status not in ("ESTABLISHED", "SYN_SENT", "NONE", "CLOSE_WAIT"):
                continue
            try:
                raddr = c.raddr.ip if c.raddr else ""
            except Exception:
                raddr = ""
            if self.is_internal(raddr):
                continue
            self.emit_once(proc, score, "R3", "net.unknown_egress",
                           "ALERT", f"出站 {raddr}:{c.raddr.port if c.raddr else '?'} status={c.status}")
            hit = True
        return hit

    # ---------- 命令行危险模式 ----------
    def check_cmdline(self, proc, score):
        cmd = " ".join(proc.info.get("cmdline") or [])
        for rx in self.danger_res:
            if rx.search(cmd):
                return self.emit_once(proc, score, "R4", "proc.dangerous_cmdline",
                                      "BLOCK", f"危险模式 {rx.pattern!r} :: {cmd[:200]}")
                # kill 由 scan 统一执行
        return False

    def enforce_block(self, proc, why):
        pid = proc.info.get("pid")
        if pid in self.killed:
            return
        self.killed.add(pid)
        cmd = " ".join(proc.info.get("cmdline") or [])
        if self.kill_marker and self.kill_marker not in cmd:
            print(f"  [enforce] 非demo进程, 仅告警不kill ({why})", flush=True)
            return
        try:
            proc.terminate()
            print(f"  [enforce] 已 terminate pid={proc.info.get('pid')} ({why})", flush=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            print(f"  [enforce] kill失败: {e}", flush=True)

    def classify(self, proc):
        """画像+深查单个进程. 返回 score(<阈值返回0). 新 agent 发 R1. BLOCK 统一 kill."""
        try:
            if proc.info.get("pid") == self.self_pid:
                return 0
            score, reasons = self.agent_score(proc)
            if score < self.threshold:
                return 0
            pid = proc.info.get("pid")
            if pid not in self.seen_agent_pids:
                self.seen_agent_pids.add(pid)
                self.emit("proc.agent_identified", proc, score, "R1",
                          "ALERT", "画像命中: " + "; ".join(reasons))
            self.agent_pids[pid] = score
            blocked = False
            if self.check_files(proc, score):
                blocked = True
            if self.check_net(proc, score):
                blocked = True
            if self.check_cmdline(proc, score):
                blocked = True
            if blocked:
                self.enforce_block(proc, "multi-rule")
            return score
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return 0

    def wrap_pid(self, pid):
        """把单个 pid 包装成 classify 可用的 proc 对象 (新进程/存量复查共用)."""
        real = psutil.Process(pid)
        info = {"pid": pid, "name": real.name(), "cmdline": real.cmdline()}

        class W:
            def __init__(self, real, info):
                self._r = real
                self.info = info
            def children(self, recursive=False):
                return self._r.children(recursive=recursive)
            def open_files(self):
                return self._r.open_files()
            def net_connections(self, kind="inet"):
                return self._r.net_connections(kind=kind)
            def terminate(self):
                return self._r.terminate()
            def parent(self):
                return self._r.parent()
        return W(real, info)

    def scan_once(self):
        self.n_scans += 1
        t0 = time.time()
        if not self.baselined:
            # 首轮 baseline: 全量枚举 (慢, 约30s+, 启动时预热一次即可)
            for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                try:
                    self.known_pids.add(proc.info.get("pid"))
                    self.classify(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            self.baselined = True
        else:
            # 增量轮: 只看新 pid + 存量 agent 复查 (快, 秒级)
            try:
                cur = set(psutil.pids())
            except Exception:
                cur = set(self.known_pids)
            for pid in list(self.agent_pids):
                if pid not in cur:
                    self.agent_pids.pop(pid, None)
            new = cur - self.known_pids
            self.known_pids = cur
            for pid in new:
                try:
                    self.classify(self.wrap_pid(pid))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            # 存量 agent 复查 L2/L3/L4 (行为随时间变, 画像一次不够)
            for pid in list(self.agent_pids):
                try:
                    if self.classify(self.wrap_pid(pid)) < self.threshold:
                        self.agent_pids.pop(pid, None)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    self.agent_pids.pop(pid, None)
                    continue
        # 清掉已退出 pid 的 R1 记忆需要 pid 复用防护, PoC 略 (文档记为已知局限)
        n_agent = len(self.agent_pids)
        dt = time.time() - t0
        print(f"-- scan#{self.n_scans} 疑似agent={n_agent} 耗时={dt:.1f}s", flush=True)
        return n_agent

    def run(self, interval):
        print(f"asg-os-sensor 启动 pid={self.self_pid} interval={interval}s events={self.events_path}", flush=True)
        print(f"阈值={self.threshold} kill锁={self.kill_marker!r} asg转发={self.asg_url}", flush=True)
        try:
            while True:
                self.scan_once()
                time.sleep(interval)
        except KeyboardInterrupt:
            print(f"停止, 共{self.n_scans}轮. 事件见 {self.events_path}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    s = Sensor(load_policies())
    if a.once:
        n = s.scan_once()
        print(f"单轮扫描 疑似agent={n}")
    else:
        s.run(a.interval)


if __name__ == "__main__":
    main()
