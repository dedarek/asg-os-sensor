// ASG 纯观测插件（OpenCode 引擎扩展点 tool.execute.before/after）。
// .js：引擎 glob"{plugin,plugins}/*.{ts,js}" 扫描 .ts/.js（.mjs 不在范围）。
// 加载时冻结 runid；每次 emit 核对 manifest 仍 active 且同 run。卸载(active 变 false/改名)或
// 重装(新 runid)后，旧已加载回调立即停止写——旧回调不复活，必须重新加载插件握手。
// nonce/事件路径不依赖 env：从 .asg-observe/manifest.json + runs/<runid>/{nonce,events.jsonl} 读取。
// 事件文件 0600；只记录 ts/event_type/adapter_source/nonce/pid/call_id/tool/outcome；fail-open。
"use strict";
import * as fs from "node:fs";
import * as path from "node:path";
import { fileURLToPath } from "node:url";

const DIR = path.dirname(fileURLToPath(import.meta.url));
const STATEDIR = path.join(DIR, ".asg-observe");
const MANIFEST = path.join(STATEDIR, "manifest.json");

function readManifest() {
  try {
    return JSON.parse(fs.readFileSync(MANIFEST, "utf8"));
  } catch (e) {
    return null;
  }
}

// 初始化时冻结绑定（新加载才能绑定新 run；卸载/重装后旧回调见到 run 不同即停）
const FROZEN = (() => {
  const m = readManifest();
  return m && m.active ? m.runid : null;
})();

function activeRun() {
  const m = readManifest();
  if (!m || !m.active || m.runid !== FROZEN) return null; // 同 run 且 active
  return m.runid;
}

function emit(ev) {
  const runid = activeRun();
  if (!runid) return; // 卸载/重装后立即停写
  let nonce = "";
  try {
    nonce = fs.readFileSync(path.join(STATEDIR, "runs", runid, "nonce"), "utf8").trim();
  } catch (e) {
    return;
  }
  if (!nonce) return;
  const row = {
    ts: new Date().toISOString(),
    event_type: ev.event_type,
    adapter_source: "asg-observe-v1",
    sdk: "opencode-plugin",
    nonce: nonce,
    pid: process.pid || 0,
    call_id: ev.call_id || null,
    tool: ev.tool || null,
    outcome: ev.outcome || null,
  };
  try {
    fs.mkdirSync(path.join(STATEDIR, "runs", runid), { recursive: true });
    const f = path.join(STATEDIR, "runs", runid, "events.jsonl");
    const fd = fs.openSync(f, "a", 0o600);
    try {
      fs.writeSync(fd, JSON.stringify(row) + "\n");
    } finally {
      fs.closeSync(fd);
    }
    try {
      fs.chmodSync(f, 0o600);
    } catch (e) {}
  } catch (e) {
    // 落盘失败不影响工具调用
  }
}

const AsgObserve = async ({ directory }) => {
  // 握手：插件被引擎真实加载时立即落盘一条（只在新加载时产生）
  emit({ event_type: "hook.loaded" });
  return {
    "tool.execute.before": async (input, output) => {
      emit({ event_type: "tool.execute.before", call_id: input && input.callID, tool: input && input.tool, outcome: "started" });
    },
    "tool.execute.after": async (input) => {
      emit({ event_type: "tool.execute.after", call_id: input && input.callID, tool: input && input.tool, outcome: "finished" });
    },
  };
};

// OpenCode 1.18.x loads the V1 object shape; newer releases also accept the
// named function export. Keep both entry shapes while the adapter is tested
// across engine versions.
export { AsgObserve };
export default { id: "asg-observe", server: AsgObserve };
