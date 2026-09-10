# Stage1 PLAN — 进程发现 → Goose 调查 → 指纹建立、复用与演进

基线: `f4194d3` (main, clean) — 分支 `work/discovery-goose-fingerprint`，worktree `/Users/mac/个人项目/asg-os-sensor-stage1`。
原 8080 服务 (PID 89739, ASG_AUTONOMOUS_ANALYSIS=0) 保持不动；演示服务用 8081。
不覆盖用户修改，不清理已有指纹/历史事件/E2E 证据；不合并 main、不推送远端。

## 1. 范围

只做第一阶段：发现哪些 Agent、每个属于哪个指纹、有证据支持的 Hook 接入方案（配方）。
不做平台授权、平台注册、实际 Hook 安装、防控算法。配方只提方案（程序校验+保存），不安装、不执行生成的脚本。
小步重构，不重写 UI、不引入完整平台、不加无关依赖。

## 2. 核查结论（以代码为准，2026-09-09）

- 发现：`Sensor.agent_score` 有通用行为路径（flag/流协议/子进程/网络门禁），产品提示仅辅助
  （`identify` + identity_score 70，ASG_IDENTITY_HINTS=0 可关）。`test_discovery.py` 8 用例通过。
  缺口：只有高分候选（>=50），无中间分待观察/重评；只用 PID 无 create_time；无有界候选/缓存/调查队列；可见性限制未显式暴露。
- `analyzer.analyze` 的 argv_shape 内嵌产品名单（pi-coding-agent/piagent/claude-code/codex/opencode/goose），违反不靠名单原则。
- `matcher`：`match()` 在读取路径直接 match_count+1 + save()（看板每 30s 扫描即膨胀，
  现有 harness-01/02/03 的 2000+ 命中数即此副作用）；仅凭 entry_token 相同即命中、空 flags/空配置也可命中；
  无 exact/similar/miss 区分；无版本/兼容条件；`remember()` 直接覆盖 hook_recipe 无 revision 历史；
  非原子写、跨进程并发不安全；DB 路径硬编码无隔离入口。
- 配方校验：`propose_recipe` 与 `verify_candidate` 只要求 {match_features, observation, hook, fallback}，远低于所需字段；
  `analyst_tools.py` 有重复分支；fingerprints.json 含 observed_exe_full 原始路径需脱敏。
- 看板：每次扫描调用 matcher.match（副作用膨胀）；只用 PID；候选无界；调查状态为内存态；
  文案把命中说成挂接（已适配挂接/挂接率/完成适配/未挂接）。
- LLM：llm.yaml custom-openai/qwen38-27b，密钥在被忽略的 .env；上游证书 2026-09-09 04:59:59 GMT 过期（openssl 已确认）；
  看板与 e2e 在 INSECURE=1 且无 ALLOW 时阻止深度外发——正确，未经批准不得自启 ALLOW。
- E2E：`e2e/e2e_unknown.py` 默认清空 runtime/fingerprints.json 与 RUN_ROOT，有破坏共享证据副作用，先加隔离入口再跑隔离测试。

## 3. 实施步骤

### S0 文档与隔离基建
- docs/stage1/{PLAN,WORKLOG,REVIEW}.md；artifacts/stage1/<run-id>/（gitignore）。
- ASG_FINGERPRINT_DB 隔离入口（matcher + 看板 + e2e 共用），测试永不写共享 runtime/fingerprints.json。

### S1 发现与评分（asg_os_sensor.py + runtime/identity.py + runtime/analyzer.py + test）
- 保留通用行为路径，产品提示仅辅助；ASG_IDENTITY_HINTS=0 下随机命名合成 Agent 仍可发现（测试）。
- 高分候选进入调查；中间分（默认 30-49）进待观察并重评；暴露 reasons + 可见性限制。
- 实例标识 instance_id = pid + create_time；退出清理；PID 复用视为新实例。
- Shell/包装器排除保持；任务文本/产品名出现在参数值中不得作为入口（entrypoints 只看 exe/argv0/首个非 flag/-m 目标）。
- 有界候选/缓存/调查队列（上限+逐出策略）。
- 去掉 analyzer 的产品名单，改为通用 basename 保留，兼容旧 <product:file> 包装的归一化。

