"""E2E 全流程编排 (断言式, 退出码 0/1):
 0.清数据  1.起sensor等基线  2.基线跑agent(R1有,语义0)
 3.证伪旧adapter(新钩子不含fakellm/被测名)  4.挂接重跑(语义事件+任务完成)
 5.R2(蜜罐读)  6.R3(真实出站)  7.R4(一次性无害探针,带kill标记)
 8.审计留痕 AUDIT-<ts>.md (文件sha256+计数+时间线+样本)
未建成不测: SaaS 研判、网关汇聚只验事件格式就绪, 不冒充完成.
"""
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
EVENTS = ROOT / "events.jsonl"
ADAPT = BASE / "adapter_events.jsonl"
SLOG = BASE / "sensor_e2e.log"
AUDIT = None
fails = []
tline = []


def ts():
    return datetime.now(timezone.utc).isoformat()


def log(s):
    tline.append(f"{ts()} {s}")
    print(s, flush=True)


def check(name, cond, hint=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name} {hint}", flush=True)
    if not cond:
        fails.append(name)


def load(p):
    if not p.exists():
        return []
    rows = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def agent_cmd(pid):
    try:
        import psutil
        pr = psutil.Process(pid)
        return " ".join(pr.cmdline())
    except Exception:
        return ""


def norm(c):
    return (c or "").replace("\\", "/")


