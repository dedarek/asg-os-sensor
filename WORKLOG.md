# 本轮：状态真实性

基线：41a53b5；仅修复状态语义及独立验收页面，不推进匹配分档/版本演进。

## 实施前字段 → 来源 → 缺失含义

- investigation：本进程 INVESTIGATION_RESULTS、运行集合及 ASG_AUTONOMOUS_ANALYSIS；缺失=未调度，禁用优先。没有持久化调查成功证明时，指纹不能补成成功。
- assets（模型、配置、MCP、Skill、规则、网络）：当前没有绑定实例的采集完成凭据；缺失=尚未采集。历史 hook_recipe 字段只作历史建议，不代表当前实例采集。共用序列化支持有证据的 collected/failed/unsupported，成功且空才为未发现。
- hook：本阶段没有安装/生效验证器；固定 not_installed，Sink 未接入／未验证，不能从指纹推断。
- associated_processes：扫描 ownership 的 process_pids；与执行事件分离。执行事件无采集器=尚未采集。
- host_platform：本地 bundle 元数据优先；没有本地宿主证据=未知。配方宿主仅历史建议。
- matched：matcher.match 的旧式布尔结果；仅指纹匹配，不宣称 exact 或兼容验证。
- network：无绑定实例的完整观测证据=尚未采集，不能推断无连接或安全。

接手时存在未跟踪 e2e/artifacts/analyst_tool_calls.jsonl 和 evidence/；保留原文件，精确忽略。初始 8080 未监听；生产库 SHA256 记录于隔离运行 baseline.json。

## 完成与验证

- 新增 runtime/status.py：页面与 API 共用证据状态；禁用优先于残留运行态，固定未安装 Hook。当前调度器无持久排队实现，因此不虚构 queued。
- 页面按 PID+创建时间保留实例，不再借用同名实例配方/调查状态；关联 PID 使用扫描归属。宿主优先本地应用元数据。
- 当前资产采集器未建立实例证据契约，旧配方单独保留 historical_recipe，不把其中模型、配置、网络数据当当前事实。前端不展示完整启动参数。
- prior 损坏/读取异常传播为 MCP 错误；指纹 API 读取失败返回 500。子进程 MCP 测试的审计/evidence/recipe 均落 TemporaryDirectory。
- 测试：`python3 -B -m unittest test_discovery test_matcher_stage1 test_status_stage1`：33 通过。原有 discovery 8；前轮 matcher 17（本轮仅改子进程隔离辅助函数）；本轮 status 8。
- 新增 8 项中：状态/视图契约单测 5，模拟扫描集成 1，本地 HTTP 集成 1，真实 MCP 子进程 1。没有模拟模型，没有真实 Goose 调查验收。
- 真实运行：8081 四个本机扫描实例，全部 investigation=disabled、Hook=not_installed；手动 POST 503；浏览器确认禁用按钮及资产“尚未采集”。截图和脱敏计数报告在 artifacts/stage1/truth-review/。
- 生产库 SHA256 前后一致；8080 前后均无监听，未启动/停止/替换 8080。
- 保留演示服务 PID 47562，8081。没有安装 Hook、跳过 TLS 或外发调查数据。本轮停止在 review。

## 追加：观测撤销链与看板 HTTP 边界

基线 `cd47ea2`。新增实现保留 manifest 无效/卸载后的已知 PID+create_time，API 和页面显示 `revoked`/`healthy=false`，不把指纹、配方或历史事件解释成 Hook 已安装或已生效。看板适配器拒绝重定向、限制 256 KiB 响应，并把非法 `events.valid/invalid` 降级为 `degraded`。

原有测试与新增测试分开记录：原有 71 项回归继续通过；新增 `test_dashboard_http.py` 3 项覆盖隔离安装卸载撤销、重定向拒绝、异常计数降级，`test_observe_page.py` 增补损坏 prior 的撤销绑定断言。完整结果为 74 项通过（17.906s），未进行真实 Goose 外发、真实 active 插件卸载、Hook 安装或 8080 操作。

最终隔离运行：看板 `http://127.0.0.1:8081/`（PID 24825），观测接收器 `http://127.0.0.1:52708`（PID 24804）；运行目录 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-1kJT3b`，日志位于该目录的 `server.log`，详细摘要位于该目录的 `verification.json`。8080 无监听，生产库哈希保持 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`。