### S2 指纹匹配（runtime/matcher.py）
- match() 纯读、无副作用；新增 record_hit() 显式计数。
- 决策三态：exact（已验证+兼容，复用配方）、similar（历史参考，进差异调查）、miss（新调查）。
- exact 条件（缺一不可）：exe 相同 + runtime 相同 + 非空 entry_token 相同 + flags 兼容 + config/版本兼容
  （空配置永不作为 exact 依据）。禁止仅凭同名/单个共享参数/空配置判 exact。
  说明稳定家族特征 vs 配方兼容特征。
- similar：同 exe+runtime 且结构接近，只给参考。
- 原子写（tmp + os.replace）+ 进程内锁；跨进程并发记录决策与已知限制+测试。
- 旧指纹保守迁移：标记 legacy-unverified，hook_verified=false，永不自动视为已验证安装方案。

### S3 Goose 调查（recipes/runtime_analyst.yaml + runtime/analyst_tools.py + runtime/recipes.py 新建）
- 配方必填：身份名称+证据引用；运行时/入口/版本或未知说明；指纹匹配与兼容条件；Hook 接入点及方式；
  是否需要重启；可观测能力；是否支持执行前决策及原因；安装/验证/撤销描述；未知项+不支持原因+降级；
  新建/演进家族建议（action + target_id）。Goose 提建议，程序校验保存；本阶段不安装执行。
- 新建 runtime/recipes.py 统一 validator：structure_valid vs hook_verified(false 恒定) vs support；
  密钥/执行材料拒绝；置信度下限；unknown 字段显式标记允许。
- 更新 analyst prompt 要求上述字段与证据引用；analyst_tools 去重分支并接 validator。

### S4 指纹保存与演进（matcher.commit + 版本史）
- 指纹-配方版本关系：指纹 revision 递增，保留 revisions[] 历史（配方快照+证据+来源 run/instance/tool 调用）。
- 演进生成新 revision 不覆盖；并发保存不丢失（读-改-写重试+原子替换+测试）；无效配方拒绝落库。
- 家族 vs 实例分离：家族共享指纹；实例独立身份与进程归属（ownership 保持）。
- 精确复用仍标记“配方待实际验证”，下一阶段才做 Hook 生效验证。

### S5 页面与 API（monitor_dashboard.py）
- 状态如实展示：候选/待观察；调查排队/调查中/调查失败(+原因)；精确命中/相似命中/未命中；
  配方结构校验通过/配方待实际验证/不支持。展示名称依据、实例 ID、主 PID、关联 PID、
  指纹/配方版本、失败原因、建议接入方式及重启要求。
- 修正全部“已挂接/已防护/已适配/挂接率”文案与字段语义；KPI 改为命中/校验口径。
- 调查调度：并发上限、超时、失败原因、重试冷却；失败/超时/无配方/重启后不永久卡 investigating。
- 看板读隔离 DB（env），演示端口 8081。

### S6 测试与证据（分别报告，不混为“全部通过”）
- 单元测试（现有 8 + 新增）：关闭产品规则随机命名发现；负对照；实例 PID 复用；
  嵌套不合并/多实例独立；matcher exact/similar/miss；入口变化不盲目复用；
  演进历史可追溯；并发保存；无效配方拒绝；失败不卡死。
- 模拟模型测试（mock propose_recipe）：隔离目标走 validator+commit 全链路，明确标识 mock。
- 真实 Goose 测试：隔离目标；先查证书/连接；若 INSECURE=1 且无 ALLOW 则记录 blocked 证据并标未完成，
  不用模拟冒充；绝不自行开启 ASG_ALLOW_INSECURE_ANALYST=1。
- 真实终端扫描：只读扫描，报告可见性限制与误报/漏检。
- 运行产物放 artifacts/stage1/<run-id>/（忽略），git 只提交简洁报告+证据索引。

## 4. 验收映射（8.1-8.10）

- 8.1 关闭产品规则随机命名合成 Agent 可发现 → S1+S6（单元+隔离合成目标）。
- 8.2 负对照不误识别 → S1+S6。
- 8.3 真实 Goose 隔离目标产出可校验配方（mock 另标）→ S3+S6；受阻则标未完成。
- 8.4 同兼容运行时重启快速复用不调 Goose → S2+S4（record_hit + exact）。
- 8.5 入口/兼容特征变化不错误 exact → S2+S6。
- 8.6 相似演进新版本历史可追溯 → S4+S6。
- 8.7 嵌套不误合并、多实例独立查询 → S1/S4/S5+S6（ownership+实例 API）。
- 8.8 失败/超时/无配方/重启不卡 → S5+S6。
- 8.9 并发不污染丢失 → S4+S6。
- 8.10 页面无“已挂接/已防护” → S5+S6。

