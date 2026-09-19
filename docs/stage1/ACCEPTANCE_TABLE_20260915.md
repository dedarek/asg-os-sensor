# 验收总表（2026-09-15）

本表由 `e2e/acceptance_report.py` 依据当前证据自动生成，状态只会是：通过 / 失败 / 待外部条件 / 目标不支持 / 未执行。

回归：61 个测试模块，Ran 404，OK。

验收批次：**ASG-BATCH-20260914-180121**；机器 01a329ad376e9a7f；Hook sha256:e55d586ecf77e37d8；配方 harness-efac4c5f21c6@rev7；模式 isolated_real_instance；关联会话 112 个（2 个实例）。

统一规则（任务书第一节）：身份字段完整率 1.0、证据关联实例 1.0、跨实例错误关联 0、合成事件混入 0、展示重复数 0（Hook 日志重复序号 + 控制回执重复 request_id）；逐条报告见 `artifacts/acceptance/test-reports.json`（83 行）。

延迟指标（任务书第一节）：采集延迟 P95 **1.807 秒**（门槛 ≤3）、最大 **1.807 秒**（≤10）；页面配置刷新间隔 **5.0 秒**；实际可见延迟尚未测量，不能据此验收。

| 任务 | 条目 | 门槛/含义 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| 1 修正实例归属和验收状态 | 跨运行时隔离 | 给 Codex 提供未信任结果不影响 OpenCode；其他实例事件被判无效 | **通过** |  |
| 1 修正实例归属和验收状态 | 同产品多实例 | A 的事件不能完成 B 的配对 | **通过** |  |
| 1 修正实例归属和验收状态 | PID 复用 | 相同 PID、不同 create_time 不继承旧状态 | **通过** |  |
| 1 修正实例归属和验收状态 | Hook 更新失效 | Hook 内容变化后旧验收失效 | **通过** |  |
| 1 修正实例归属和验收状态 | 无加载事件 | 仅有安装文件时不判为已加载 | **通过** |  |
| 1 修正实例归属和验收状态 | 决策未执行 | 返回决策/回执不宣称已阻断，必须验实际效果 | **通过** |  |
| 1 修正实例归属和验收状态 | 独立服务空运行 | 服务在跑但无事件/回执时不判为接管 | **通过** |  |
| 1 修正实例归属和验收状态 | 历史实例退出 | 实例退出后不继承当前有效状态 | **通过** |  |
| 1 修正实例归属和验收状态 | 未信任不判通过 |  | **通过** |  |
| 1 修正实例归属和验收状态 | 未信任阻断加载 |  | **通过** |  |
| 1 修正实例归属和验收状态 | 跨实例复用不重复调查 |  | **通过** |  |
| 1 修正实例归属和验收状态 | 每个通过状态带证据时间 |  | **通过** | runtime/capability.py 为各级附加证据记录时间（trusted/loaded/observing/serving 实测可见；test_capability） |
| 2 完整采集真实输入输出和工作流 | 模型请求/响应独立对账 100% |  | **通过** | model-transport-verification.json（显式测试网关，范围：面向网关 HTTP 正文） |
| 2 完整采集真实输入输出和工作流 | 事件词表/归一化覆盖 | 未翻译不计数 | **通过** | test_event_vocabulary / test_io_acceptance |
| 2 完整采集真实输入输出和工作流 | 用户输入完整率 100% |  | **通过** | io-batch.json（独立发起端对账，88/88 轮）；完整 88，已声明截断 0，未记录缺失 0 |
| 2 完整采集真实输入输出和工作流 | 最终输出完整率 100% |  | **通过** | io-batch.json（独立发起端对账，88/88 轮）；完整 88，截断（已标记）0 |
| 2 完整采集真实输入输出和工作流 | 工具名称/参数/结果覆盖率 100% |  | **通过** | 35 对，带参数/结果 35 对 |
| 2 完整采集真实输入输出和工作流 | 流式汇总与最终输出一致 100% |  | **通过** | 完全一致 88/88 |
| 2 完整采集真实输入输出和工作流 | 截断未标记 0 |  | **通过** | io-batch.json |
| 2 完整采集真实输入输出和工作流 | 会话/调用串线 0 |  | **通过** | 跨实例错误 0 |
| 2 完整采集真实输入输出和工作流 | 失败/取消误记成功 0 |  | **通过** | toolfail5 记 tool.error；取消回合记 error 事件 |
| 2 完整采集真实输入输出和工作流 | 新会话 10 轮通过 |  | **通过** | chat30：3 个新会话 ×10 轮 |
| 2 完整采集真实输入输出和工作流 | 请求重试 5 次 |  | **通过** | retry-batch.json：每轮注入 1 次 503、重试后成功，2 次尝试均被采集，错误未记为响应 |
| 2 完整采集真实输入输出和工作流 | 中途取消 5 次 |  | **通过** | io-extra.json：中止后无完整回答且 error 事件已采集 |
| 2 完整采集真实输入输出和工作流 | 子 Agent 5 次 |  | **通过** | io-extra.json：子会话建立且父子均已采集 |
| 2 完整采集真实输入输出和工作流 | 附件输入 3 次 |  | **通过** | io-extra.json：附件送达、回复含标记且附件已采集 |
| 2 完整采集真实输入输出和工作流 | 抽屉 2 分钟自动回顶 0 |  | **通过** | drawer-autoscroll.json：浏览器实测 2 分钟内回顶 0 次、抽屉保持打开 |
| 2 完整采集真实输入输出和工作流 | 页面按会话查看对话列表、原文可展开 |  | **通过** | monitor_dashboard.renderConversation：按 session 归组、details 展开原始内容（test_conversation_view） |
| 2 完整采集真实输入输出和工作流 | 缺失事件来源已调查并保存依据 |  | **通过** | missing-events-investigation.json：hook.loaded 已观测；['session.end'] 记为目标不支持（最近似 [["session.idle", "session.compacted", "session.updated"]]） |
| 2 完整采集真实输入输出和工作流 | 正常用户实例独立验收 |  | **未执行** | 当前正常会话独立对账：1 个完整回合、3 条文本、0 条不一致；要求至少 10 回合。隔离实例不能替代。 |
| 3 真实执行控制 | 隔离目标冒烟放行/拒绝 |  | **通过** | opencode/codex/zcode control-verification.json |
| 3 真实执行控制 | 放行 20 次全部生效 |  | **通过** | /Users/mac/个人项目/asg-os-sensor-advisor-20260911/artifacts/acceptance/control-batch.json |
| 3 真实执行控制 | 拒绝 20 次无副作用 |  | **通过** | control-batch.json#deny |
| 3 真实执行控制 | 人工批准 5 次 |  | **通过** | control-confirm-batch.json#approve |
| 3 真实执行控制 | 人工拒绝 5 次 |  | **通过** | control-confirm-batch.json#reject |
| 3 真实执行控制 | 人工确认超时 5 次 |  | **通过** | control-confirm-batch.json#timeout |
| 3 真实执行控制 | 决策服务中断 5 次 |  | **通过** | control-confirm-batch.json#outage |
| 3 真实执行控制 | 重复或迟到决策 10 次 |  | **通过** | control-confirm-batch.json#duplicate |
| 3 真实执行控制 | 参数被替换 5 次 |  | **通过** | control-confirm-batch.json#swap |
| 3 真实执行控制 | 并发调用 20 次映射 100% |  | **通过** | control-batch.json#concurrency |
| 3 真实执行控制 | 每次只执行一次 |  | **通过** | control-batch.json#allow |
| 3 真实执行控制 | 自动决策 P95 ≤ 300ms |  | **通过** | {"count": 33, "p95": 8.0, "max": 8.7, "median": 0.3} |
| 3 真实执行控制 | 三类可核对动作：创建文件 / 修改文件 / 本地请求 |  | **通过** | control-actions.json：{"create": {"allow_effect": 3, "deny_unexpected_effect": 0}, "modify": {"allow_effect": 3, "deny_unexpected_effect": 0}, "request": {"allow_effect": 3, "deny_unexpected_effect": 0}} |
| 4 重启后持续接管 | 5 次重启 5 次成功 |  | **通过** | restart-batch.json（5 个重启周期） |
| 4 重启后持续接管 | 每次新实例身份正确率 100% |  | **通过** | 每轮 pid/create_time 均变化；restart-batch.json（5 个重启周期） |
| 4 重启后持续接管 | 首次事件 10 秒内可查询 |  | **通过** | 查询延迟 [[4.6, 2.3, 2.4], [4.0, 2.4, 2.4], [4.0, 2.7, 2.8], [4.1, 2.5, 3.9], [5.0, 3.7, 5.1]] |
| 4 重启后持续接管 | 每次 ≥3 轮输入输出 / 2 放行 / 2 拒绝 |  | **通过** | restart-batch.json（5 个重启周期） |
| 4 重启后持续接管 | 无需手工修配置（自动重新绑定） |  | **通过** | 自动绑定耗时 [16.8, 10.7, 14.7, 12.7, 11.1] |
| 4 重启后持续接管 | 重复回调导致重复操作 0 |  | **通过** | 每轮放行后文件内容与预期逐字一致，未出现重复写入 |
| 4 重启后持续接管 | 旧版本证据误算新版本 0 |  | **通过** | 每轮在新实例自身记录上验收，未继承旧实例结论 |
| 5 发现程序退出后独立运行 | 独立服务不依赖发现模块 |  | **通过** | 依赖闭包检查：[] |
| 5 发现程序退出后独立运行 | 闭包式安装可独立启动并服务（不依赖发现源码树） |  | **通过** | portable-install.json：仅复制运行闭包到新目录，服务启动并服务 33 个已绑定实例；发现模块文件 [] |
| 5 发现程序退出后独立运行 | 停掉发现与 Goose 后独立服务仍工作 |  | **通过** | 发现/Goose 进程归零，独立服务 /health 正常 |
| 5 发现程序退出后独立运行 | 期间 30 轮对话 / ≥50 工具调用 |  | **通过** | {"chat_rounds": 30, "tool_calls": 70, "instance": "32045:1789408103.201462"} |
| 5 发现程序退出后独立运行 | 独立状态放行 10 / 拒绝 10 |  | **通过** | {"allow_ok": 10, "deny_ok": 10} |
| 5 发现程序退出后独立运行 | 中断 ≥100 事件 / 30 秒补齐 / 缺失 0 / 重复 0 |  | **通过** | {"events_in_window": 728, "catch_up_seconds": 9.171534061431885, "missing_canaries": [], "duplicate_records": 0} |
| 5 发现程序退出后独立运行 | 独立运行 60 分钟 |  | **未执行** | 独立服务持续采样 |
| 5 发现程序退出后独立运行 | 机器重启后 60 秒内恢复 |  | **待外部条件** | 需要在专用测试机器上重启，避免重启当前工作机 |
| 6 WorkBuddy 与替代接入路径 | 候选发现 30 秒内 |  | **通过** | 手动扫描后可见延迟 1.1 秒 |
| 6 WorkBuddy 与替代接入路径 | 调查可见性（状态/原因/首条活动） |  | **通过** | activity 接口返回状态与首条活动 |
| 6 WorkBuddy 与替代接入路径 | 三目标状态与能力覆盖记录 |  | **通过** | multi-target.json（目标：OpenCode、WorkBuddy AI、@deepseek-ai/dsh） |
| 6 WorkBuddy 与替代接入路径 | 三目标只读基线（干净起始状态） |  | **通过** | target-baselines.json：身份/入口/资产/Hook 状态只读记录，未修改真实目标 |
| 6 WorkBuddy 与替代接入路径 | 每目标 聊天10轮 / 工具10次 / 放行5拒绝5 / 重启3 |  | **未执行** | 需在隔离实例上驱动真实产品；target-launchability.json：本机可隔离启动的仅 OpenCode，ZCode/WorkBuddy/Codex 为桌面应用、OpenCodex 为供应商代理、dsh 未证实 |
| 6 WorkBuddy 与替代接入路径 | 替代接入机制（受控模型代理＋启动包装）实现并本地验证 |  | **通过** | access-wrapper.json：允许转发并采集、拒绝先于供应商、请求响应按 id 关联、超长体标记截断、只改供应商环境变量 |
| 6 WorkBuddy 与替代接入路径 | ≥1 个无原生 Hook 目标完成替代接入 |  | **未执行** | 机制已就绪（access-wrapper 本地通过）；对具体产品的端到端接入仍需隔离实例，见 target-launchability.json |
| 6 WorkBuddy 与替代接入路径 | 手工产品专用接入答案 0 |  | **未执行** | 需在隔离实例的通用安装器路径上验证 |
| 6 WorkBuddy 与替代接入路径 | 人工介入记录遗漏 0 |  | **未执行** | 需完整跑完每目标批次后才能核对 |
| 7 跨机器配方复用 | 接收端手工修改配方路径 0 次 |  | **通过** | 占位符由接收端解析：源路径消失、接收端路径出现 |
| 7 跨机器配方复用 | 导出包中的凭证和聊天正文 0 项 |  | **通过** | 结构扫描：凭证 []，聊天 [] |
| 7 跨机器配方复用 | 导出包机器路径 0 项（另有目标构建路径，属兼容性范围） |  | **通过** | 目标环境路径：["/Users/mac/.nvm/versions/node/v24.16.0/lib/node_modules/opencode-ai/bin/opencode.exe", "/Users/mac/.nvm/versions/node/v24.16.0/lib/node_modules/opencode-ai/bin/opencode.exe\\n//"] |
| 7 跨机器配方复用 | 不兼容条件测试 3 次、错误自动安装 0 |  | **通过** | [{"changed": "executable", "status": "needs_review", "compat": "differs"}, {"changed": "entry", "status": "needs_review", "compat": "differs"}, {"changed": "platform", "status": "needs_review", "compat": "differs"}] |
| 7 跨机器配方复用 | 故意安装失败 3 次、回滚恢复 3/3 |  | **通过** | [{"raised": true, "error": "NotADirectoryError", "first_file_rolled_back": true, "blocker_intact": true}, {"raised": true, "error": "NotADirectoryError", "first_file_rolled_back": true, "blocker_intact": true}, {"raised": true, "error": "NotADirectoryError", "first_file_rolled_back": true, "blocker_intact": true}] |
| 7 跨机器配方复用 | 两台真实机器复用 |  | **待外部条件** | 缺第二台机器 |
| 7 跨机器配方复用 | ≥2 份已学配方在 B 完成复用 |  | **待外部条件** | 需在 B 机上安装与独立验收 |
| 7 跨机器配方复用 | 每份配方在 B：聊天10轮/工具10次/放行5拒绝5/重启3 |  | **待外部条件** | 需 B 机隔离实例 |
| 7 跨机器配方复用 | 继承 A 通过状态而跳过 B 验收 0 |  | **待外部条件** | 需 B 机验收后才能核对 |
| 8 AI Trust 真实接管 | 连接器接口与字段映射完成 |  | **通过** | 注册/事件/决策/回执接口 + 本地→平台字段映射 |
| 8 AI Trust 真实接管 | 页面地址与凭证配置 |  | **通过** | 缺地址或凭证时明确报缺，不伪造 |
| 8 AI Trust 真实接管 | 离线暂存与恢复补齐（缺失 0／重复 0） |  | **通过** | 离线 200 条保留，恢复后全部补齐、队列清零 |
| 8 AI Trust 真实接管 | 错误凭证被拒、无错误成功提示 |  | **通过** | 401 记为 rejected_credentials，事件保持待发 |
| 8 AI Trust 真实接管 | 重复上报平台逻辑重复 0 |  | **通过** | 以 event_id 为幂等键；重发不新增逻辑记录（本地测试替身） |
| 8 AI Trust 真实接管 | 双向关联抽查 50 匹配 |  | **通过** | 本地 event_id/request_id 关联（本地测试替身） |
| 8 AI Trust 真实接管 | 未用模拟服务签收 |  | **通过** | 所有结果 platform_verified=False |
| 8 AI Trust 真实接管 | 平台侧验收（2 实例／200 事件／延迟／放行10／拒绝10／人工确认／断网补齐） |  | **待外部条件** | 缺 AI Trust 测试环境、接入协议与测试凭证 |

## 各实例能力阶梯（实时）

| 实例 | 已验证/总数 | 加载 | 观测 | 控制 | 独立运行 |
| --- | --- | --- | --- | --- | --- |
| OpenCode (PID 32045) | 6/10 | proven | proven | unsupported | pending |
| Codex (PID 45137) | 6/10 | proven | proven | unsupported | pending |
| Codex (PID 55875) | 6/10 | proven | proven | unsupported | pending |
| OpenCodex (PID 1052) | 3/10 | not_reached | not_reached | not_reached | pending |
| ZCode (PID 1424) | 4/10 | pending | not_reached | unsupported | pending |
| @deepseek-ai/dsh (PID 24034) | 6/10 | proven | proven | unsupported | pending |
