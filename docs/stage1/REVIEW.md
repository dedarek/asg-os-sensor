# Stage1 REVIEW（唯一最新 review 入口：docs/stage1/）

根目录 PLAN.md / WORKLOG.md / REVIEW.md 仅存历史轮次概览；最新进度、证据与结论
一律以本目录为准。

## 当前提交与工作树
- 本轮基线 `cd47ea2`，最终提交见本次独立提交（分支 work/discovery-goose-fingerprint，基线 f4194d3）；
  `docs/stage1/ADVISOR_REAL_HOOK_HANDOFF.md` 仍是未跟踪的顾问交接文件，不能声明工作树干净。
- 原多 Agent 看板运行在 `127.0.0.1:8081`；单实例观测接收器独立运行在随机回环端口。
  8080 未触碰，生产指纹库未修改。
- 真实事件已验证 3 条（见 WORKLOG），active 插件保持不动。

## 本轮状态真实性修复

- 调查、资产、Hook 三个维度独立投影：调查禁用不再显示解析中；没有采集凭据的资产为尚未采集；指纹或配方不能升级 Hook 状态，Hook 仍为未安装/未验证。
- 观测服务错误和卸载统一返回 `revoked`、`healthy=false` 及已知 PID/create_time；看板 API 使用同一绑定结果。父 PID 4970 不继承子引擎 PID 5297 的成功证据。
- 看板适配拒绝重定向、限制响应体 256 KiB，并将非法事件计数降级为 `degraded`，不因单个适配异常中止全局扫描。
- 真实隔离 CLI install→uninstall→HTTP observer→dashboard/API 回归已覆盖；活动插件未卸载。

## 真实验收（2026-09-10，非模拟）
- 用户已打开隔离工作区并执行一次目录列举任务；hook.loaded 与 tool.execute
  before/after（tool=read，同 call_id）均被接收端校验通过（PID 5297 绑定）。
- 证据归档：artifacts/stage1/evidence/real-hook-2026-09-10/（脱敏，不含 nonce）。
- 观测接收器 `/page` 位于独立回环随机端口；它区分历史加载/当前新鲜度/观测 vs 阻断，
  页面与 JSON 均不公开 nonce，只描述绑定实例。
- 8081 的根页面恢复为原 `monitor_dashboard.py` 多 Agent 看板；`/api/state` 保留
  多 Agent 列表和扫描状态。看板通过 `ASG_OBSERVE_URL` 读取观测契约；当前证据绑定
  5297，而扫描卡片主 PID 4970，因此 4970 及其他 Agent 不显示已观测成功。
- 当前隔离演示：看板 `http://127.0.0.1:8081/`（PID 24825），观测接收器
  `http://127.0.0.1:52708`（PID 24804）；运行记录与停止方式见 WORKLOG，摘要在
  `artifacts/stage1/dashboard-review-1kJT3b/verification.json`。
