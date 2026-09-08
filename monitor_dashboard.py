"""ASG Agent Live Monitor & Autonomous Runtime Governance Engine
功能：
1. 30s 周期实时扫描操作系统进程，通过零先验行为特征画像打分（识别 Agent）
2. 特征指纹比对：已沉淀的 Agent 实现毫秒级命中路由与 Adapter 挂接
3. 陌生 Agent 自动逆向接管：自动异步调度 Goose (DeepSeek) 执行 Agent Work 受控调查
4. 自主推导 Agent 真实业务名称、通信协议与观测 Recipe，并自动沉淀至指纹库
5. 实时拦截/挂接语义事件流，展示最新脱敏消息
"""
from __future__ import annotations

import os
import sys
import json
import time
import shutil
import threading
import subprocess
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import psutil

# 确保根目录在 sys.path
ROOT = Path("D:/proj/asg-os-sensor")
sys.path.insert(0, str(ROOT))

from asg_os_sensor import Sensor, load_policies
from runtime import analyzer, matcher
from runtime.stream_parser import redact

GOOSE = Path.home() / ".local" / "bin" / "goose.exe"
if not GOOSE.exists():
    GOOSE = Path(shutil.which("goose") or shutil.which("goose.exe") or "goose")
RECIPE = ROOT / "recipes" / "runtime_analyst.yaml"

# 共享状态与锁
STATE_LOCK = threading.Lock()
SCAN_STATE = {
    "last_scan_time": None,
    "scan_interval": 30,
    "scan_count": 0,
    "agents": [],
    "fingerprints_count": 0,
    "active_investigations": {}  # pid -> {status, started_at, turns}
}

# 记录当前已挂起正在调查的 PID，避免重复拉起多个 Goose
INVESTIGATING_PIDS = set()
INVESTIGATION_LOCK = threading.Lock()
INVESTIGATION_SEMAPHORE = threading.Semaphore(2)  # 最多同时允许 2 个 Goose 并发，防止跑满 API 与进程雪崩


def now() -> float:
    return time.time()


def iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts or now()))


def analyst_route() -> dict[str, str]:
    route = os.environ.get("ASG_ANALYST_ROUTE", "opencode-go").strip().lower()
    routes = {
        "commandcode": {
            "provider": "openai",
            "model": "deepseek/deepseek-v4-flash",
            "base_url": "https://api.commandcode.ai/provider/v1",
            "key_env": "COMMANDCODE_API_KEY",
        },
        "opencode-go": {
            "provider": "openai",
            "model": "deepseek-v4-flash",
            "base_url": "https://opencode.ai/zen/go/v1",
            "key_env": "OPENCODE_GO_API_KEY",
        },
    }
    if route not in routes:
        route = "opencode-go"
    return {"route": route, **routes[route]}


