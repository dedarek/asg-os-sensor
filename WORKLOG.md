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
- 最终全量命令：`ASG_TEST_NODE=/Users/mac/.nvm/versions/node/v24.16.0/bin/node python3 -B -m unittest test_discovery test_matcher_stage1 test_status_stage1 test_goose_stage1 test_adapter_stage1 test_synthetic_hook_integration test_opencode_plugin test_real_cli test_observe_page test_dashboard_http test_onboarding`；结果 `Ran 80 tests in 18.302s`，`OK`。
- 测试类型：单元/HTTP 集成为真实本地代码路径；Node 用例运行仓库真实插件和真实事件校验器，但配方来源是 `goose-simulated`，不冒充真实 Goose；本轮没有真实 Goose 外发验收。

### 隔离运行准备

代码提交后只重启本任务自己的 8081 看板，使其加载本轮代码；沿用独立观测接收器和已存在的隔离 OpenCode workspace。计划使用新的 `artifacts/stage1/dashboard-review-<run-id>/` 运行目录、指纹库、事件目录和日志，不删除旧产物。验收页面、manifest、插件和事件文件的最终绝对路径在提交后补录；8080 与真实 active Agent 保持不动。

## 最终隔离验收运行（commit 7dec303）

- 看板：`http://127.0.0.1:8081/`，PID `33217`；运行目录 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-hPAgMHZb`；日志 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-hPAgMHZb/server.log`；核对摘要 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-hPAgMHZb/verification.json`。
- 看板仅使用隔离指纹库 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/authorized-goose/fingerprints.json`，`ASG_AUTONOMOUS_ANALYSIS=0`，未设置授权自动安装变量；页面与 `/api/state` 的 investigation 均为 `disabled`，Hook 为 `not_installed`。
- 观测接收器：`http://127.0.0.1:52708`，PID `24804`；接收器工作区 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe`。manifest `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json` 为 active；插件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/asg-observe.js`；事件文件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`，3 行，`valid=3/invalid=0`，类型为 `hook.loaded`、`tool.execute.before`、`tool.execute.after`。
- 真实接收器当前返回 `stale/healthy=false`，原因是最近事件超过 TTL；这不被页面解释为当前 Hook 生效。插件 manifest 仍 active，真实 Agent PID `5297` 和 create_time `1789006943.640438` 未动。
- 保护核对：8080 无监听；生产库 `/Users/mac/个人项目/asg-os-sensor-stage1/runtime/fingerprints.json` SHA256 为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`，与既有记录一致。未改全局配置，未重启或停止现用 Agent。
- 停止方式：先确认 PID `33217` 的命令仍为本工作树 `monitor_dashboard.py`，再只对该 PID 发送 TERM；观测接收器如需停止只处理 PID `24804`。不按进程名批量终止，不手动删除上述证据目录。

## 2026-09-10 review follow-up：受控 onboarding 反馈闭环

实现提交：`a90e0c8 feat(stage1): close onboarding feedback loop`，基于 `8dfe6a0`。本节追加记录本轮实现和最新验收；前文旧 PID、目录和测试数字保留为历史运行记录。

### 字段 → 来源 → 缺失含义

| 维度/字段 | 来源 | 缺失或异常含义 |
| --- | --- | --- |
| 调查 | `ASG_AUTONOMOUS_ANALYSIS`、内存调度记录和冻结的 PID+create_time 结果 | 关闭=`disabled`；无持久队列=`not_scheduled`；实际运行=`running`；真实结果才可为 `succeeded`/`failed` |
| prior 经验 | 隔离 `experience.json` 的锁内读写；MCP 只读当前实例生命周期摘要 | 文件不存在=空历史；损坏/读取失败=显式错误，不转换为空；不返回配方、路径、凭据或 nonce |
| 资产 | 当前实例采集器带来源的结果 | 无采集凭据=`not_collected`；成功且空才可显示“未发现”；失败=`failed`；不支持=`unsupported` |
| 关联进程 | 扫描器已有 ownership 的 `process_pids` | 只表示进程归属；无执行事件不推断没有子进程 |
| 执行事件 | 观测接收器或当前实例执行采集器 | 未接入采集=`not_collected`，与关联进程区域分开 |
| 网络 | 当前实例网络观测凭据 | 无数据=`not_collected`，不推断无连接或安全 |
| 宿主类型 | 已获取的 bundle/本地应用证据 | 没有本地证据=`未知`，不默认 CLI |
| 指纹/配方 | matcher 的 exact/similar/miss 和结构校验 | 命中只表示匹配；结构校验通过只表示可保存，均不表示调查完成或 Hook 生效 |
| Hook/Sink | ghost_install manifest、EventVerifier 和后续事件 | 看板当前无实际安装=`未安装`；Sink=`未接入／未验证`；`hook.loaded` 仅为加载证据，需工具事件才是观测验证 |

### 实现和安全边界

