// ASG 纯观测插件（OpenCode 引擎扩展点 tool.execute.before/after）。
// 本文件是 CJS .js：引擎加载器 glob"{plugin,plugins}/*.{ts,js}" 才会发现它
// （.mjs 不在扫描范围）。nonce 与事件路径不依赖注入环境变量，而是从插件同目录
// .asg-observe/ 下的 nonce 与 events.jsonl 文件读取 —— 用户打开工作区即可工作，
// 无需启动脚本自定义 env，也不要求全局重启。
// 只记录：ts, event_type, adapter_source, nonce, pid（自报，接收端独立校验
// PID+create_time）, call_id(关联 ID), tool 名, outcome；绝不记录参数内容或密钥；
// 任何失败均吞掉（fail-open），不影响原工具执行。事件文件权限强制 0600。
"use strict";
const fs = require("node:fs");
const path = require("node:path");

const DIR = __dirname;
const STATEDIR = path.join(DIR, ".asg-observe");
const NONCE_FILE = path.join(STATEDIR, "nonce");
const EVENTS_FILE = path.join(STATEDIR, "events.jsonl");

function readNonce() {
  try {
    return fs.readFileSync(NONCE_FILE, "utf8").trim();
  } catch (e) {
    return "";
  }
}

function emit(ev) {
  const nonce = readNonce();
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
    fs.mkdirSync(STATEDIR, { recursive: true });
    const fd = fs.openSync(EVENTS_FILE, "a", 0o600);
    try {
      fs.writeSync(fd, JSON.stringify(row) + "\n");
    } finally {
      fs.closeSync(fd);
    }
    try {
      fs.chmodSync(EVENTS_FILE, 0o600);
    } catch (e) {}
  } catch (e) {
    // 落盘失败不影响工具调用
  }
}

module.exports = async ({ directory }) => {
  // 启动（handshake）：插件被引擎真实加载时立即落盘一条。不输出 directory。
  emit({ event_type: "hook.loaded" });
  return {
    "tool.execute.before": async (input, output) => {
      // 不写 output/args；只记工具名与关联 ID
      emit({ event_type: "tool.execute.before", call_id: input && input.callID, tool: input && input.tool, outcome: "started" });
    },
    "tool.execute.after": async (input) => {
      emit({ event_type: "tool.execute.after", call_id: input && input.callID, tool: input && input.tool, outcome: "finished" });
    },
  };
};
