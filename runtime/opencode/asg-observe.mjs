// ASG 纯观测插件（OpenCode 引擎扩展点 tool.execute.before/after）。
// 只记录：ts, event_type, adapter_source, nonce, pid（自报，接收端独立校验
// PID+create_time）, call_id(关联 ID), tool 名, result 状态；绝不记录参数内容
// 或密钥；任何失败均吞掉（fail-open），不影响原工具执行。
// 部署：复制到测试工作区 .opencode/plugins/asg-observe.mjs（非全局）。
// 接收端必须通过 psutil.Process(pid).create_time() 与预期 create_time 比对，
// 不能只信任自报 pid 或随机文件名。
import { appendFileSync } from "node:fs";
import { join } from "node:path";

const EVENTS = process.env.ASG_OBSERVE_EVENTS || "";
const NONCE = process.env.ASG_OBSERVE_NONCE || "";
const LOCK = process.env.ASG_OBSERVE_LOCK || "1";
const VER = "asg-observe-v1";

function now() {
  return new Date().toISOString();
}
function emit(ev) {
  if (!EVENTS) return;
  try {
    appendFileSync(EVENTS, JSON.stringify(ev) + String.fromCharCode(10));
  } catch (e) {
    // 落盘失败不影响工具调用
  }
}
function base(input, et) {
  return {
    ts: now(), event_type: et, adapter_source: VER, sdk: "opencode-plugin",
    nonce: NONCE, pid: (process.pid || 0), call_id: input && input.callID,
    tool: input && input.tool, lock: LOCK,
  };
}
export default async ({ directory }) => {
  // 启动（handshake）事件：插件被引擎真实加载时立即落盘一条
  emit({ ...base({}, "hook.loaded"), directory });
  return {
    "tool.execute.before": async (input, output) => {
      // 不写 output/args；只记工具名与关联 ID
      emit({ ...base(input, "tool.execute.before"), outcome: "started" });
    },
    "tool.execute.after": async (input) => {
      emit({ ...base(input, "tool.execute.after"), outcome: "finished" });
    },
  };
};