- `runtime/onboarding.py` 注册唯一已验证的 `opencode-workspace-plugin` backend；计划、安装、加载、工具观测和撤销分别表达。精确命中复用同一安装/验证入口，新 PID+create_time 重新绑定；旧 run 事件不能替代新实例的加载握手。
- `runtime/analyst_tools.py` 脚本启动显式加入仓库根，`get_prior_experience` 通过子进程使用继承的隔离配置。prior 读错误由 MCP 返回错误；窄投影不暴露路径、配方、凭据或 nonce。`matcher.remember_verified` 使用 supervisor 参数写入 `recipe_source`，不采信模型自行声称的 provenance。
- `verify_activation` 先核对原安装 runid 和 active manifest，再分别返回 `loaded_verified`、`events_verified` 或 `revoked`；加载本身不会计入 `hook_verified`。阻断能力仍为 `unsupported`。
- `monitor_dashboard.py` 的 exact 扫描路径调用统一的授权、幂等安装和验证 helper；失败/禁用/繁忙结果写入经验历史，供后续差异调查识别失败先验。模拟来源只有测试环境 `ASG_TEST_SIMULATED=1` 才被接受。

### 测试分类与结果

- 原有测试：前轮 80 项 matcher、发现、状态真实性、观测 HTTP、插件事务和 onboarding 回归继续通过。
- 新增/本轮扩展测试：真实 MCP 子进程读取隔离失败历史并验证验证状态窄投影；exact 扫描调用授权 onboarding executor；同一 workspace 的第二个 PID+create_time 重新绑定；撤销 manifest 不会由旧事件升级为 Hook 验证。目标集合 `test_onboarding test_dashboard_http` 共 14 项通过。
- 全量命令：`ASG_TEST_NODE=/Users/mac/.nvm/versions/node/v24.16.0/bin/node python3 -B -m unittest test_discovery test_matcher_stage1 test_status_stage1 test_goose_stage1 test_adapter_stage1 test_synthetic_hook_integration test_opencode_plugin test_real_cli test_observe_page test_dashboard_http test_onboarding`；结果 `Ran 83 tests in 19.980s`，`OK`。仅有既存 LibreSSL/urllib3 和 ResourceWarning 输出。
- 单元/本地集成：上述全量回归；模拟集成：Node 子进程加载仓库真实插件并产生 `hook.loaded` 与工具事件，但配方来源为 `goose-simulated`，不冒充 Goose。
- 真实 Goose：受控随机目标记录于 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-KouKJi6H/real_goose_result.json`，状态 `blocked_before_model_request`，原因是配置的 `ASG_ANALYST_API_KEY` 缺失，`external_request_sent=false`；因此没有真实 Goose 成功调查。

### 最新隔离验收运行

- 页面：`http://127.0.0.1:8081/`；看板 PID `38258`；工作目录 `/Users/mac/个人项目/asg-os-sensor-stage1`；运行目录 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-jcA6qPaU`；日志 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-jcA6qPaU/server.log`；脱敏核对摘要 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-jcA6qPaU/verification.json`。
- 看板隔离配置：`ASG_PORT=8081`、`ASG_AUTONOMOUS_ANALYSIS=0`、`ASG_FINGERPRINT_DB`、`ASG_RUN_DIR`、`ASG_EVENT_DIR`、`ASG_AUDIT_DIR`、`ASG_RECIPE_DIR`、`ASG_OBSERVE_URL=http://127.0.0.1:52708`；未设置真实调查凭据、不安全 TLS 覆盖或自动安装授权。当前页面/API 为 3 个真实本机实例，调查=`disabled`，资产/网络=`not_collected`，Hook=`not_installed`。
- 插件接收器：`http://127.0.0.1:52708`，PID `24804`；隔离 workspace `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe`；manifest `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json`，active，runid `660ad5f492e7ab91`；插件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/asg-observe.js`；事件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`，3 条有效、0 条无效，类型为 `hook.loaded`、`tool.execute.before`、`tool.execute.after`。接收器当前 `stale/healthy=false`，因为最新事件超过 TTL，页面没有把它显示为当前生效。
- 保护核对：8080 无监听；生产库 `/Users/mac/个人项目/asg-os-sensor-stage1/runtime/fingerprints.json` SHA256 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`；现用 Agent 的绑定实例为 PID `5297`、create_time `1789006943.640438`，未重启、未卸载、未写入其工作区。

### 启动与停止

每次启动先在 worktree 下创建新的 `artifacts/stage1/dashboard-review-<run-id>/`，使用上述 `ASG_*` 隔离变量运行 `python3 -u -B monitor_dashboard.py`；不复用旧运行目录。当前只保留看板 PID `38258` 和接收器 PID `24804`。停止前先用进程 PID 和工作目录核对归属，再只对对应 PID 发送 TERM；不按进程名批量终止。原始运行产物继续由 `.gitignore` 忽略。

本轮是状态真实性修复和受控 onboarding 反馈闭环，不是 Stage1 闭环完成；真实 Goose 成功调查、通用 Agent backend、MCP/Skill/规则/网络实例采集、真实目标安装、Hook 生效及阻断仍未完成。停在 review，不合并、不推送、不部署 8080、不进入下一阶段。

## 2026-09-10 真实 Goose 复核：配置加载、调查与 prior 回读

本节更新此前“真实 Goose 未完成”的历史记录。新增安全修复提交：`055adca fix(stage1): hide credential suffixes from logs`；真实调查使用工作树当时的实现提交 `a90e0c8`，掩码修复随后提交。主仓库 `.env` 只被 `dotenv_values` 读入本次 Python 进程内存，没有复制到 worktree、命令行、报告或日志；预检仅输出路由、`key_env` 和 `key_present`。

### 实际结果

- 预检：`route=custom-openai`、`key_env=ASG_ANALYST_API_KEY`、`key_present=True`、模型 `qwen38-27b`；TLS 兼容仅对本次指定网关授权。
- 随机目标：PID `40956`、create_time `1789016070.516786`，脚本仅监听 `127.0.0.1`，启动参数不含产品名；调查结束后由驱动脚本终止。
- 第一轮真实 Goose：`succeeded`，生成候选配方并通过结构/证据门禁；第二轮使用相同 PID+create_time 强制重测，也为 `succeeded`。两轮都没有安装 Hook，自动安装为关闭。
- 中间真实 MCP：独立子进程、去除 `PYTHONPATH`，通过 `get_prior_experience` 读取同一隔离经验库，返回 `returncode=0`；结果确认有 prior 配方历史、实例 ID 存在、没有暴露 run 路径。第二轮的 `get_target_context` 证据显示 `prior_experience.recent` 为 1 条，证明 Goose 实际收到上一轮经验。
- Goose 对该通用 Python 目标提出了身份 `Python`，接入方式为 `unsupported`；因此候选配方可以记录调查结论，但没有被误报为可安装接入方案。

### 证据路径

- 总结：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/real_goose_result.json`
- 预检：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/preflight.json`
- prior MCP 摘要：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/prior_mcp_result.json`
- 隔离指纹库：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/fingerprints.json`
- 第一轮候选：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/pid_40956_1789016070/recipes/candidate.json`
- 第二轮候选：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/pid_40956_1789016168/recipes/candidate.json`
- 第二轮 Goose 的 `get_target_context` 证据位于同一第二轮目录的 `evidence/`，摘要记录 `prior_recent_count=1`。

### 测试与保护核对

- 掩码修复后全量命令重新执行，结果 `Ran 84 tests in 22.612s`，`OK`；其中新增回归确认密钥尾号不会进入掩码文本。目标 onboarding/dashboard 集合仍为 14 项通过。
- 本次真实调查 run 的 `fingerprints.json` 和 `experience.json` 只在 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/`；没有写入生产指纹库。生产库 `/Users/mac/个人项目/asg-os-sensor-stage1/runtime/fingerprints.json` SHA256 仍为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`。
- 8081 看板仍为 `http://127.0.0.1:8081/`、PID `38258`；观测接收器仍为 `http://127.0.0.1:52708`、PID `24804`。8080 无监听，现用 Agent PID `5297` 未重启或触碰。