def run_autonomous_investigation(pid: int, struct: dict[str, Any]):
    """由 Goose (DeepSeek) 执行后台非交互式受控逆向接管"""
    with INVESTIGATION_LOCK:
        if pid in INVESTIGATING_PIDS:
            return
        INVESTIGATING_PIDS.add(pid)

    with INVESTIGATION_SEMAPHORE:
        run_dir = ROOT / "e2e" / "artifacts" / "autonomous-governance" / f"pid_{pid}_{int(time.time())}"
    run_dir.mkdir(parents=True, exist_ok=True)
    recipes_dir = run_dir / "recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)

    stream_file = run_dir / "target_stream.jsonl"
    stream_file.touch()

    with STATE_LOCK:
        SCAN_STATE["active_investigations"][pid] = {
            "status": "investigating",
            "started_at": time.strftime("%H:%M:%S"),
            "log_dir": str(run_dir)
        }

    try:
        route = analyst_route()
        key = os.environ.get(route["key_env"], "")
        if not key:
            for env_path in [Path.home() / "AppData/Local/hermes/.env", Path.home() / ".env"]:
                if env_path.exists():
                    for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                        if line.startswith(f"{route['key_env']}="):
                            key = line.split("=", 1)[1].strip()
                            break
        
        if not key:
            print(f"[Analyst] 缺少凭据 {route['key_env']}，跳过接管 PID {pid}", file=sys.stderr)
            return

        extension = f"asg-runtime-tools:ASG_TARGET_PID={pid} ASG_AUDIT_DIR={run_dir} ASG_RECIPE_DIR={recipes_dir} ASG_TARGET_STREAM_FILE={stream_file} python runtime/analyst_tools.py"
        cmd = [
            str(GOOSE), "run", "--no-profile", "--no-session",
            "--recipe", str(RECIPE),
            "--params", f"target_pid={pid}",
            "--provider", route["provider"],
            "--model", route["model"],
            "--max-turns", "6",
            "--max-tool-repetitions", "2",
            "--output-format", "stream-json",
            "--with-extension", extension
        ]

        env = os.environ.copy()
        env["GOOSE_PROVIDER"] = route["provider"]
        env["GOOSE_MODEL"] = route["model"]
        env["GOOSE_MODE"] = "auto"
        env["OPENAI_BASE_URL"] = route["base_url"]
        env["OPENAI_API_KEY"] = key
        if route["route"] == "opencode-go":
            env["OPENAI_CUSTOM_HEADERS"] = f"x-opencode-session=asg-live-{pid},x-opencode-client=asg-live-analyst"

        out_path = run_dir / "analyst_stdout.jsonl"
        err_path = run_dir / "analyst_stderr.log"

        print(f"[Analyst] Goose 开始自主逆向接管 PID={pid}...")
        t0 = time.time()
        cp = subprocess.run(cmd, cwd=ROOT, env=env, stdout=out_path.open("w", encoding="utf-8"), stderr=err_path.open("w", encoding="utf-8"), text=True, timeout=180)
        elapsed_ms = int((time.time() - t0) * 1000)

        # 检查是否成功产出 candidate.json
        candidate_file = recipes_dir / "candidate.json"
        if candidate_file.exists():
            payload = json.loads(candidate_file.read_text(encoding="utf-8"))
            recipe = payload.get("recipe", {})
            identity = recipe.get("agent_identity_name", "")
            # 严格质量门禁：只有逆向成功观测到有效特征且非"unidentified/unknown"时，才允许写入指纹库
            if (
                isinstance(recipe, dict)
                and "match_features" in recipe
                and identity not in ["", "unknown", "unknown-runtime", "unidentified-agent"]
                and recipe.get("confidence", 0) >= 0.3
            ):
                # 写入指纹库
                entry = matcher.remember(struct, recipe, elapsed_ms)
                print(f"[Analyst] 接管成功并写入指纹库! Agent={entry.get('name')}, HarnessID={entry.get('id')}")
            else:
                print(f"[Analyst] 逆向目标在调查期间已退出或不可达 (identity={identity}, confidence={recipe.get('confidence')})，放弃生成无效指纹。")
        else:
            print(f"[Analyst] 接管完成但未产生有效 Recipe (returncode={cp.returncode})", file=sys.stderr)

    except Exception as exc:
        print(f"[Analyst Error PID={pid}] {exc}", file=sys.stderr)
    finally:
        with INVESTIGATION_LOCK:
            INVESTIGATING_PIDS.discard(pid)
        with STATE_LOCK:
            SCAN_STATE["active_investigations"].pop(pid, None)
        # 立即更新指纹库统计与扫描结果
        try:
            fp_path = ROOT / "runtime" / "fingerprints.json"
            if fp_path.exists():
                fp_data = json.loads(fp_path.read_text(encoding="utf-8"))
                with STATE_LOCK:
                    SCAN_STATE["fingerprints_count"] = len(fp_data.get("fingerprints", []))
        except Exception:
            pass
        scan_agents_once()