## 5. 提交节奏

- 小步提交：S0 → S1 → S2 → S3 → S4 → S5 → S6，每步可审阅；信息含范围+验证。
- 不合并 main、不推送远端；收尾保留 worktree 与提交供 review；清理自建后台测试进程；
  若留演示服务则记录端口/PID/日志/停止方式。

## 6. 本轮增量（状态真实性修复）

- 基于 `cd47ea2`，只修正观测撤销、状态投影和隔离验收路径；不推进 exact/similar/miss、版本演进、Hook 安装或 8080。
- 验收重点：撤销后保留已知 PID/create_time 但不返回 healthy；看板与 API 使用同一观测语义；回环适配不跟随重定向并限制响应大小；异常事件计数降级且不中断全局扫描。
- 完成后停在 review 点，保留独立 8081 看板和随机回环观测端口供查看。

## 7. 本轮增量（受控 onboarding 纵向切片）

- 基线 `f8af00e`；只把已有发现实例、exact 复用或 Goose 调查计划、固定 project workspace-plugin 安装事务、真实事件验证和隔离经验持久化串成最小闭环。
- 指纹条目/revision 的来源元数据为 `recipe_source=goose` 才能进入可复用调查配方；无来源旧配方标为 manual/legacy，不能自动安装。安装需 `ASG_ONBOARDING_AUTHORIZED=1`、`ASG_ONBOARDING_AUTO_INSTALL=1`、`ASG_ONBOARDING_SCOPE=project` 和匹配 workspace。
- 计划、安装、激活和验证状态始终独立；本轮不启用真实 Goose 外发、不触碰现用 Agent/全局配置、不操作 8080。通过隔离临时 workspace 和现有真实观测接收器验证，交付后停在 review。

## 2026-09-10 review follow-up

实现提交为 `a90e0c8`（基线 `8dfe6a0`）。本轮补齐 Goose prior 经验窄投影、真实来源门禁、exact 扫描的统一授权/幂等执行路径、PID+create_time 新实例重绑定，以及 manifest 撤销时的验证降级。验收仍使用隔离 8081、隔离指纹库和已有隔离观测接收器，不触碰 8080、全局配置或现用 Agent。真实 Goose 仅记录实际的模型请求前凭据阻塞，不把模拟结果当成功。

## 2026-09-10 真实 Goose 复核追加

已按顾问要求从 `/Users/mac/个人项目/asg-os-sensor/.env` 内存加载配置，在隔离目标上完成真实 Goose 两轮调查和中间 prior MCP 回读。结果目录为 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/`；自动安装、全局配置、8080 和现用 Agent 均保持不变。详细结果见根目录 `WORKLOG.md`。

## 2026-09-10 review milestone：兼容历史与真实 OpenCode 观测证据

本次基线为 `8dc06d9`，范围仅包含兼容条件下的跨实例 prior 查询、回环观测证据工具、脚本启动的隔离路径边界及对应回归；使用现有真实 OpenCode 实例进行只读 Goose 调查。自动安装保持关闭，不重启或重新安装现用 Agent，不操作 8080。交付后停在 review 点，完整 exact/similar/miss 调度和 revision 演进留待下一步。

验收标准：兼容 executable/entry/build/launch 的新 PID 可读历史并保留 revision/source；入口或构建变化不命中；MCP 子进程不回退默认 recipe/fingerprint 库；Goose 可看到绑定 PID+create_time 的观测健康和事件类型，但不会把 stale 或事件历史当作当前 Hook 生效；原生二进制仍不要求脚本 entry token。

## 2026-09-10 review follow-up：加载器证据与隔离验收准备

基于 `b00bf10`，本次只补充 loader/config/plugin 作用域的只读证据、隔离验收工作区和独立启动可行性核对；不把目标 CWD 当安装范围，不重启现用 Agent，不操作 8080。新工作区在用户打开前保持无事件；完整 Stage1 闭环、Hook 推广安装与防控仍留在后续 review。