部署核对路径：manifest `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json` 为 active，插件位于其同级 `asg-observe.js`，run 事件文件为 `runs/660ad5f492e7ab91/events.jsonl`。`ASG_AUTONOMOUS_ANALYSIS=0` 仅作用于本次隔离看板进程；恢复时将该进程环境改为 `1`，不修改全局配置且不添加 `ASG_ALLOW_INSECURE_ANALYST`。

## 2026-09-10 受控 onboarding 纵向切片

基线为 `f8af00e`。本轮把已有发现实例接入一条窄的状态链：`exact` 只读匹配后复用历史配方；`similar/miss` 生成 Goose 调查所需计划；通过固定的 `opencode-workspace-plugin`、project 作用域、隔离 workspace 和双重授权环境变量后，才可调用已有 `ghost_install`；安装结果再由绑定 PID+create_time 的 `hook.loaded` 事件确认，最后写入隔离 `experience.json`。默认不安装，未验证的配方不改变 `hook_state`。

配方来源已显式区分：`matcher.remember_verified` 在指纹条目和 revision 元数据写入 `recipe_source=goose`；没有来源标记的旧配方视为 `manual/legacy`，即使字段形状相似也不能自动复用或安装。安装器只接受登记的插件名和 project 作用域，重复执行同一 active manifest 且 hash 一致时返回 `already_installed`，不会再次覆盖插件。

### 字段 → 来源 → 缺失含义（onboarding）

- `onboarding.plan`：当前扫描结构、`matcher.classify` 结果或已持久化的调查配方；缺失调查结果时是 `investigation_required`，精确命中仍只代表匹配，不代表安装。
- `onboarding.install`：`runtime.opencode.ghost_install` 的事务结果；未获 `ASG_ONBOARDING_AUTHORIZED=1`、`ASG_ONBOARDING_AUTO_INSTALL=1`、`ASG_ONBOARDING_SCOPE=project` 和对应 workspace 授权时是 `pending_authorization`。
- `onboarding.verification`：`EventVerifier` 对 manifest、nonce、PID+create_time 和事件的结果；没有 `hook.loaded` 是 `pending_restart`，事件错误是 `verification_failed`，阻断能力固定为 `unsupported`。
- `experience.json`：跨进程锁内读改写、fsync 后原子替换的调查/安装/验证转移记录；不存在时惰性初始化，损坏或读取失败抛出错误并保留原文件。
- 配方中的模型、MCP、Skill、规则和网络字段仍由现有实例采集凭据决定；没有采集凭据保持 `not_collected`，不会从配方空值推断“未发现”。

### 原有测试与新增测试

- 原有测试：前轮 74 项状态真实性、观测 HTTP、插件事务、matcher、发现和隔离 prior 回归继续通过。
- 新增测试：`test_onboarding.py` 5 项，覆盖 exact 复用不调用 Goose、legacy/manual 来源拒绝、similar/unsupported 不安装、真实 Node 插件事件链与幂等安装、损坏经验库显式报错；`test_dashboard_http.py` 新增 1 项，覆盖 onboarding API 的计划读取和无授权不安装。
- 最终全量命令：`ASG_TEST_NODE=/Users/mac/.nvm/versions/node/v24.16.0/bin/node python3 -B -m unittest test_discovery test_matcher_stage1 test_status_stage1 test_goose_stage1 test_adapter_stage1 test_synthetic_hook_integration test_opencode_plugin test_real_cli test_observe_page test_dashboard_http test_onboarding`。新增来源门禁后需再执行并记录最终计数。
- 测试类型：单元/HTTP 集成为真实本地代码路径；Node 用例运行仓库真实插件和真实事件校验器，但配方来源是 `goose-simulated`，不冒充真实 Goose；本轮没有真实 Goose 外发验收。

### 隔离运行准备

代码提交后只重启本任务自己的 8081 看板，使其加载本轮代码；沿用独立观测接收器和已存在的隔离 OpenCode workspace。计划使用新的 `artifacts/stage1/dashboard-review-<run-id>/` 运行目录、指纹库、事件目录和日志，不删除旧产物。验收页面、manifest、插件和事件文件的最终绝对路径在提交后补录；8080 与真实 active Agent 保持不动。