这两轮是受控真实调查和 prior 传递验收，不是 Stage1 闭环完成；强制第二轮用于验证 prior 链路，不替代后续 exact 命中去重、版本演进和真实 Hook 生效验收。

## 2026-09-10 review milestone：兼容历史、观测证据与真实 OpenCode 调查

### 字段 → 来源 → 缺失含义

| 字段 | 来源 | 缺失或异常含义 |
| --- | --- | --- |
| 运行时兼容 prior | `runtime.analyzer` 的 `compatibility`，包含 executable/entry/build、平台、运行时和 launch 条件 | 没有兼容快照时不跨实例复用；旧记录只保留同实例 legacy 可见性 |
| prior 匹配方式 | `runtime.onboarding.load_prior_experience` | `compatibility` 表示可跨 PID 复用的历史参考；`instance_id_legacy` 只兼容旧格式；`none` 表示没有历史 |
| 观测健康 | `runtime.analyst_tools.inspect_observation` 读取配置的 `127.0.0.1` 接收器 | `not_configured`/`unbound`/`mismatch` 或读取错误不表示 Hook 生效；`stale` 只表示历史事件过期 |
| 观测事件 | 接收器 `/events` 的绑定 PID+create_time 投影 | `valid=0` 是没有观测事件，不能推断没有子进程、没有连接或安全 |
| Goose 候选配方 | Goose `propose_recipe` 输出，经 `recipe_validation` 和 Supervisor 质量门禁保存 | 候选配方、结构校验和指纹命中均不表示安装、当前加载或阻断生效 |
| prior recipe/fingerprint 库 | 子进程继承的 `ASG_RECIPE_DIR`、`ASG_FINGERPRINT_DB`；脚本启动显式加入仓库根 | 显式隔离配置下只读隔离路径；导入、读取或 JSON 损坏直接报错，不回退默认库 |

### 本次实施