- active 部署核对：manifest `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json`、插件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/asg-observe.js` 和 3 行事件文件均存在；真实引擎 PID 5297/create_time `1789006943.640438` 在线。

## 待办（受控卸载验收）
- 未执行卸载；步骤已写入 WORKLOG。等待顾问协调用户再次只读调用后执行。
## 全量测试（本轮实际执行一次）
- 命令: `ASG_TEST_NODE=/Users/mac/.nvm/versions/node/v24.16.0/bin/node python3 -B -m unittest
  test_discovery test_matcher_stage1 test_status_stage1 test_goose_stage1
  test_adapter_stage1 test_synthetic_hook_integration test_opencode_plugin test_real_cli
  test_observe_page test_dashboard_http`
- 结果: `Ran 74 tests in 17.906s`，`OK`。其中 `test_real_cli` 3 项需 node，其余 71 项
  无需 node；本轮新增 `test_dashboard_http.py` 3 项，并补充损坏 prior 断言。输出含现有 LibreSSL/urllib3
  与 ResourceWarning，未影响测试结果。

## 已改实现（本分支全部提交）
- 生命周期 instance_id 贯穿 running/retry/result/API；_record_investigation_result 不再
  结束时重读 psutil（PID 复用不串结果）。
- matcher: classify exact 带 bounds；remember_verified 演进门禁（可执行 digest+入口身份，
  同 shell 不同 app 不合并、原生升级同族演进）；analyzer 移除产品名单。
- recipe_validation: 证据绑定冻结实例+PID+create_time、真实非占位结果、观测工具白名单；
  hook_evidence_supported 恒 False（proposed/unverified），未知接入点不标已证实。
- collection: 局部失败保留成功项；limitations 区分本机读取与脱敏外发。
- ghost_install/server/plugin: 事务化安装（run 目录+manifest 原子发布+回滚）、路径组件
  symlink 拒绝、O_EXCL 原子创建、manifest+hash 校验卸载拒绝删除；插件加载冻结 runid,
  emit 核对同 run 且 active；server 每请求动态 manifest（运行中卸载即 503），/events 投影
  不公开 nonce，读取端对 state/events 路径做 symlink 拒检；load_manifest 严格 schema
  （active 布尔 True、runid 纯安全、nonce 非空、workspace 恒等于绑定路径）。

## 真实证据（A 阶段，只读调查）
- 引擎: OpenCode 桌面包 1.18.25, Electron main..utilityProcess.fork(sidecar.js)..
  chunks/node-Cuna2N2U.js Server（引擎运行在 NodeService；NodeService 参数只能证明
  Electron Node 服务，不能自动证明就是 Agent 引擎——未知保持未知）。
- 配置链: globalConfigFile 搜 opencode.jsonc/opencode.json/config.json；OPENCODE_CONFIG_DIR
  与 OPENCODE_CONFIG= 为官方隔离入口。CLAUDE.md 存在(379B)但未观测到打开，不推断已加载/未加载。
- 插件机制: engine bundle 含 tool.execute.before/after、plugin 加载 Glob.scan(
  开启 plugin/plugins 目录)；桌面实例是否加载插件需在 UI 打开工作区后实测。
- 部署目录 `artifacts/stage1/opencode-observe` 已安装并由用户真实打开过观测插件；
  当前 active manifest 与历史事件保留。nonce 不进入页面、API 或脱敏证据。

## 已知限制与未完成项
- 真实 OpenCode 观测插件已在隔离工作区加载并捕获一次工具调用；未执行受控卸载验收，
  active manifest、插件和引擎保持不动，等待顾问协调用户再次做无敏感目录调用。
- 插件文件删除不等于模块已从引擎内存卸载：未验证运行中卸载后不再产事件即不得宣称
  完全回滚已被证明；回滚应使用已校验 manifest+hash 的 uninstall 命令并保留 runs 历史，
  不得要求用户手动删除 .asg-observe 状态目录。
- 退出工作区不保证退出独立引擎（Electron utility 可能被共享）：无法证实则明确未知，
  不要求用户终止共享进程。
- Windows msvcrt 锁分支未本机实测（macOS）；真实跨版本目标复用未复跑。
- 接入点证据映射、Hook 安装器真实生效验证与回滚为下一阶段；本阶段未完成 Hook 安装。

## 当前 review 点
- 本轮是看板功能回归与状态分层修复，不是 Stage1 闭环完成。
- 受控卸载验收仍未执行；下一步需顾问协调用户再次做无敏感目录调用，之后验证调用成功、
  旧 run 无新增事件、接收器即时 revoked 三项同时成立。
- Hook 安装、生效验证、阻断算法、完整资产采集和其他 Agent 支持仍未完成，不能进入下一阶段。

## 受控 onboarding 纵向切片（基线 f8af00e）

本轮新增 onboarding 协调层和 dashboard API，使计划、安装、激活和经验记录共用实例身份与状态语义。固定适配器只允许隔离 project workspace-plugin；安装仍需明确授权，事件验证成功也只表示观测 Hook 已收到绑定事件，阻断能力保持 unsupported。旧无来源配方显示 manual/legacy，不计为 Goose 从零调查成功；新保存的指纹条目/revision 带有 `recipe_source=goose` 元数据。

| 验收项 | 结果 | 证据边界 |
| --- | --- | --- |
| 行为发现、随机命名、实例归属 | 部分真实 | 本机真实扫描与既有回归；跨 Agent 盲测及误报/漏检统计未完成 |
| 自动 Goose 调查 | 未完成/未验证 | 调度入口已接通；本轮调查禁用，无真实模型外发 |
| MCP/Skill/规则/网络采集 | 未完成 | API/UI 保持尚未采集，不把空值当未发现 |
| 指纹学习与 exact 复用 | 模拟/单测验证 | 5 项 onboarding 回归覆盖；未用新真实 Agent 验证跨启动复用 |
| 授权安装计划与执行 | 隔离真实路径 | 临时目录使用真实 ghost_install；未触碰现用工作区 |
| 下次启动激活 | 模拟事件验证 | Node 加载真实插件并产生 `hook.loaded`；不等于真实 OpenCode 下次启动 |
| 真实事件/API | 既有真实证据 + 新 API 模拟 | 隔离 OpenCode 已有 3 条绑定事件；新 onboarding API 在隔离 HTTP 测试验证 |
| 撤销/回滚 | 隔离真实路径 | 既有 install→uninstall→revoked 回归；未卸载当前 active 插件 |
| 阻断/平台授权/平台注册 | 未实现 | 后续范围 |

本轮是状态真实性修复和最小受控 onboarding 切片，不是 Stage1 闭环完成；通用主流 Agent 接入、真实 Goose 配方、真实目标安装和 Hook 生效验收仍未完成。完成后停在 review，不合并、不推送、不部署 8080。