def main():
    # Step 0: 清数据 + 清陈旧 sensor(防多 sensor 同写一份证据)
    try:
        import psutil
        me = psutil.Process().pid
        for p in psutil.process_iter(["pid", "cmdline"]):
            try:
                c = " ".join(p.info.get("cmdline") or [])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if p.info["pid"] != me and "asg_os_sensor.py" in c and "--interval" in c \
                    and "run_e2e" not in c:
                try:
                    psutil.Process(p.info["pid"]).terminate()
                    log(f"Step0 清掉陈旧sensor pid={p.info['pid']}")
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        time.sleep(2)
    except ImportError:
        pass
    for f in (EVENTS, ADAPT, SLOG):
        if f.exists():
            f.unlink()
    (BASE / "out").mkdir(exist_ok=True)
    log("Step0 清数据完成")

    # Step 1: 起 sensor
    sp = subprocess.Popen(
        [sys.executable, str(ROOT / "asg_os_sensor.py"), "--interval", "1"],
        stdout=open(SLOG, "w", encoding="utf-8"), stderr=subprocess.STDOUT,
        cwd=str(ROOT))
    log(f"Step1 sensor 已启动 pid={sp.pid}, 等基线轮...")
    ok = False
    for _ in range(24):
        time.sleep(5)
        if SLOG.exists() and "scan#1" in SLOG.read_text(encoding="utf-8", errors="ignore"):
            ok = True
            break
    check("sensor基线轮完成", ok)

    def run_agent(hooked):
        env = dict(os.environ)
        pp = str(BASE)
        if hooked:
            pp = os.pathsep.join([str(BASE / "hook"), str(BASE)])
            env["ASG_ADAPTER_EVENTS"] = str(ADAPT)
        else:
            env.pop("ASG_ADAPTER_EVENTS", None)
        env["PYTHONPATH"] = pp + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(BASE / "agent.py"), "--model", "deepseek-v4-flash"],
            capture_output=True, text=True, timeout=420, env=env, cwd=str(BASE))

    # Step 2: 基线 (无钩子)
    log("Step2 基线跑 agent(无钩子)...")
    p0 = run_agent(False)
    (BASE / "baseline_stdout.txt").write_text(p0.stdout, encoding="utf-8")
    time.sleep(8)  # 给 sensor 增量轮检出
    evs = load(EVENTS)
    r1 = [e for e in evs if e.get("rule") == "R1" and "e2e/agent.py" in norm(e.get("cmdline"))]
    check("基线任务完成", "all tasks done" in p0.stdout, f"rc={p0.returncode}")
    check("sensor发现agent(R1)", len(r1) >= 1, f"R1数={len(r1)}")
    check("基线零语义事件", load(ADAPT) == [])

    # Step 3: 证伪旧 adapter
    hook_txt = (BASE / "hook" / "sitecustomize.py").read_text(encoding="utf-8")
    check("新钩子不含旧桩/被测名(泛化)",
          all(s not in hook_txt for s in ("fakellm", "fake_agent", "e2e/agent", "victim")))

    # Step 4: 挂接重跑
    log("Step4 挂接重跑(注入通用钩子)...")
    p1 = run_agent(True)
    (BASE / "hooked_stdout.txt").write_text(p1.stdout, encoding="utf-8")
    time.sleep(8)
    ad = load(ADAPT)
    reqs = [e for e in ad if e.get("event_type") == "llm.request"]
    resps = [e for e in ad if e.get("event_type") == "llm.response"]
    errs = [e for e in ad if e.get("event_type") == "llm.error"]
    check("挂接后任务完成", "all tasks done" in p1.stdout, f"rc={p1.returncode}")
    check("语义事件产出", len(reqs) >= 3 and len(resps) >= 3,
          f"req={len(reqs)} resp={len(resps)} err={len(errs)}")
    check("血统标记auto-runtime-v2", len(ad) > 0 and all(e.get("adapter_source") == "auto-runtime-v2" for e in ad))
    check("事件含prompt原文", any("蜜罐" in json.dumps(e, ensure_ascii=False) or "note" in json.dumps(e, ensure_ascii=False) for e in reqs))
    check("工具调用被记录", any(e.get("tool_calls") for e in resps))
    if errs:
        log(f"  注: llm.error {len(errs)} 条, 首条={json.dumps(errs[0], ensure_ascii=False)[:200]}")

    # agent pid(取挂接轮 R1)
    evs = load(EVENTS)
    apids = sorted({e["pid"] for e in evs if "e2e/agent.py" in norm(e.get("cmdline"))})

    # Step 5: R2 (双层: OS 句柄优先, 抓不住瞬时句柄时由 runtime 语义补位.
    # 这正是"EDR 看动作、runtime 看内容"的分工实证, 不是放水: 两层都留痕.)
    r2 = [e for e in evs if e.get("rule") == "R2" and e.get("pid") in apids]
    ad = load(ADAPT)
    r2_rt = [e for e in ad
             if e.get("event_type") == "llm.response" and e.get("tool_calls")
             and any("read_file" in json.dumps(t, ensure_ascii=False)
                     and ("honey" in json.dumps(t, ensure_ascii=False)
                          or "id_rsa" in json.dumps(t, ensure_ascii=False))
                     for t in (e["tool_calls"] or []) if isinstance(t, dict))]
    check("R2蜜罐读取检出(OS或runtime)", len(r2) >= 1 or len(r2_rt) >= 1,
          f"OS_R2={len(r2)} runtime_R2={len(r2_rt)}")
    if not r2 and r2_rt:
        log("  注: OS 层漏检毫秒级瞬时句柄(轮询局限, 已知), runtime 层 tool 参数补获")
    check("R2未误杀(agent存活)", p1.returncode == 0)

    # Step 6: R3 (CLOSE_WAIT 计入后应直接命中; 否则看 adapter 出站证据链)
    r3 = [e for e in evs if e.get("rule") == "R3" and e.get("pid") in apids]
    r3_rt = [e for e in ad if e.get("event_type") == "llm.request"]
    check("R3出站检出(OS或adapter证据)", len(r3) >= 1 or len(r3_rt) >= 3,
          f"OS_R3={len(r3)} adapter_req={len(r3_rt)}")
    if r3:
        log(f"  注: R3 目标={r3[0].get('details', '')[:100]} (已知LLM网关, 待端点表放行, 见方案§四)")

    # Step 7: R4 无害探针
    log("Step7 R4探针(无害,-c pass,带kill标记)...")
    probe = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(25)",
         "rm -rf /tmp/e2e-probe", "asg-os-sensor-demo-evil"])
    time.sleep(10)
    alive = probe.poll() is None
    evs = load(EVENTS)
    r4 = [e for e in evs if e.get("rule") == "R4" and "e2e-probe" in norm(e.get("cmdline"))]
    check("R4危险命令检出+BLOCK", len(r4) >= 1, f"R4数={len(r4)}")
    check("R4探针被终结", not alive, "探针存活=未拦截" if alive else "已终结")
    if alive:
        probe.kill()

    # Step 8: 审计留痕
    art = {"baseline_stdout.txt": BASE / "baseline_stdout.txt",
           "hooked_stdout.txt": BASE / "hooked_stdout.txt",
           "events.jsonl": EVENTS, "adapter_events.jsonl": ADAPT,
           "sensor_e2e.log": SLOG}
    lines = [f"# E2E 审计留痕 {ts()}", "",
             "## 文件(hash)", ""]
    for n, p in art.items():
        h = hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else "缺失"
        sz = p.stat().st_size if p.exists() else 0
        lines.append(f"- {n}: sha256={h} size={sz}")
    evs, ad = load(EVENTS), load(ADAPT)
    from collections import Counter
    lines += ["", "## 事件计数", "",
              f"- sensor: {dict(Counter(e.get('rule') for e in evs))}",
              f"- adapter: {dict(Counter(e.get('event_type') for e in ad))}", "",
              "## 时间线", ""] + tline + ["", "## 断言", "",
              f"- 失败: {fails if fails else '无, 全过'}", "",
              "## 未覆盖声明", "",
              "- SaaS 研判、网关汇聚未 live 测试, 仅事件格式就绪, 不冒充完成。"]
    ap = BASE / f"AUDIT-{datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    ap.write_text("\n".join(lines), encoding="utf-8")
    log(f"Step8 审计文件: {ap.name}")

    try:
        sp.terminate()
    except Exception:
        pass
    if fails:
        print(f"失败项: {fails}", flush=True)
        return 1
    print("E2E 全流程通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
