import { appendFileSync } from "node:fs";

const init = async () => {
  const mod = await import("file:///Users/mac/个人项目/asg-os-sensor-stage1/runtime/opencode/asg-observe.mjs");
  const plug = await mod.default({ directory: "/tmp" });
  const input = { tool: "bash", callID: "call-abc-123" };
  await plug["tool.execute.before"](input, { args: { command: "ls -la", secret: "TOPSECRET" } });
  await plug["tool.execute.after"](input);
  process.exit(0);
};
init().then(() => {}).catch((e) => { console.error(e); process.exit(1); });
