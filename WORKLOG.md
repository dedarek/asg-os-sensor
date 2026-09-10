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