def get_last_semantic_message(pid: int, exe_name: str, cmdline: str) -> dict:
    """获取该 Agent 进程真实关联的语义消息，绝不把其他进程或历史残留瞎挂上去"""
    # 查找专属绑定到该 PID 的会话流
    pid_stream = ROOT / "e2e" / "artifacts" / f"stream_{pid}.jsonl"
    if pid_stream.exists():
        try:
            lines = [l.strip() for l in pid_stream.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
            if lines:
                last_obj = json.loads(lines[-1])
                return {
                    "source": f"pid_{pid}",
                    "event_type": last_obj.get("event_type", "unknown"),
                    "ts": last_obj.get("ts", ""),
                    "detail": last_obj.get("detail") or last_obj.get("blocks") or last_obj.get("prompt") or last_obj.get("call") or "N/A"
                }
        except Exception:
            pass

    return {
        "source": "none",
        "event_type": "未监听",
        "ts": time.strftime("%H:%M:%S"),
        "detail": "当前进程尚未挂接流式 Sink 或暂无新消息"
    }


def scan_agents_once():
    """执行一次完整的 30s OS 级扫描"""
    global SCAN_STATE
    policies = load_policies()
    sensor = Sensor(policies)
    
    found_agents = []
    
    # 遍历进程并收集初筛候选
    candidates = []
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            pinfo = proc.info
            pid = pinfo.get("pid")
            name = pinfo.get("name") or ""
            cmdline = pinfo.get("cmdline") or []

            if not cmdline or pid == os.getpid() or name.lower() in ["system", "registry", "smss.exe"]:
                continue

            w = sensor.wrap_pid(pid)
            score, reasons = sensor.agent_score(w)
            if score >= 50:
                candidates.append((proc, pinfo, score, reasons))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    # 进程树去重 (Root Deduplication):
    # 若候选集合中存在 A 包含子进程 B (A 是 B 的父级且两者均在候选集合中)，
    # 优先由根节点/主编排进程 A 代表 Agent 实体进行纳管与逆向，避免一个 Agent 派生的子进程反复在看板盖楼
    candidate_pids = {pinfo["pid"] for _, pinfo, _, _ in candidates}
    sub_worker_pids = set()
    for proc, pinfo, _, _ in candidates:
        try:
            for child in proc.children(recursive=True):
                if child.pid in candidate_pids:
                    sub_worker_pids.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    for proc, pinfo, score, reasons in candidates:
        pid = pinfo["pid"]
        # 如果当前候选只是其他已纳管 Agent 的派生子进程，将其归为子 Worker 忽略，聚焦根编排进程
        if pid in sub_worker_pids:
            continue

        try:
            name = pinfo.get("name") or ""
            cmdline = pinfo.get("cmdline") or []
            struct = {}
            try:
                struct = analyzer.analyze(pid)
            except Exception as e:
                print(f"[Analyze Error PID={pid}] {e}", file=sys.stderr)
                struct = {
                    "pid": pid,
                    "exe": name,
                    "runtime": "native",
                    "argv_shape": [(x if str(x).startswith("-") else "<value>") for x in cmdline],
                    "config_dirs": []
                }
            
            matched_fp, match_ms = matcher.match(struct)
            print(f"[Scan Match] PID={pid}, name={name}, matched={bool(matched_fp)}, harness={(matched_fp.get('id') if matched_fp else None)}")
            
            # 计算真实的展示名称 (如果已经识别/适配过，展示 Agent 真实身份，而非 .exe)
            display_name = name
            is_matched = bool(matched_fp)
            if matched_fp:
                fp_name = matched_fp.get("name")
                if fp_name and fp_name != "unknown-runtime":
                    display_name = f"{fp_name} ({name})"
            else:
                # 尚未命中指纹库：未逆向接管前展示为待调查状态
                display_name = f"未知 Agent ({name})"

            is_investigating = False
            with INVESTIGATION_LOCK:
                is_investigating = (pid in INVESTIGATING_PIDS)

            adapter_info = {
                "matched": is_matched,
                "investigating": is_investigating,
                "match_ms": match_ms,
                "harness_id": matched_fp.get("id") if matched_fp else "unregistered",
                "behavioral_class": (matched_fp.get("hook_recipe", {}).get("match_features", {}).get("behavioral_class")) if matched_fp else "unknown-runtime",
                "observation": (matched_fp.get("hook_recipe", {}).get("observation")) if matched_fp else ("⚡ Goose 正在非交互式自主逆向接管中..." if is_investigating else "未挂接 (需要首次逆向)"),
                "hook": (matched_fp.get("hook_recipe", {}).get("hook")) if matched_fp else "未挂接"
            }
            
            last_msg = get_last_semantic_message(pid, name, " ".join(cmdline))
            
            found_agents.append({
                "pid": pid,
                "name": display_name,
                "raw_exe": name,
                "score": score,
                "reasons": reasons,
                "cmdline": redact(" ".join(cmdline))[:250] + ("..." if len(" ".join(cmdline)) > 250 else ""),
                "adapter": adapter_info,
                "last_message": last_msg,
                "uptime_sec": int(time.time() - (pinfo.get("create_time") or time.time()))
            })

            # 自动接入闭环：若发现陌生 Agent 且尚未在调查中，立即在后台拉起 Goose (DeepSeek) 进行接管！
            if not is_matched and not is_investigating:
                threading.Thread(
                    target=run_autonomous_investigation,
                    args=(pid, struct),
                    name=f"analyst-worker-{pid}",
                    daemon=True
                ).start()

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    # 同类型 Agent 聚合 (Group by Agent Identity / Harness):
    # 将属于同一 Harness 或同名 Agent 的多个运行实例合并为一个治理卡片，避免同类型进程分散刷屏
    grouped_agents = {}
    for a in found_agents:
        # 聚类 Key：优先以命中的 harness_id 聚合；若未命中则以推导身份或执行入口聚合
        hid = a.get("adapter", {}).get("harness_id")
        if hid and hid != "unregistered":
            group_key = f"harness:{hid}"
        else:
            group_key = f"raw:{a.get('name')}"

        if group_key not in grouped_agents:
            # 建立主卡片，记录实例集合
            a_copy = dict(a)
            a_copy["instances"] = [a["pid"]]
            a_copy["all_pids"] = [a["pid"]]
            grouped_agents[group_key] = a_copy
        else:
            # 聚合到已有同类卡片中
            main_card = grouped_agents[group_key]
            main_card["instances"].append(a["pid"])
            main_card["all_pids"].append(a["pid"])
            # 保留更高的画像分与最新的消息
            if a["score"] > main_card["score"]:
                main_card["score"] = a["score"]
                main_card["reasons"] = a["reasons"]
            if a.get("adapter", {}).get("matched") and not main_card.get("adapter", {}).get("matched"):
                main_card["adapter"] = a["adapter"]
                main_card["name"] = a["name"]

    final_agents = list(grouped_agents.values())

    # 更新指纹库统计
    fp_count = 0
    fp_path = ROOT / "runtime" / "fingerprints.json"
    if fp_path.exists():
        try:
            fp_data = json.loads(fp_path.read_text(encoding="utf-8"))
            fp_count = len(fp_data.get("fingerprints", []))
        except Exception:
            pass

    with STATE_LOCK:
        SCAN_STATE["last_scan_time"] = time.strftime("%Y-%m-%d %H:%M:%S")
        SCAN_STATE["scan_count"] += 1
        SCAN_STATE["agents"] = final_agents
        SCAN_STATE["fingerprints_count"] = fp_count


def background_scanner_loop():
    while True:
        try:
            scan_agents_once()
        except Exception as e:
            print(f"[Scanner Error] {e}", file=sys.stderr)
        time.sleep(30)


HTML_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ASG 运行时治理 · 实时 Agent 监控看板</title>
<style>
  :root {
    --bg: #0b0f19;
    --card-bg: #151d2e;
    --border: #23324d;
    --text-primary: #f8fafc;
    --text-muted: #94a3b8;
    --accent: #38bdf8;
    --accent-glow: rgba(56, 189, 248, 0.15);
    --green: #4ade80;
    --amber: #fbbf24;
    --indigo: #818cf8;
    --tag-bg: #0b0f19;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; }
  body { background: var(--bg); color: var(--text-primary); padding: 24px; line-height: 1.5; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 16px; }
  .title { font-size: 20px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; }
  .pulse { width: 10px; height: 10px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; } }
  .meta-bar { display: flex; gap: 20px; font-size: 13px; color: var(--text-muted); }
  .meta-item b { color: var(--accent); }
  .refresh-btn { background: var(--border); border: none; color: #fff; padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 12px; transition: all 0.2s; }
  .refresh-btn:hover { background: #334769; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(460px, 1fr)); gap: 20px; }
  .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 10px; padding: 18px; display: flex; flex-direction: column; gap: 14px; box-shadow: 0 4px 12px rgba(0,0,0,0.25); }
  .card-top { display: flex; justify-content: space-between; align-items: flex-start; }
  .agent-name { font-size: 16px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 8px; }
  .pid-tag { font-size: 11px; background: #1e293b; color: var(--accent); padding: 2px 6px; border-radius: 4px; border: 1px solid #334155; }
  .score-badge { font-size: 12px; font-weight: 700; padding: 4px 8px; border-radius: 6px; }
  .score-high { background: rgba(56, 189, 248, 0.2); color: var(--accent); border: 1px solid rgba(56, 189, 248, 0.4); }
  .section-label { font-size: 11px; text-transform: uppercase; color: var(--text-muted); font-weight: 600; margin-bottom: 4px; }
  .cmdline { font-size: 11px; color: #cbd5e1; background: #0b0f19; padding: 8px; border-radius: 6px; border: 1px solid #1e293b; word-break: break-all; font-family: monospace; }
  .adapter-box { background: rgba(30, 41, 59, 0.5); border: 1px solid #23324d; border-radius: 6px; padding: 10px; display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
  .adapter-row { display: flex; justify-content: space-between; align-items: center; }
  .adapter-status { color: var(--green); font-weight: 600; display: inline-flex; align-items: center; gap: 4px; }
  .adapter-unmatched { color: var(--amber); }
  .adapter-working { color: var(--indigo); animation: blink 1.5s infinite; }
  @keyframes blink { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
  .msg-box { background: #080c14; border: 1px solid #1c273c; border-radius: 6px; padding: 10px; font-family: monospace; font-size: 11px; }
  .msg-header { display: flex; justify-content: space-between; color: var(--accent); margin-bottom: 6px; font-weight: 600; border-bottom: 1px dashed #1c273c; padding-bottom: 4px; }
  .msg-content { color: #e2e8f0; white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; }
  .footer { margin-top: 30px; text-align: center; font-size: 12px; color: var(--text-muted); display: flex; justify-content: center; gap: 15px; }
</style>
</head>
<body>

<div class="header">
  <div class="title">
    <div class="pulse"></div>
    ASG 运行时治理 · 实时 Agent 监控与自主接管
  </div>
  <div class="meta-bar">
    <div class="meta-item">扫描周期: <b>30s</b></div>
    <div class="meta-item">已存指纹: <b id="fp-count">-</b></div>
    <div class="meta-item">已扫轮次: <b id="scan-count">-</b></div>
    <div class="meta-item">上次更新: <b id="last-time">-</b></div>
    <button class="refresh-btn" onclick="triggerScan()">立即扫描</button>
  </div>
</div>

<div class="grid" id="agents-grid">
  <div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 60px;">正在进行初次扫描...</div>
</div>

<div class="footer">
  <div>OS-Level Zero-Prior Agent Governance</div>
  <div>·</div>
  <div>自动识别与 Goose 自主逆向适配闭环</div>
  <div>·</div>
  <div>每 5 秒刷新</div>
</div>

<script>
async function updateUI() {
  try {
    const res = await fetch('/api/state');
    const data = await res.json();
    document.getElementById('last-time').innerText = data.last_scan_time || '初始化中';
    document.getElementById('scan-count').innerText = data.scan_count;
    document.getElementById('fp-count').innerText = data.fingerprints_count;
    
    const grid = document.getElementById('agents-grid');
    if (!data.agents || data.agents.length === 0) {
      grid.innerHTML = '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 80px; background: var(--card-bg); border-radius: 12px; border: 1px dashed var(--border);">未发现存活的 Agent 进程 (阈值 >= 50)</div>';
      return;
    }
    
    let html = '';
    data.agents.forEach(a => {
      const isMatched = a.adapter && a.adapter.matched;
      const isInvestigating = a.adapter && a.adapter.investigating;
      
      let statusHtml = '';
      if (isMatched) {
        statusHtml = `<span class="adapter-status">✓ 已适配挂接 (${a.adapter.harness_id} · ${a.adapter.match_ms}ms)</span>`;
      } else if (isInvestigating) {
        statusHtml = `<span class="adapter-status adapter-working">⚡ 正在自主逆向接管中 (Goose Agent Work)...</span>`;
      } else {
        statusHtml = `<span class="adapter-status adapter-unmatched">⚡ 陌生 Runtime (等待调度逆向)</span>`;
      }
        
      let msgDetail = a.last_message ? a.last_message.detail : '暂无消息';
      if (typeof msgDetail === 'object') {
        msgDetail = JSON.stringify(msgDetail, null, 2);
      }

      const instanceCount = (a.instances && a.instances.length > 1) ? ` <span class="pid-tag" style="background: rgba(16, 185, 129, 0.2); color: #34d399; border-color: rgba(16, 185, 129, 0.4);">${a.instances.length} 实例聚合</span>` : '';
      const pidsList = (a.instances && a.instances.length > 1) ? `PIDs: ${a.instances.join(', ')}` : `PID: ${a.pid}`;

      html += `
        <div class="card">
          <div class="card-top">
            <div>
              <div class="agent-name">${a.name} <span class="pid-tag">${pidsList}</span>${instanceCount}</div>
              <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">原生程序: ${a.raw_exe} · 存活时间: ${a.uptime_sec}s</div>
            </div>
            <div class="score-badge score-high">画像分: ${a.score}</div>
          </div>
          
          <div>
            <div class="section-label">启动命令行与参数特征</div>
            <div class="cmdline">${a.cmdline}</div>
          </div>

          <div>
            <div class="section-label">Adapter 挂接与指纹状态</div>
            <div class="adapter-box">
              <div class="adapter-row">
                <span style="color: var(--text-muted);">状态:</span>
                ${statusHtml}
              </div>
              <div class="adapter-row">
                <span style="color: var(--text-muted);">运行时类:</span>
                <span style="color: #fff; font-family: monospace;">${a.adapter.behavioral_class}</span>
              </div>
              <div>
                <span style="color: var(--text-muted);">观测配方 (Recipe):</span>
                <div style="color: #cbd5e1; margin-top: 4px; font-size: 11px; line-height: 1.4;">${a.adapter.observation}</div>
              </div>
            </div>
          </div>

          <div>
            <div class="section-label">最新拦截/捕获的语义消息 (Semantic Ingestion)</div>
            <div class="msg-box">
              <div class="msg-header">
                <span>事件: ${a.last_message.event_type}</span>
                <span style="font-size: 10px; color: var(--text-muted);">${a.last_message.ts || ''}</span>
              </div>
              <div class="msg-content">${msgDetail}</div>
            </div>
          </div>
        </div>
      `;
    });
    grid.innerHTML = html;
  } catch (err) {
    console.error("更新失败:", err);
  }
}

async function triggerScan() {
  await fetch('/api/scan', { method: 'POST' });
  await updateUI();
}

setInterval(updateUI, 5000);
updateUI();
</script>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode("utf-8"))
        elif self.path == "/api/state":
            with STATE_LOCK:
                data = json.dumps(SCAN_STATE, ensure_ascii=False)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/scan":
            threading.Thread(target=scan_agents_once, daemon=True).start()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"status": "scanning"}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    port = 8080
    print("[Monitor] 执行首次进程环境扫描...")
    scan_agents_once()
    
    t = threading.Thread(target=background_scanner_loop, name="scanner-thread", daemon=True)
    t.start()
    print("[Monitor] 30s 扫描与自动接管引擎已启动")
    
    server = ThreadingHTTPServer(("127.0.0.1", port), MonitorHandler)
    print(f"[Monitor] Web 界面已就绪: http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
