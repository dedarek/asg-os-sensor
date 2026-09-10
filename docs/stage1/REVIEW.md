# Stage1 REVIEW（唯一最新 review 入口：docs/stage1/）

根目录 PLAN.md / WORKLOG.md / REVIEW.md 仅存历史轮次概览；最新进度、证据与结论
一律以本目录为准。

## 当前提交与工作树
- 当前改动待提交（分支 work/discovery-goose-fingerprint，基线 f4194d3）；
  `docs/stage1/ADVISOR_REAL_HOOK_HANDOFF.md` 仍是未跟踪的顾问交接文件，不能声明工作树干净。
- 原多 Agent 看板运行在 `127.0.0.1:8081`；单实例观测接收器独立运行在
  `127.0.0.1:64680`（随机回环端口示例）。8080 未触碰。
- 真实事件已验证 3 条（见 WORKLOG），active 插件保持不动。

## 真实验收（2026-09-10，非模拟）
- 用户已打开隔离工作区并执行一次目录列举任务；hook.loaded 与 tool.execute
  before/after（tool=read，同 call_id）均被接收端校验通过（PID 5297 绑定）。
- 证据归档：artifacts/stage1/evidence/real-hook-2026-09-10/（脱敏，不含 nonce）。
- 观测接收器 `/page` 位于独立回环随机端口；它区分历史加载/当前新鲜度/观测 vs 阻断，
  页面与 JSON 均不公开 nonce，只描述绑定实例。
- 8081 的根页面恢复为原 `monitor_dashboard.py` 多 Agent 看板；`/api/state` 保留
  多 Agent 列表和扫描状态。看板通过 `ASG_OBSERVE_URL` 读取观测契约；当前证据绑定
  5297，而扫描卡片主 PID 4970，因此 4970 及其他 Agent 不显示已观测成功。

## 待办（受控卸载验收）
- 未执行卸载；步骤已写入 WORKLOG。等待顾问协调用户再次只读调用后执行。
## 全量测试（本轮实际执行一次）
- 命令: `ASG_TEST_NODE=/Users/mac/.nvm/versions/node/v24.16.0/bin/node python3 -B -m unittest
  test_discovery test_matcher_stage1 test_status_stage1 test_goose_stage1
  test_adapter_stage1 test_synthetic_hook_integration test_opencode_plugin test_real_cli
  test_observe_page test_dashboard_http`
- 结果: `Ran 71 tests in 15.065s`，`OK`。其中 `test_real_cli` 3 项需 node，其余 68 项
  无需 node；本轮新增 `test_dashboard_http.py` 2 项。输出含现有 LibreSSL/urllib3
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
