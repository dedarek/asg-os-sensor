# 当前可展示的真实正例（2026-09-10）

## 看什么
打开 http://127.0.0.1:8081/ ，找到隔离 CLI 实例（当前 PID 77504，OpenCode）。
应看到：**指纹复用（未调用 Goose） / Hook 已观测（before/after 配对） / 执行事件已采集**。
用户原有桌面应用未重启、未安装本轮 Hook。此处不是所有 Agent 已自动纳管。

## 实际验证了什么
1. 原始真实实例 PID 44187：Goose 通过本机工具查到安装包、SDK、嵌入式加载说明；在监督者分阶段引导下生成完整 Hook 文件内容。
2. 通用 file-plan 安装器写入隔离工作区；实际安装字节与 Goose 候选内容一致，未调用旧产品适配器。
3. 真实模型驱动目标读取测试文件：独立校验得到 hook.loaded 与同一 call_id 的 tool.execute.before/after，0 无效事件。
4. 仅重启本轮自建 CLI，成为 PID 77504：构建、启动参数、工作目录精确匹配，复用同一已安装文件；**无新增 Goose 调用**。新 nonce、新 PID/create_time 的真实 read 调用再次产生配对事件，0 无效事件。

## 不能包装成已完成的部分
- 这是**监督引导的单目标正例**，不是未干预盲测，也不是任意新 Agent 一键接入的覆盖率证明。
- 监督者处理了调查工具崩溃、上下文、模型协议、候选格式和测试驱动配置问题。没有人工撰写/替换目标 Hook 源码。
- 传输层显式记录了 JSON 字符串解码及缺失末尾对象括号的窄修复；不修复源码、字段或安装前置条件。
- 模型/MCP/Skill/规则资产调查仍不齐全；测试驱动设置的模型路由不冒充 Goose 的资产发现。
- 页面事件是本实例的验收快照，不是持续健康/完整覆盖保证。平台授权注册、语义阻断尚未实现。
- 首次安装脚本曾报测试驱动语法错误；真实目标配置更新接口写入了其未加载的文件，随后将测试模型配置写入此隔离实例显式配置目录，才完成真实调用。这些不是 Hook 适配源码修改。

## 本机证据与复现
所有路径相对仓库根。原始秘密 nonce 不进入页面和汇报。
- 元信息：`artifacts/stage1/learned-demo/current.json`
- 源调查：`artifacts/stage1/learned-demo/run-20260910-203924/investigations/pid_44187_1789052847065/`
- 源候选、原始工具调用、生命周期记录均在上述目录；来源可追溯到更早的同实例调查。
- 安装批准与摘要：同 run 下 `approved-candidate.json`、`install-result.json`。
- 首次校验：`verification-first.json`；新实例校验：`verification-reuse.json`；复用范围与零新增调查：`reuse-result.json`。
- 原始事件：同 run 下 `events/events-first.jsonl`、`events/events.jsonl`（含本地 nonce，不对外分享原文）。
- 重发无害工具测试：`python3 -B artifacts/stage1/learned-demo/trigger_real_tool.py`，仅对本轮隔离工作区有效，不操作用户桌面 Agent；页面目前保存的是既有验收快照，重发后需重新校验发布才能更新它。
- 8081 启动：`python3 artifacts/stage1/dashboard-review-vIbGLtFEA/start-dashboard.py`；先确认旧实例已停止，勿重复占用端口。
- 安装源字节 SHA256：`b8f2a8d2b627b66689ea4402f52c9af83338c0bd758141f68676ec527f47649f`。

历史调查和失败证据均保留；不将修复后回归当作独立盲测成绩。