- `runtime/onboarding.py` 在调查生命周期记录中保存脱敏兼容快照；prior 查询优先按完整兼容条件跨实例匹配，同时保留 revision/source 摘要，不返回私有路径、配方原文或 nonce。入口、构建或 launch 改变时不会命中该历史。
- `runtime/analyst_tools.py` 增加只读 `inspect_observation` 工具，拒绝非回环地址、重定向和超大响应，并把接收器健康与事件类型放入 `get_target_context`。脚本方式启动显式加入项目根；显式指纹库与 recipe 目录发生错误时向 MCP 返回错误，禁止静默读默认库。
- `recipes/runtime_analyst.yaml` 明确要求 Goose 把观测作为证据而不是安装或阻断证明。没有向候选配方注入 Hook 字段；本次 OpenCode 配方中的 workspace-plugin 选择来自 Goose 读取的真实 bundle/观测证据，并由程序校验后保存。

### 测试分类与结果

- 原有回归：前轮 84 项 matcher、发现、状态/API、插件事务和 onboarding 测试保持通过；现有 MCP 子进程用例改为真实 native `sleep` 目标，验证跨进程按兼容条件读 prior。
- 新增/扩展回归：`test_onboarding.py` 增加新 PID 兼容历史命中与构建变化拒绝；`test_matcher_stage1.py` 增加显式隔离指纹库不读取默认 `committed.json`。针对性 3 项通过；最终全量 `86` 项通过。
- 模拟集成：既有 Node 子进程加载仓库真实观测插件并通过事件校验器，来源仍为 `goose-simulated`，不计为真实 Goose 或真实 OpenCode 闭环。
- 真实运行：`real-goose-opencode-live-8eCFRnwK` 使用现有真实 OpenCode helper PID `5297` 做一次只读调查，Goose 实际调用 `get_target_context` 后保存候选；目标配置、凭据和事件原文没有进入报告，密钥仅在授权进程内存中使用。

### 真实调查与隔离证据

- 总结：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-opencode-live-8eCFRnwK/real_goose_result.json`
- Goose 运行目录：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-opencode-live-8eCFRnwK/pid_5297_1789017233/`
- 候选配方：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-opencode-live-8eCFRnwK/pid_5297_1789017233/recipes/candidate.json`
- 证据目录：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-opencode-live-8eCFRnwK/pid_5297_1789017233/evidence/`
- 隔离指纹库和经验库：上述 run 根目录下的 `fingerprints.json`、`experience.json`。
- Goose 结果为 `succeeded`，身份 `OpenCode`、置信度 `0.65`，候选接入方式为 `opencode-workspace-plugin/workspace-plugin`，`restart_required=unknown`；计划保存为待授权，自动安装为关闭。接收器证据绑定 `5297:1789006943.640438`，事件 `3` 条有效、`0` 条无效；当前健康为 `stale/healthy=false`，不宣称当前生效。

### 现有隔离部署核对与边界

- 观测接收器：`http://127.0.0.1:52708`；工作区 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe`；manifest `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json`；插件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/asg-observe.js`；事件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`。
- 这些部署和 3 条真实事件来自此前已打开的隔离 OpenCode 工作区；本次没有重装、卸载或重启它。Node callback 用例只证明机制集成；真实 OpenCode 的既有事件证明一次加载和工具事件接收，不能替代下一次启动后的完整安装/生效验收。
- 本轮实现已作为当前分支独立提交交付；8081 看板使用新的隔离运行目录运行，8080、全局配置、生产指纹库和现用 Agent 保持不动。

本轮是兼容历史和真实观测证据补强，不是 Stage1 闭环完成；完整 exact/similar/miss、版本演进、跨 Agent 通用后端和 Hook 安装/生效验证仍停在下一 review 点。

## 2026-09-10 review follow-up：加载器作用域证据与隔离验收工作区

### 字段 → 来源 → 缺失含义

| 字段 | 来源 | 缺失或异常含义 |
| --- | --- | --- |
| 加载器规则 | 目标可执行文件对应的 OpenCode `Info.plist`、`app.asar` 有界静态标记 | 只证明实现中存在配置/插件搜索规则，不证明当前目标已加载插件 |
| 配置范围 | 目标打开文件类别、`OPENCODE_CONFIG_DIR` 静态入口和目标实际配置文件 | 未观察到目标配置文件或项目插件路径=`unknown`；CWD 只作上下文 |
| 插件范围 | 目标进程打开文件中是否出现项目 `.opencode/plugins` 路径 | 没有目标插件路径=`unresolved`，不能据此安装或宣称可用 |
| 真实观测 | 回环接收器按 nonce、PID、create_time 校验后的 `/events` 投影 | 无观测字段/无事件不等于没有子进程或没有连接；`stale` 不等于当前生效 |
| 隔离启动 | 独立 `HOME`、XDG 目录、`OPENCODE_CONFIG_DIR`、CWD 和端口的实际启动探测 | 启动成功只证明服务可运行；没有会话/工具请求就没有插件握手或工具事件结论 |

### 本次实施

- `runtime/analyst_tools.py` 新增只读 `inspect_loader_surface`：读取目标 bundle 的名称/版本、配置覆盖入口、候选配置文件名、插件 glob、Hook 事件标记和目标打开文件类别；不读取全局配置内容，不扫描 workspace，不执行安装。`get_target_context` 在盲调查未配置 `ASG_OBSERVE_URL` 时不再添加 observation 字段。
- Goose 配方提示要求桌面/打包运行时先取得 loader 证据；目标 CWD 不再被解释成安装范围；没有目标项目插件路径时可以诚实提出 `unsupported`。候选回滚只保留“未来在已确认作用域使用既有事务卸载器”的描述，不把任意删除文件当成已验证回滚。
- 新增隔离工作区 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance`，事务安装器只在该目录预置插件。manifest、插件和空的本次 run 事件文件均为独立路径；未打开前事件数为 0。

