// ASG 纯观测插件（OpenCode 引擎扩展点 tool.execute.before/after）。
// 本文件是 CJS .js：引擎加载器 glob"{plugin,plugins}/*.{ts,js}" 才会发现它
// （.mjs 不在扫描范围）。nonce 与事件路径不依赖注入环境变量，而是从插件同目录
// .asg-observe/runs/<runid>/ 下的 nonce 与 events.jsonl 文件读取 —— 用户打开工作区
// 即可工作，无需启动脚本自定义 env，也不要求全局重启。
// 只记录：ts, event_type, adapter_source, nonce, pid（自报，接收端独立校验
// PID+create_time）, call_id(关联 ID), tool 名, outcome；绝不记录参数内容或密钥。
// nonce 缺失（卸载后）立即停止写事件；任何失败均吞掉（fail-open），不影响原工具执行。
"use strict";
const fs = require("node:fs");
const path = require("node:path");

const DIR = __dirname;
const STATEDIR = path.join(DIR, ".asg-observe");

function activeRun() {
  // 安装器维护 runs/<runid>/{nonce,events.jsonl}；manifest.json 记录当前 active run
  try {
    const man = JSON.parse(fs.readFileSync(path.join(STATEDIR, "manifest.json"), "utf8"));
    return man.active ? man.runid : null;
  } catch (e) {
    return null;
  }
}

function emit(ev) {
  // 卸载后（或从未安装）nonce 缺失 -> 立即停止写，不产生任何事件
  const runid = activeRun();
  if (!runid) return;
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

module.exports = async ({ directory }) => {
  // 启动（handshake）：插件被引擎真实加载时立即落盘一条。不输出 directory。
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