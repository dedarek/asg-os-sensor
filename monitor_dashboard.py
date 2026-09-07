"""ASG Agent Live Monitor (30s 周期实时扫描与能力看板)
功能：
1. 后台周期（默认 30s，支持手动立即触发）扫描 OS 进程
2. OS Sensor 行为特征画像打分（识别 Agent）
3. Matcher 结构特征指纹比对（获取 Adapter/Recipe 信息）
4. 读取并展示该 Agent Adapter 挂接详情及捕获到的最后一条语义消息
5. 轻量 Web 服务 (8080)，单页纯原生 HTML/CSS/JS 自动轮询刷新，无第三方重依赖
"""
import os
import sys
import json
import time
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import psutil

# 确保 asg-os-sensor 根目录在 path 中
ROOT = Path("D:/proj/asg-os-sensor")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from asg_os_sensor import Sensor, load_policies
from runtime import analyzer, matcher
from runtime.stream_parser import redact

# 共享状态
STATE_LOCK = threading.Lock()
SCAN_STATE = {
    "last_scan_time": None,
    "scan_interval": 30,
    "scan_count": 0,
    "agents": [], # 识别出的 Agent 列表
    "recent_events": [], # 最近拦截/捕获的语义事件
    "fingerprints_count": 0
}

def get_last_semantic_message(pid: int, exe_name: str, cmdline: str) -> dict:
    """获取该 Agent 进程真实关联的语义消息，绝不把其他进程或历史残留瞎挂上去"""
    artifacts_dir = ROOT / "e2e" / "artifacts" / "unknown-runtime"
    
    # 查找是否有挂接在当前 PID 或明确匹配该目标会话的流
    stream_candidates = [
        artifacts_dir / "second" / "semantic_events.jsonl",
        artifacts_dir / "first" / "semantic_events.jsonl",
    ]
    
    # 检查进程是否是真正被接管的 Agent
    for c in stream_candidates:
        if c.exists():
            try:
                manifest_file = c.parent.parent / "execution_manifest.json"
                # 检查 manifest 中是否记录过该 PID
                matched_pid = False
                if manifest_file.exists():
                    mdata = json.loads(manifest_file.read_text(encoding="utf-8", errors="ignore"))
                    pids = [
                        mdata.get("phases", {}).get("first", {}).get("target_pid"),
                        mdata.get("phases", {}).get("second", {}).get("target_pid")
                    ]
                    if pid in pids:
                        matched_pid = True
                
                # 如果 PID 匹配，或者正在执行 e2e 且命令签名一致
                if matched_pid:
                    lines = [l.strip() for l in c.read_text(encoding="utf-8", errors="ignore").splitlines() if l.strip()]
                    if lines:
                        last_obj = json.loads(lines[-1])
                        return {
                            "source": c.parent.name,
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
    
    # 遍历进程
    for proc in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            pinfo = proc.info
            pid = pinfo.get("pid")
            name = pinfo.get("name") or ""
            cmdline = pinfo.get("cmdline") or []
            
            # 过滤明显无关进程
            if not cmdline or pid == os.getpid() or name.lower() in ["system", "registry", "smss.exe"]:
                continue
                
            w = sensor.wrap_pid(pid)
            score, reasons = sensor.agent_score(w)
            
            # 达到 Agent 判定阈值 (默认 50)
            if score >= 50:
                # 提取进程上下文与指纹比对
                struct = {}
                try:
                    struct = analyzer.analyze(pid)
                except Exception:
                    struct = {
                        "pid": pid,
                        "exe": name,
                        "runtime": "native",
                        "argv_shape": [(x if str(x).startswith("-") else "<value>") for x in cmdline],
                        "config_dirs": []
                    }
                
                matched_fp, match_ms = matcher.match(struct)
                
                adapter_info = {
                    "matched": bool(matched_fp),
                    "match_ms": match_ms,
                    "harness_id": matched_fp.get("id") if matched_fp else "unregistered",
                    "behavioral_class": (matched_fp.get("hook_recipe", {}).get("match_features", {}).get("behavioral_class")) if matched_fp else "unknown-runtime",
                    "observation": (matched_fp.get("hook_recipe", {}).get("observation")) if matched_fp else "未挂接 (需要首次逆向)",
                    "hook": (matched_fp.get("hook_recipe", {}).get("hook")) if matched_fp else "未挂接"
                }
                
                last_msg = get_last_semantic_message(pid, name, " ".join(cmdline))
                
                found_agents.append({
                    "pid": pid,
                    "name": name,
                    "score": score,
                    "reasons": reasons,
                    "cmdline": redact(" ".join(cmdline))[:250] + ("..." if len(" ".join(cmdline)) > 250 else ""),
                    "adapter": adapter_info,
                    "last_message": last_msg,
                    "uptime_sec": int(time.time() - (pinfo.get("create_time") or time.time()))
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

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
        SCAN_STATE["agents"] = found_agents
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
<title>ASG Agent 实时监控看板 (30s 扫描)</title>
<style>
  :root {
    --bg: #0f172a;
    --card-bg: #1e293b;
    --border: #334155;
    --text-primary: #f8fafc;
    --text-muted: #94a3b8;
    --accent: #38bdf8;
    --accent-glow: rgba(56, 189, 248, 0.15);
    --green: #4ade80;
    --amber: #fbbf24;
    --tag-bg: #0f172a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, monospace; }
  body { background: var(--bg); color: var(--text-primary); padding: 24px; line-height: 1.5; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; border-bottom: 1px solid var(--border); padding-bottom: 16px; }
  .title { font-size: 20px; font-weight: 700; color: #fff; display: flex; align-items: center; gap: 10px; }
  .pulse { width: 10px; height: 10px; border-radius: 50%; background: var(--green); box-shadow: 0 0 8px var(--green); animation: pulse 2s infinite; }
  @keyframes pulse { 0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; } }
  .stats-bar { display: flex; gap: 20px; font-size: 13px; color: var(--text-muted); }
  .badge { background: #334155; padding: 4px 10px; border-radius: 6px; color: #fff; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(480px, 1fr)); gap: 20px; }
  .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 20px; display: flex; flex-direction: column; gap: 14px; position: relative; }
  .card:hover { border-color: var(--accent); box-shadow: 0 4px 20px var(--accent-glow); }
  .card-top { display: flex; justify-content: space-between; align-items: flex-start; }
  .agent-name { font-size: 17px; font-weight: 700; color: #fff; }
  .pid-tag { font-size: 12px; color: var(--accent); background: rgba(56, 189, 248, 0.1); border: 1px solid rgba(56, 189, 248, 0.3); border-radius: 4px; padding: 2px 6px; }
  .score-badge { font-size: 12px; padding: 3px 8px; border-radius: 12px; font-weight: 700; }
  .score-high { background: rgba(74, 222, 128, 0.15); color: var(--green); border: 1px solid rgba(74, 222, 128, 0.3); }
  .cmdline { font-size: 12px; color: var(--text-muted); background: var(--tag-bg); padding: 8px 10px; border-radius: 6px; word-break: break-all; border: 1px solid var(--border); }
  .section-label { font-size: 11px; text-transform: uppercase; color: var(--text-muted); font-weight: 700; letter-spacing: 0.5px; margin-bottom: 4px; }
  .adapter-box { background: rgba(15, 23, 42, 0.6); border: 1px solid var(--border); border-radius: 8px; padding: 12px; display: flex; flex-direction: column; gap: 8px; font-size: 12px; }
  .adapter-row { display: flex; justify-content: space-between; }
  .adapter-status { color: var(--green); font-weight: 600; display: flex; align-items: center; gap: 4px; }
  .adapter-unmatched { color: var(--amber); }
  .msg-box { background: rgba(56, 189, 248, 0.05); border: 1px solid rgba(56, 189, 248, 0.2); border-radius: 8px; padding: 12px; font-size: 12px; }
  .msg-header { display: flex; justify-content: space-between; color: var(--accent); font-weight: 600; margin-bottom: 6px; }
  .msg-content { color: #e2e8f0; white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow-y: auto; background: #0b1120; padding: 8px; border-radius: 4px; font-family: monospace; }
  .footer { margin-top: 30px; text-align: center; font-size: 12px; color: var(--text-muted); display: flex; justify-content: center; gap: 15px; }
  .btn-refresh { background: #2563eb; color: #fff; border: none; padding: 6px 14px; border-radius: 6px; font-size: 12px; cursor: pointer; }
  .btn-refresh:hover { background: #1d4ed8; }
</style>
</head>
<body>

<div class="header">
  <div class="title">
    <div class="pulse"></div>
    ASG 运行时治理 · 实时 Agent 监控看板
  </div>
  <div class="stats-bar">
    <div>扫描周期: <span class="badge">30s</span></div>
    <div>已存指纹: <span class="badge" id="fp-count">-</span></div>
    <div>已扫总轮次: <span class="badge" id="scan-count">-</span></div>
    <div>上次更新: <span id="last-time">-</span></div>
    <button class="btn-refresh" onclick="triggerScan()">立即扫描</button>
  </div>
</div>

<div class="grid" id="agents-grid">
  <div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 60px;">正在进行初次扫描...</div>
</div>

<div class="footer">
  <div>OS-Level Zero-Prior Agent Governance</div>
  <div>·</div>
  <div>自动轮询: 每 5 秒刷新前端展示</div>
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
      const statusHtml = isMatched 
        ? `<span class="adapter-status">✓ 已命中指纹库 (${a.adapter.harness_id} · ${a.adapter.match_ms}ms)</span>`
        : `<span class="adapter-status adapter-unmatched">⚡ 陌生 Runtime (需逆向接管)</span>`;
        
      let msgDetail = a.last_message ? a.last_message.detail : '暂无消息';
      if (typeof msgDetail === 'object') {
        msgDetail = JSON.stringify(msgDetail, null, 2);
      }

      html += `
        <div class="card">
          <div class="card-top">
            <div>
              <div class="agent-name">${a.name} <span class="pid-tag">PID: ${a.pid}</span></div>
              <div style="font-size: 11px; color: var(--text-muted); margin-top: 2px;">存活时间: ${a.uptime_sec}s</div>
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

// 每 5 秒轮询一次状态
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
    # 先做一次同步扫描预热
    print("[Monitor] 执行首次进程环境扫描...")
    scan_agents_once()
    
    # 启动 30s 周期后台扫描线程
    t = threading.Thread(target=background_scanner_loop, name="scanner-thread", daemon=True)
    t.start()
    print("[Monitor] 30s 扫描线程已启动")
    
    server = HTTPServer(("127.0.0.1", port), MonitorHandler)
    print(f"[Monitor] Web 界面已启动: http://127.0.0.1:{port}")
    server.serve_forever()

if __name__ == "__main__":
    main()