### 真实运行与安全核对

- 真实盲 MCP 子进程证据：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/loader-blind-gpozbzy/verification.json`。OpenCode 1.18.25 的 bundle 规则显示 `OPENCODE_CONFIG_DIR`、`opencode.jsonc/opencode.json/config.json` 和 `{plugin,plugins}/*.{ts,js}`，但目标项目插件路径未观察到，scope=`unresolved`，且没有 observation 字段。
- 真实 Goose 盲调查：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-opencode-blind-oDNlaY9r/real_goose_result.json`。Goose 实际调用 loader 证据后输出 OpenCode、置信度 `0.75`、接入方式 `unsupported`；这证明了“根据实际 loader 证据拒绝无依据安装”，不证明新实例 Hook 已加载。其候选路径为该 run 下 `pid_5297_1789018526/recipes/candidate.json`。
- 独立引擎可行性探测：`opencode serve --hostname 127.0.0.1 --port 52709` 在验收工作区启动成功，进程 PID `52110` 随后已按身份停止；日志和启动摘要在 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/engine-startup/`。探测使用隔离 `HOME`、XDG 五类目录和 `OPENCODE_CONFIG_DIR`，只生成该目录内数据库/缓存/日志；没有发送会话或工具请求，也没有产生新插件事件。
- app bundle 静态证据同时含 `requestSingleInstanceLock`、`--user-data-dir`、全局配置候选及缺失时创建配置文件的实现标记。因此安全启动的最小路径必须显式提供隔离数据根、配置目录、CWD 和端口；直接启动桌面 GUI 仍可能触发单实例/全局路径，不作为本次自动化动作。

### 测试分类与结果

- 原有测试：前轮 86 项回归继续通过。
- 新增/扩展测试：`test_onboarding.py` 的 loader scope 分离、脚本 MCP 隔离 prior 和原生兼容路径纳入全量；本次完整命令结果为 `Ran 87 tests in 22.509s`、`OK`。输出只有既存 LibreSSL/ResourceWarning 警告。
- 模拟集成：Node 插件事件链仍属于 `goose-simulated` 机制测试，不冒充真实 Agent。
- 真实 Goose：盲调查成功返回候选，但接入方式为 `unsupported`；没有真实新实例的工具事件验收。
- 真实终端：现用 OpenCode 仍为只读证据；已有隔离接收器仍有 `3` 条有效、`0` 条无效历史事件，health=`stale/healthy=false`。

### 交接路径与边界

- 新验收工作区：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance`。
- 新工作区插件：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/asg-observe.js`。
- 新工作区 manifest：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/.asg-observe/manifest.json`。
- 新工作区事件文件：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/.asg-observe/runs/b5fd699f4d755aa7/events.jsonl`，用户打开前为 0 行。
- 已有真实接收器仍为 `http://127.0.0.1:52708`，PID `24804`，绑定现用 OpenCode helper `5297:1789006943.640438`；旧部署路径和 3 条事件保存在 `artifacts/stage1/opencode-observe`，与新工作区严格分开。
- 8081 页面仍为 `http://127.0.0.1:8081/`，PID `48028`，摘要 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-vIbGLtFEA/verification.json`；调查为 disabled，Hook 不显示已安装或已验证。
- 保护核对：8080 无监听；生产指纹库 SHA256 仍为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`；现用 Agent、全局配置和旧 active manifest 未重启、未修改、未卸载。新工作区验收摘要为 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/readiness.json`。

本轮是加载器证据、隔离启动可行性和验收工作区准备，不是 Stage1 闭环完成；停止在 review，不进入 Hook 安装推广或防控。

## 2026-09-10 review continuation：路由级 TLS 与当前隔离验收工作区

### 字段 → 来源 → 缺失含义

| 字段 | 来源 | 缺失或异常含义 |
| --- | --- | --- |
| TLS 传输例外 | `llm.yaml` 当前 route 的 `tls.verify`、`transport`，由 `runtime.llm_config` 解析 | 未明确 opt-in 或 route 非 HTTPS=`direct-verified`；旧 `ASG_INSECURE_SSL` 只作为没有显式 route 设置时的兼容回退，不能覆盖显式校验配置 |
| 代理上游范围 | 当前 route 的 `base_url` origin | origin 改变、query/fragment 存在或发生重定向=`拒绝`；代理不接受任意上游 |
| 插件部署 | 隔离 workspace 的 `ghost_install` manifest、插件 SHA256 | active manifest 只表示文件已准备；未打开引擎=`awaiting_user_open`，不表示已加载 |
| 当前事件接收 | 新 workspace 的绑定 PID+create_time、nonce 校验后的 events 文件 | 打开前 0 条=`尚未接收`；历史接收器 stale 不代表当前 Hook 生效 |

### 实施

- `runtime/llm_config.py` 支持任意自定义 route 的显式 `tls.verify=false` + `loopback-proxy`，显式配置优先于兼容环境变量；`ASG_ANALYST_ENV_FILE` 只读取调用方明确选择的环境文件，不搜索其他项目凭据。
- `runtime/llm_proxy.py` 只转发当前 route 的 `/chat/completions` 与 `/responses`，保存并校验 origin，不跟随重定向，不转发跨域 Authorization；没有把供应商或 route 名写入代码白名单。
- `runtime/opencode/asg-observe.js` 使用 OpenCode 1.18.x 可识别的 V1 默认对象导出，同时保留 named export；本地 Node 合同夹具和 onboarding 夹具均兼容函数/对象两种测试入口。

### 原有测试与新增测试（分开记录）

- 原有回归：本轮前已通过的 `87` 项保持通过；本次没有删除或降低原有断言。
- 新增回归：`test_goose_stage1.py` 新增 3 项，覆盖任意自定义 route 的配置驱动 TLS、显式 `verify=true` 覆盖旧环境变量、代理跨 origin 重配置拒绝。
- 测试适配：`test_opencode_plugin.py`、`test_real_cli.py`、`test_onboarding.py` 的既有 Node 夹具改为同时识别 V1 对象导出和旧函数导出，不增加验收项。
- 当前完整回归分组为 `55 + 12 + 23 = 90` 项通过：发现/matcher/status/Goose/adapter/synthetic 55 项；插件事务与真实接收器 API 12 项；页面/API/onboarding 23 项。模拟插件事件明确是本地机制测试，不计为真实 OpenCode 插件加载。

### 真实运行与隔离证据

- 真实路由烟测：`ASG_ANALYST_ENV_FILE=/Users/mac/个人项目/asg-os-sensor/.env python3 -B verify_llm.py`，chat completions 和 responses 均 200；输出只含 route/model、掩码凭据和 loopback transport。日志：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-cli-chain-20260910/verify-llm-route.log`。
- 真实 Goose 工具链：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-cli-chain-20260910/goose-clean5/` 有 5 条成功的真实 MCP 工具调用和对应 evidence；本次有界运行没有生成 candidate recipe，故不宣称调查成功。脱敏摘要：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-cli-chain-20260910/goose-clean5/derived-summary.json`。
- headless CLI 对照：无插件基线 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-cli-baseline-mcw9yjed/result.json` 的 `/path` 通过；带插件对照 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-cli-plugin-prepared-chap12re/result.json` 的全局健康通过，但实例 `/path` 在外部插件初始化阶段超时，未产生 CLI 事件。该对照目录与用户验收 workspace 分离。
- 当前用户验收 workspace：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance`；active manifest runid=`d40626064e384a68`，插件 SHA256=`866504bbc7f25c811e25f1d989909342b056c5445061f85fad6cbbde1b1e3825`，事件文件为该 run 下 `events.jsonl`，打开前 `0` 行。准备摘要已同步到 `readiness.json`。
- 历史真实接收器仍为 `http://127.0.0.1:52708`、PID `24804`，绑定旧隔离 workspace 的实例 `5297:1789006943.640438`，已有 `3` 条有效事件但当前 `stale/healthy=false`；它不被当作新 workspace 已加载证明。

### 保护核对

8081 看板仍为 `http://127.0.0.1:8081/`、PID `48028`，运行目录为 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-vIbGLtFEA`，调查开关为 `ASG_AUTONOMOUS_ANALYSIS=0`，使用隔离指纹库。8080 无监听；生产指纹库 SHA256 仍为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`。未修改全局配置，未重启或触碰现用 Agent，未改变旧 active manifest。

本轮是路由级 TLS 配置收紧、测试合同修复和隔离验收准备，不是 Stage1 闭环完成；当前用户打开 workspace 后的真实新实例事件接收、完整 Goose 候选输出、exact/similar/miss 闭环、revision 演进和真实 Hook 生效仍待 review。


## 2026-09-10 review continuation：通用运行时证据与 Goose 调查生命周期

本轮实现提交 当前 HEAD（本轮独立提交），基线 `246078c`。新增 `runtime/analyst_evidence.py`，从 supervisor 绑定的 PID+create_time 提供原始/解析 executable 和 entry candidates、父子进程、打开文件类别、近旁 manifest 候选、冲突与不确定性；`find_related_files` 和 `read_related_file` 只允许进程派生根目录，文件名、大小、深度和读取字节数有界，隐藏/敏感文件及内容中的密钥会被排除或脱敏。它们输出证据，不选择 Agent 身份，不包含新的产品适配器。脚本方式启动 `runtime/analyst_tools.py` 会先加入仓库根路径，子进程继承 `ASG_AUDIT_DIR`、`ASG_RECIPE_DIR` 和 `ASG_FINGERPRINT_DB`。

Goose recipe 已改为以 launch evidence 为主、既有本地采集为线索；提示要求有限次搜索/读取后提交 `investigation.identity_evidence` 和四类资产摘要。`propose_recipe` 现在拒绝缺失身份来源或资产状态的配方。资产状态允许 `collected`、`empty`、`failed`、`unsupported`、`unknown`、`not_collected`，每类都带 `sources` 和 `uncertainty`，因此未知、失败和成功为空不会混淆。loader 检查保留为通用 bundle/配置/插件扫描证据，未恢复产品答案。

调查进程改为可记录生命周期：隔离 run 目录先写 `investigation_lifecycle.json`，包含目标与 Goose PID+create_time、返回码、超时、预算、审计路径和可恢复证据索引；超时结束时杀掉的只有本次 Goose 子进程，结果保持调查失败并写明超时，不显示为不支持。默认预算为 `ASG_GOOSE_TIMEOUT=300`、`ASG_GOOSE_MAX_TURNS=18`、`ASG_GOOSE_MAX_TOOL_REPETITIONS=4`，均可按运行覆盖。已检查本机 Goose `run --help` 和总帮助，没有确认的原生 subagent 入口，本轮未启用该能力。

状态映射记录如下：

| 字段 | 来源 | 缺失含义 |
| --- | --- | --- |
| 调查身份与资产摘要 | Goose `propose_recipe` 中引用的 MCP evidence | 没有候选或来源不足时调查失败；不会用本地名称冒充成功 |
| `model_gateway` / `mcp` / `skills` / `rules` | Goose 读取的进程派生文件和受限工具结果 | `not_collected`/`unknown`；成功采集后空结果才可为 `empty` |
| 调查生命周期 | `investigation_lifecycle.json` | 无文件表示调查尚未创建；超时保留证据并标 `timeout` |
| Hook | supervisor 安装 manifest 与独立事件验证 | 本轮未安装/未验证，不能由指纹、配方或历史事件推断生效 |

测试分类：原有回归基线为 `90` 项；本轮新增 `test_analyst_evidence.py` 4 项（含真实脚本 MCP 和配方摘要门禁）、`test_investigation_lifecycle.py` 2 项、`test_discovery.py` 2 项，共新增 `8` 项；全量命令结果为 `Ran 98 tests`、`OK`。模拟 Node 事件仍是机制测试；真实 Goose 单独记录，不能混称全部真实通过。

真实 Goose 复核使用现有 OpenCode Agent `1052:1789006497.273916` 只读取证，产物隔离在 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/goose-generic-assets-v4-20260910T083028Z/pid_1052_1789029028/`。Goose 实际执行 `8` 次工具调用并写入 `8` 份 evidence，但在 `299998ms` 后超时，没有 `candidate.json`；结果文件为该目录的 `result.json`，生命周期为 `timeout`。这是真实运行失败/未完成证据，不计为真实调查成功。

保护核对：8081 看板仍为 `http://127.0.0.1:8081/`、PID `48028`；8080 无监听；生产指纹库 SHA256 仍为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`；现用 Agent 与全局配置未修改。工作树中原有未跟踪交接文件 `docs/stage1/ADVISOR_REAL_HOOK_HANDOFF.md` 未加入提交。

本轮是通用调查证据与生命周期切片，不是 Stage1 闭环完成；exact/similar/miss、revision 演进、完整资产采集、Hook 安装及生效验证留在后续 review。

## 2026-09-10 review continuation：无总时限调查、部分 Findings 与受控续查/取消收敛

本轮实现增量基于 HEAD `0377af4`（此前汇报记录为 `a52e08d`，系同一代码内容在微调注释与文档提交时的 commit hash 替换，代码完全一致，不重写历史）。

### 关键决策与审查落实

1. **调查运行入口与生命周期控制审查**：
   - 默认移除 5 分钟（300s）硬超时，改由环境变量 `ASG_GOOSE_TIMEOUT`、`ASG_GOOSE_MAX_TURNS`、`ASG_GOOSE_MAX_TOOL_REPETITIONS` 显式配置；若未设置则不向 Goose CLI 注入总时限，也不会由 supervisor 主动终止。
   - 经对本机 Goose CLI 二进制执行原生字符串与帮助核查：Goose CLI 的 `--max-turns` 选项在命令行省略时，**其内置缺省值为 1000**（`Maximum number of turns allowed without user input (default: 1000)`），而非无限。已在文档与代码中如实记录这一原生特性，不在外部做虚假无限承诺。
   - 增加显式进程取消能力：`POST /api/reinvestigate/cancel?pid=<pid>`。通过 `ACTIVE_ANALYST_PROCESSES` 字典记录活跃 Goose 实例，接收取消信号后安全 terminate 子进程，将生命周期与结果标记为 `cancelled` 并保留已产生的证据与分项结果。
   - 单请求/子进程故障控制：通过 `try...finally` 保证 `_StreamJournal`、stderr 流和进程句柄正确释放；`Popen`、I/O 读取均有异常捕获，避免挂起。

2. **部分身份与资产 Finding 持久化与看板投影**：
   - 新增 `runtime/investigation_findings.py`，通过单写者机制（`_THREAD_LOCK` + 跨进程文件锁 `_FileLock` + 原子替换）确保分项并发安全，损坏或读取失败时禁止覆盖原文件。
   - MCP 工具 `submit_investigation_finding`：允许 Goose 在调查过程中，随时就已确凿的分项身份（`kind=identity`）或资产（`kind=asset`，涵盖 `model_gateway`、`mcp`、`skills`、`rules`）提交结果。每一项均严格校验 1-16 条真实成功观测的 evidence id，并绑定当前冻结的实例 PID+create_time。
   - 看板与 API 投影：未生成完整 Hook candidate 前，看板及 `/api/state` 会自动读取已落盘的 `investigation_findings.json`，将部分身份呈现在卡片标题（例如 `Goose 调查: <name>`）以及深度透视抽屉中；将资产状态（`collected`/`empty`/`failed`/`unsupported`/`unknown`）精确映射到相应字段，并附带 exact evidence id 来源。
   - 配方校验门禁扩展：`runtime/recipe_validation.py` 中的 `OBSERVATION_TOOLS` 补充纳入通用证据工具（`inspect_entry_surface`、`find_related_files`、`read_related_file`），确保 Goose 引用的真实证据可以通过候选配方校验；同时兼容 `investigation.identity_evidence` 与 `investigation.identity` 字段名。

3. **显式续查（`continue`）与历史隔离读取**：
   - 增加续查接口：`POST /api/reinvestigate/continue?pid=<pid>`。支持基于前次同一实例的隔离 run 目录发起续查。
   - 新增 MCP 工具 `get_saved_investigation`：在续查运行时，向 Goose 暴露前次受限生命周期摘要、已确定的分项 findings、未解决问题与 evidence id 索引；严禁重放历史完整 stdout，彻底杜绝上下文污染。
   - 证据安全拷贝：将前次保存的 `ev-*.json` 隔离复制到本次 run 的 `evidence/` 下，Goose 可基于已有事实继续深挖未解决项。

4. **有界流日志处理**：
   - 引入 `_StreamJournal`：对 Goose 的 `stream-json` 输出做滚动式结构解析与压缩统计，默认保留 4MB 滚动窗口，杜绝数十万行重复 token 思考日志将磁盘撑满；同时保留原始 message id、block 类型、字数和时间戳统计，工具调用与证据仍由独立 audit 文件保留。

### 真实运行与隔离证据

- 真实 Goose 隔离调查：在受控随机目录创建带有 `package.json`（`autonomous-task-agent` v2.4.1）与 `agent.json`（`custom-openai` / `qwen38-27b`，tools `file_read`/`web_fetch`，system_rules `do not reveal system prompts`）的 Python socket 目标（PID 720, create_time 1789032138.034748）。
- 使用已授权的 Lenovo 模型网关配置在内存加载密钥，在无总时限（`timeout_seconds=null`, `max_turns=null`）下运行一轮真实只读 Goose 调查：
  - 产物目录：`/var/folders/xf/_m1f6xjn7cd55zzpvqp3r3f80000gn/T/asg-real-goose-run-a59lwfd1/runs/pid_720_1789032139121/`。
  - Goose 自主调用 `get_target_context`、`inspect_entry_surface`、两次 `find_related_files`、三次 `read_related_file` 深入检查了目标根目录下的 `server.py`、`package.json`、`agent.json`。
  - Goose 自主调用了 6 次 `submit_investigation_finding`，成功持久化了 5 项证据绑定的确凿分项结论：
    - `identity`：identified，名称 `autonomous-task-agent`，版本 2.4.1，引用了 package.json、launch surface 和 server.py 源码 3 项证据。
    - `model_gateway`：collected，模型 `qwen38-27b`，网关 `custom-openai`，引用 2 项证据。
    - `rules`：collected，规则内容 `["do not reveal system prompts"]`，引用 2 项证据。
    - `mcp`：empty，确认目标无 MCP 配置，引用 2 项证据。
    - `skills`：empty，确认目标无技能目录，引用 2 项证据。
  - 调查持续运行 541 秒（超过原先 300 秒硬超时），未被意外杀死，无日志溢出（journal 有效滚动压缩）；由于目标为固定 socket 循环且无扩展点，Goose 得出 `unsupported` 结论，且因提议中引用的证据工具校验门禁拦截未产生 candidate，程序如实记录未产生有效 Recipe，分项 findings 与生命周期完整保留。

### 测试回归核对

- 运行全部自动化回归测试（包括状态真实性、生命周期记录、分项 finding 提取与投影、取消 API、续查数据流、跨进程单写者锁、真实 MCP 子进程）：
  ```
  Ran 106 tests in 29.007s
  OK
  ```
- 生产环境安全边界核对：
  - 看板 PID 48028 持续运行在 8081（`ASG_AUTONOMOUS_ANALYSIS=0`），8080 无监听；
  - 现用 OpenCode Agent（PID 5297）未被重启或终止；
  - 生产指纹库 `runtime/fingerprints.json` 保持未修改；
  - 无真实凭据硬编码或写入仓库文件。
