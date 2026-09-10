# Stage1 REVIEW（唯一最新 review 入口：docs/stage1/）

根目录 PLAN.md / WORKLOG.md / REVIEW.md 仅存历史轮次概览；最新进度、证据与结论
一律以本目录为准。

## 当前提交与工作树
- HEAD: 7291263（分支 work/discovery-goose-fingerprint，基线 f4194d3）。
- 工作树干净（docs/stage1/ADVISOR_REAL_HOOK_HANDOFF.md 未跟踪，保留顾问交接）。
- 8081 接收服务运行中（PID 见下文/ps），隔离指纹库，真实事件已验证 3 条（见 WORKLOG）。

## 真实验收（2026-09-10，非模拟）
- 用户已打开隔离工作区并执行一次目录列举任务；hook.loaded 与 tool.execute
  before/after（tool=read，同 call_id）均被接收端校验通过（PID 5297 绑定）。
- 证据归档：artifacts/stage1/evidence/real-hook-2026-09-10/（脱敏，不含 nonce）。
- 8081 状态页已接通（commit 7291263）：区分历史加载/当前新鲜度/观测 vs 阻断；
  页面与 JSON 均不公开 nonce；只描述绑定实例，不把其他实例/Agent 标成功。

## 待办（受控卸载验收）
- 未执行卸载；步骤已写入 WORKLOG。等待顾问协调用户再次只读调用后执行。
## 全量测试（精确命令与数量）
- 命令: ASG_TEST_NODE=<node> python3 -B -m unittest test_discovery test_matcher_stage1
  test_status_stage1 test_goose_stage1 test_adapter_stage1 test_synthetic_hook_integration
  test_opencode_plugin test_real_cli test_observe_page
- 结果: Ran 69 tests, OK。按文件（grep def test_ 统计）：discovery 8、matcher 17、status 11、
  goose 11、adapter 2、synthetic 1、opencode_plugin 9、real_cli 3、observe_page 7。
- 节点依赖：test_real_cli 3 项需 ASG_TEST_NODE；其余 66 项无需 node。
- observe_page 7 项为本轮新增（页面/健康/事件语义、nonce 不泄露、fail-closed）。

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
- 部署目录 artifacts/stage1/opencode-observe 当前仅含 ghost_install.py——尚未安装已审查的
  插件/manifest，nonce 尚未写入（待部署路径未就绪，不视为已部署）。

## 已知限制与未完成项
- 真实 OpenCode Hook 尚未安装/未加载；真实引擎/插件验收待用户在桌面打开隔离工作区后
  进行（此步需审阅批准后告知用户，不预先宣称必然成功）。
- 插件文件删除不等于模块已从引擎内存卸载：未验证运行中卸载后不再产事件即不得宣称
  完全回滚已被证明；回滚应使用已校验 manifest+hash 的 uninstall 命令并保留 runs 历史，
  不得要求用户手动删除 .asg-observe 状态目录。
- 退出工作区不保证退出独立引擎（Electron utility 可能被共享）：无法证实则明确未知，
  不要求用户终止共享进程。
- Windows msvcrt 锁分支未本机实测（macOS）；真实跨版本目标复用未复跑。
- 接入点证据映射、Hook 安装器真实生效验证与回滚为下一阶段；本阶段未完成 Hook 安装。

## 进入下一阶段（Hook 安装与生效验证）的条件
- 条件未齐：接入点证据映射、真实插件安装/握手/事件验证、安全回滚证明。当前完成
  发现..调查..落库..复用闭环，并已准备好隔离测试工作区方案（待批准后执行）。