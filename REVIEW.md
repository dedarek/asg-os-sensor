# 状态真实性修复：review 交付

基线 41a53b5，工作树 /Users/mac/个人项目/asg-os-sensor-stage1，分支 work/discovery-goose-fingerprint。本轮基线 `cd47ea2`，独立提交见 git log -1。

关键文件：runtime/status.py 共用状态；monitor_dashboard.py API/页面/实例呈现；runtime/analyst_tools.py prior 失败传播；test_status_stage1.py 回归；test_matcher_stage1.py 子进程产物隔离；.gitignore 保留接手时本地产物；根目录 PLAN.md/WORKLOG.md/REVIEW.md 是本轮记录，docs/stage1/ 保留前轮历史。

状态映射详见 WORKLOG.md。调查 disabled/not_scheduled/running/succeeded/failed 来自开关和调度器；没有实际队列，未返回 queued。资产默认 not_collected，带采集来源的契约可区分 collected（空则未发现）/failed/unsupported；目前真实采集尚未接入该契约。Hook 固定 not_installed、verified=false。匹配仍为旧式布尔，不是 exact/similar/miss。历史配方不替代实例采集凭据。

## 验证

在本工作树执行：
```sh
python3 -B -m unittest test_discovery test_matcher_stage1 test_status_stage1
```
33 通过：原有 discovery 8 + 前轮 matcher 17 + 本轮 8。新增用例包括模拟扫描集成、HTTP 禁用入口、真实脚本 MCP 损坏库错误。未运行真实 Goose；真实 MCP 不是模型调查成功证明。Windows 锁分支未在 Windows 验收。

真实页面 http://127.0.0.1:8081/ ，当前 PID 47562，日志 artifacts/stage1/truth-review/server.log；截图 page.png；验证摘要 verification.json；生产库基线 baseline.json。原始产物均已忽略。真实运行发现 4 个实例，关联 PID 数随进程变化。8080 初始及结束均无监听；生产库哈希一致。

重现启动（先确认 8081 空闲；每次新建独立目录）：
```sh
cd '/Users/mac/个人项目/asg-os-sensor-stage1'
run_dir="$(mktemp -d "$PWD/artifacts/stage1/truth-XXXXXXXX")"
ASG_AUTONOMOUS_ANALYSIS=0 ASG_PORT=8081 \
ASG_FINGERPRINT_DB="$run_dir/fingerprints.json" \
ASG_RUN_DIR="$run_dir" ASG_EVENT_DIR="$run_dir/events" \
ASG_AUDIT_DIR="$run_dir/audit" ASG_RECIPE_DIR="$run_dir/recipes" \
python3 -B monitor_dashboard.py >"$run_dir/server.log" 2>&1 &
echo $!
```
停止当前演示服务：先 `ps -p 47562 -o pid=,comm=` 确认仍是本次 Python 服务，再 `kill -TERM 47562`；禁止按进程名批量终止。

本轮新增撤销链回归：隔离 `ghost_install.py --install/--uninstall` 后，观测 `/health` 与 `/events` 返回 `503/status=revoked` 并保留绑定实例字段，看板 `/api/state` 同步为 `revoked`；真实活动插件未卸载。完整回归为 74 项通过。本轮是状态真实性修复，不是 Stage1 闭环完成。

最终隔离验收页面为 `http://127.0.0.1:8081/`（PID 24825），观测接收器为 `http://127.0.0.1:52708`（PID 24804）；运行目录和日志见 WORKLOG，8080 无监听，生产指纹库哈希未变化。页面截图已在本次 review 中核对，显示 3 个真实扫描实例、调查禁用、资产/网络尚未采集、Hook 未安装。

真实部署 manifest 位于 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json`，当前 active；事件接收文件为 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`。调查禁用只作用于隔离看板进程，恢复命令是将其启动环境 `ASG_AUTONOMOUS_ANALYSIS` 改为 `1`，不改全局配置、不添加不安全 TLS 覆盖。

## review 重点与限制

- 资产保守降级：旧配方资料保留为 historical_recipe；尚无当前实例的采集完整性证明，故不显示历史空数组为“未发现”。后续须连接真实采集器，不能只填状态。
- 同名实例不再聚合状态，避免跨实例借用成功。发现算法、归属启发式本轮未重做，误报/漏检与嵌套归属仍需完整 Stage1 验收。
- 调查状态仍是内存/PID 调度记录；重启后未调度；PID 复用下调查记录的完整生命周期仍属后续闭环工作。
- 未做匹配兼容分档、revision 演进、真实 Goose 调查和 Hook 安装/验证。TLS 外发权限保持原边界，未启用 ASG_ALLOW_INSECURE_ANALYST。

**本轮是状态真实性修复，不是 Stage1 闭环完成。** 停在 review 点；不具备宣称 Hook 安装验证阶段已就绪的依据，不合并、不推送、不动 8080。

## 受控 onboarding 纵向切片（本次独立提交）

本次以 `f8af00e` 为基线，新增 `runtime/onboarding.py`，把发现实例、精确指纹复用/调查所需计划、固定安装器、事件验证和隔离经验持久化串起来。`monitor_dashboard.py` 提供 `/api/onboarding`、`/api/onboarding/execute`、`/api/onboarding/verify`，页面追加接入链状态；`recipes/runtime_analyst.yaml` 明确固定适配器契约；`matcher.py` 给新保存的调查指纹条目和 revision 写入 Goose 来源元数据。旧手写/无来源配方显示为 manual/legacy 并拒绝自动安装。

状态语义：调查来自开关和调度器记录（禁用=disabled、运行=running、成功/失败按真实结果）；资产来自实例采集凭据（缺失=尚未采集，成功空结果才=未发现，失败不转空）；Hook 来自 ghost_install 和 EventVerifier（未安装、待重启、事件已验证、验证失败），指纹命中和配方结构通过均不会升级 Hook 状态。关联 PID 来自扫描 ownership，执行事件来自观测采集，两者没有互相推断；网络无证据保持尚未采集；宿主无本地证据保持未知。

验收矩阵：

| 能力 | 状态 | 证据与边界 |
| --- | --- | --- |
| 行为发现、随机命名和实例归属 | 部分真实 | 本机 8081 扫描和既有 discovery 回归；尚未完成跨 Agent 盲测与系统误报/漏检统计 |
| 自动 Goose 调查 | 未完成/未验证 | 调度和候选配方保存路径已接通；本轮 `ASG_AUTONOMOUS_ANALYSIS=0`，没有真实模型外发 |
| MCP/Skill/规则/网络采集 | 未完成 | 页面/API 诚实返回尚未采集；没有把空字段当未发现 |
| 指纹学习与 exact 复用 | 模拟/单测验证 | matcher 与 onboarding 回归覆盖；未以新的真实 Agent 做跨启动复用验收 |
| 授权范围安装计划与执行 | 隔离真实代码路径 | 临时 workspace 执行真实 ghost_install；仅支持 project + 固定插件，未触碰现用工作区 |
| 下次启动激活 | 模拟事件验证 | plan 明确 pending_restart；Node 子进程加载真实插件并产生 `hook.loaded`，不等于真实 OpenCode 下一次启动 |
| 真实事件/API | 已有真实证据，新增链路未实测 | 隔离 OpenCode 接收器已有 3 条绑定事件；onboarding 新 API 在测试中验证，未对现用 Agent 执行安装 |
| 撤销/回滚 | 隔离真实代码路径 | 既有 install→uninstall→HTTP revoked 回归通过；未卸载当前 active 插件 |
| 阻断、平台授权、平台注册 | 未实现 | 明确为后续范围 |

关键限制：当前自动安装适配器只覆盖已经登记的 OpenCode project workspace-plugin；通用主流 Agent 的发现和调查入口尚未完成真实盲测。真实 Goose 仍受本轮隔离看板禁用和 TLS 外发边界限制。配方来源、授权范围、激活时机和事件验证记录会写入隔离 `experience.json`，但这不代表安装已生效或具备阻断能力。

本轮是状态真实性修复和一条受控 onboarding 纵向切片，不是 Stage1 闭环完成；完成后停在 review，不合并、不推送、不部署到 8080、不进入 Hook 安装阶段。

## 最终隔离验收工作区（commit 7dec303）

当前可查看页面为 [http://127.0.0.1:8081/](http://127.0.0.1:8081/)，看板 PID `33217`。运行目录为 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-hPAgMHZb`，日志和脱敏核对摘要分别为 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-hPAgMHZb/server.log` 与 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-hPAgMHZb/verification.json`。页面展示 3 个本机扫描实例，Goose 调查为禁用，Hook 为未安装；未混入模拟成功状态。

插件部署与事件接收的绝对路径：工作区 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe`；manifest `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json`；插件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/asg-observe.js`；事件文件 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`。接收器 URL 为 `http://127.0.0.1:52708`、PID `24804`，真实绑定实例为 `5297:1789006943.640438`，事件 `3` 条有效、`0` 条无效。接收器当前健康为 `stale/healthy=false`，因为事件超过 TTL；这项状态保持诚实。

验证摘要同时确认 8080 无监听，生产指纹库 `/Users/mac/个人项目/asg-os-sensor-stage1/runtime/fingerprints.json` SHA256 为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`，与此前记录一致。当前运行的看板环境使用隔离指纹库 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/authorized-goose/fingerprints.json`、`ASG_RUN_DIR` 指向上述 run 目录、`ASG_AUTONOMOUS_ANALYSIS=0`；没有设置真实调查外发或自动安装授权变量。

停止只处理本任务 PID `33217`（看板）或 `24804`（接收器），先核对命令归属，不按进程名批量终止。保留当前真实 Agent 和 active manifest，等待 review。

## 最新审查交付：commit a90e0c8

本轮工作树 `/Users/mac/个人项目/asg-os-sensor-stage1`、分支 `work/discovery-goose-fingerprint`，实现提交 `a90e0c8`，基线 `8dfe6a0`。本轮把 prior 经验读取、真实来源门禁、exact 扫描自动执行和撤销验证补到受控 onboarding 切片；文档提交随后单独记录。状态映射以 `WORKLOG.md` 的“字段 → 来源 → 缺失含义”为准。

### 交付内容

- 调查：禁用时 API/UI 均为 `disabled`，不显示解析中；无真实持久队列时不伪造 `queued`。失败、阻塞和繁忙原因可写入隔离经验供后续 Goose 参考。
- 资产：实例没有采集证据时统一为 `not_collected`；成功空采集才可显示未发现；采集失败、网络无数据、执行事件未接入均不转换成否定结论。
- Hook：指纹命中、配方结构校验通过、`hook.loaded` 都不等价于已防护；加载只返回 `loaded_verified`，加载加工具事件才返回 `events_verified`，撤销 manifest 返回 `revoked`。Sink 保持未接入／未验证。
- prior：新增 `get_prior_experience`，脚本方式启动也使用显式仓库导入路径和继承的隔离库；损坏库是 MCP 错误，窄投影不含路径、配方、凭据或 nonce。
- 执行路径：exact 扫描与新调查共用授权、固定 backend、隔离 workspace、幂等安装和 PID+create_time 验证；新实例不会继承旧实例的事件成功状态。

### 验收证据

| 类别 | 结果 |
| --- | --- |
| 单元/本地回归 | 全量 83 项通过；目标 `test_onboarding test_dashboard_http` 14 项通过 |
| 模拟集成 | Node 加载仓库真实插件并验证 `hook.loaded`、`tool.execute.before/after`；来源明确为 `goose-simulated` |
| 真实 MCP 子进程 | 真实 `python runtime/analyst_tools.py` 进程在无 `PYTHONPATH` 时读取隔离 prior，并返回失败/验证摘要；审计和证据均在临时目录 |
| 真实 Goose | 未通过：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-KouKJi6H/real_goose_result.json` 为 `blocked_before_model_request`，`external_request_sent=false`，原因是配置凭据缺失 |
| 真实终端扫描 | 8081 扫描 3 个本机实例；调查 disabled、资产/网络 not_collected、Hook not_installed |

### 隔离验收页面与插件路径

页面现在可打开：[http://127.0.0.1:8081/](http://127.0.0.1:8081/)，看板 PID `38258`。运行目录、日志和核对摘要分别为：

- `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-jcA6qPaU`
- `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-jcA6qPaU/server.log`
- `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-jcA6qPaU/verification.json`

插件部署和事件接收：workspace `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe`；manifest `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json`；plugin `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/asg-observe.js`；events `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`。接收器为 `http://127.0.0.1:52708`、PID `24804`，已有 `3` 条有效事件、`0` 条无效事件，当前健康为 `stale/healthy=false`，不被宣称为当前 Hook 生效。

保护核对：8080 无监听；生产指纹库 `/Users/mac/个人项目/asg-os-sensor-stage1/runtime/fingerprints.json` SHA256 为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`，与既有记录一致；未修改全局配置、未重启或停止现用 Agent、未卸载 active 插件。

### Review 结论和限制

`a90e0c8` 是受控 onboarding 反馈闭环和状态真实性修复，不是 Stage1 闭环完成。真实 Goose 没有因凭据缺失而被模拟成功；通用 backend、真实目标安装、下一次启动后的真实 Hook 生效、MCP/Skill/规则/网络当前实例采集和阻断算法仍未完成。当前具备继续 review exact/similar/miss 与 revision 演进的代码基础，但不具备进入真实 Hook 安装与防控阶段的验收依据。

当前保留隔离服务供 review。停止前确认 PID `38258` 的工作目录为本 worktree 后，仅停止该 PID；接收器如需停止只处理 PID `24804`，不按进程名批量终止。

**本轮是状态真实性修复和受控 onboarding 反馈闭环，不是 Stage1 闭环完成。**

## 真实 Goose 复核追加（run 6UDd4Dsj）

顾问指出此前的凭据阻塞来自隔离 worktree 未继承主仓库配置。现已按指定方式从 `/Users/mac/个人项目/asg-os-sensor/.env` 内存加载配置；未复制密钥、未输出值或尾号。预检为 `custom-openai`、`ASG_ANALYST_API_KEY`、`key_present=True`、`qwen38-27b`，TLS 兼容只对本次授权网关生效。

真实随机目标 PID `40956`、create_time `1789016070.516786` 完成两轮真实 Goose：第一轮成功生成并保存隔离候选配方；中间真实 MCP 子进程读取到同一实例的配方 prior；第二轮 `get_target_context` 收到 `prior_experience.recent` 1 条后再次成功。目标最终配方身份为 `Python`，Hook 接入方式为 `unsupported`，所以没有安装或宣称可用 Hook。

证据总览：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/real_goose_result.json`；prior MCP 摘要：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/prior_mcp_result.json`；隔离指纹库：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-live-6UDd4Dsj/fingerprints.json`。审计、证据、候选和日志均在该 run 目录，未污染生产库。

当前全量测试为 84 项通过，包含真实 MCP 子进程和真实 Goose 结果目录的隔离核对；模拟 Node 插件事件仍明确标为 `goose-simulated`。真实调查已完成，但这是受控目标验收，不代表通用 Agent、exact 复用去重、revision 演进或 Hook 生效验证完成。

页面、插件、接收器和保护状态保持不变：8081 页面 PID `38258`；接收器 PID `24804`；manifest、plugin、events 的绝对路径见前文；8080 无监听；生产库哈希仍为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`；现用 Agent PID `5297` 未重启或触碰。

**本轮补充证明真实 Goose 调查和 prior 传递链路，不是 Stage1 闭环完成。**

## 2026-09-10 review milestone：兼容历史与真实 OpenCode 观测证据

本轮是状态真实性与调查证据补强，不是 Stage1 闭环完成。基线为 `8dc06d9`，已作为当前分支独立提交交付。

实现重点：`runtime/onboarding.py` 按完整兼容快照跨实例读取 prior，保留 revision/source，入口或构建变化拒绝历史复用；`runtime/analyst_tools.py` 修复脚本启动路径，显式隔离库错误不回退默认库，并把配置回环接收器的绑定健康/事件类型作为只读证据提供给 Goose；`recipes/runtime_analyst.yaml` 明确观测、安装和阻断的边界。原生二进制继续允许 `entry=native`，不要求脚本 entry token。

状态口径保持独立：调查来自调度器/真实 Goose 结果；资产来自带来源的采集器；Hook 来自安装 manifest 和绑定事件验证。指纹命中或候选配方保存不升级 Hook 状态，接收器 `stale` 不升级为当前生效。

### 真实 Goose 结果

真实运行总结：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-opencode-live-8eCFRnwK/real_goose_result.json`。目标为现有 OpenCode helper `5297:1789006943.640438`，只读调查使用授权配置的内存副本，Goose 实际读取 `get_target_context` 中的真实回环观测证据，识别 `OpenCode`，输出并保存 workspace-plugin 候选计划；候选配方路径为 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-goose-opencode-live-8eCFRnwK/pid_5297_1789017233/recipes/candidate.json`。自动安装关闭，未注入 Hook 字段，未重启或重新安装现用 Agent。

候选计划的接入方式为 `opencode-workspace-plugin/workspace-plugin`，`restart_required=unknown`，但接收器当前 `stale/healthy=false`；这代表需要未来新鲜加载事件才能验证激活，不代表已经安装或生效。真实隔离接收器的既有部署和事件路径为：workspace `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe`，manifest `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json`，plugin `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/asg-observe.js`，events `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`，接收器 `http://127.0.0.1:52708`；当前 `3` 条有效、`0` 条无效。

### 测试与审阅边界

- 原有回归 84 项保持通过；新增兼容跨实例/版本拒绝、MCP 子进程兼容 prior 和显式隔离 recipe 测试后，全量为 86 项通过。
- 模拟集成仍明确标为 `goose-simulated`：Node 子进程加载仓库真实插件并验证事件链，只证明机制集成。
- 真实 OpenCode 的既有事件证明一次插件加载和工具事件接收；本轮不对正在使用的桌面实例执行 plan→auth→install 或重启，因此不把它写成新的真实安装闭环。
- 8081 看板使用全新的隔离运行目录 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-vIbGLtFEA`，地址为 `http://127.0.0.1:8081/`，PID `48028`，日志 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-vIbGLtFEA/server.log`，摘要 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/dashboard-review-vIbGLtFEA/verification.json`。页面/API 已验证真实扫描结果、调查 `disabled`、观测 `connected` 但健康 `stale`；指纹、事件、审计和 recipe 目录均为该 run 独立路径。
- 保护核对：8080 无监听；生产指纹库 SHA256 仍为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`；现用 Agent PID `5297`、接收器 PID `24804` 和 active manifest 未动。macOS 当前锁屏，无法从自动化界面取得截图；页面已通过本机 HTTP 200 和 `/api/state` 核对，解锁后可直接打开上述地址。

本轮不具备进入 Hook 安装与防控阶段的条件：通用 Agent backend、完整资产采集、exact/similar/miss 闭环去重、版本演进、真实新实例复用和 Hook 生效/撤销的全量验收仍未完成。停在 review，不合并、不推送、不部署 8080。

## 2026-09-10 review follow-up：隔离验收工作区已准备

本轮补足了 reviewer 要求的 loader/config/plugin 只读证据和独立引擎可行性核对，并准备了可由用户打开的隔离工作区。`inspect_loader_surface` 通过目标 OpenCode 1.18.25 bundle 的 `Info.plist`、有界 `app.asar` 标记和目标打开文件类别判断作用域；没有观察到目标项目插件路径时保持 `unresolved`。真实盲 Goose 调查因此给出 `unsupported` 候选，不把既有 observer 事件当作未加装目标的证明。

### 已审查的绝对路径

- 工作区：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance`
- 插件：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/asg-observe.js`
- manifest：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/.asg-observe/manifest.json`（active，sha256 `7991e3ae5ac6f475f7b322f9aded1fbbb577d6307abe9d57470f71a33e3ccd18`）
- 本次 run 事件文件：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/.asg-observe/runs/b5fd699f4d755aa7/events.jsonl`（用户打开前 0 行）
- 准备摘要：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/readiness.json`
- 独立启动探测：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/engine-startup/startup.json`；端口 `52709` 的探测进程已停止，未保留测试引擎。

### 已核对的真实接收路径

已有真实隔离接收器 `http://127.0.0.1:52708`（PID `24804`）绑定现用 OpenCode helper `5297:1789006943.640438`。其 manifest、插件和事件文件分别为：

- `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/manifest.json`
- `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/asg-observe.js`
- `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`

该文件有 `3` 条有效、`0` 条无效事件，类型为 `hook.loaded`、`tool.execute.before`、`tool.execute.after`；当前 health 是 `stale/healthy=false`，所以这只证明历史加载和一次工具调用曾被接收，不代表当前 Hook 生效，也不代表新验收工作区已经加载。

### 安全与可行性结论

独立 CLI `/Users/mac/.nvm/versions/node/v24.16.0/bin/opencode` 的 `serve --help` 和 1.18.25 实际 `serve` 探测均成功；显式隔离 HOME/XDG/`OPENCODE_CONFIG_DIR`、CWD 和 52709 端口后，生成物只出现在 `engine-startup` 目录。bundle 仍含单实例锁和全局配置回退/写入实现，因此直接启动桌面 GUI 不安全；用户后续打开新工作区后，应先取得新实例 PID＋create_time，再为该实例启动独立接收器并验证新 run 的 `hook.loaded` 与 `tool.execute.before/after`。

保护核对：8081 仍为 `http://127.0.0.1:8081/`、PID `48028`；8080 无监听；生产指纹库 `/Users/mac/个人项目/asg-os-sensor-stage1/runtime/fingerprints.json` SHA256 仍为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`。本轮没有重启或修改现用 Agent、全局配置、旧 active manifest 或 8080。

本轮完整回归为 `87` 项通过；原有测试、模拟 Node 机制测试、真实 Goose 盲调查和真实终端已有事件分别记录，未混为一个“全部真实通过”。本轮是状态真实性与隔离验收准备，不是 Stage1 闭环完成；停在 review，不进入 Hook 安装或防控。

## 2026-09-10 review continuation：已准备当前隔离验收工作区

本轮在 `dac0a8f` 基础上收紧了自定义 LLM 的 route-driven TLS 兼容实现，并修正 Node 测试夹具以覆盖当前 OpenCode V1 插件导出合同。TLS 例外只由当前 `llm.yaml` route 显式配置启用；默认仍校验证书，loopback proxy 只接受该 route 的 origin，拒绝重定向和跨 origin 重配置。代码没有硬编码供应商、route 名或上游地址，也没有设置系统/Python 全局 TLS 绕过。

### 状态与证据映射

| 维度 | 当前真实状态 | 证据 | 缺失含义 |
| --- | --- | --- | --- |
| 真实路由烟测 | `passed` | `verify-llm-route.log` 中 chat/responses 均 200 | 只证明配置 route 可达，不证明 Goose 产出配方 |
| 真实 Goose | `tools_executed_no_candidate` | `goose-clean5/derived-summary.json` 及 5 个 evidence | 没有 candidate 就不显示调查成功或接入方案 |
| 用户验收插件 | `prepared_awaiting_user_open` | 当前 workspace active manifest、插件 SHA、0 行 events | 未打开引擎前不声称 hook.loaded |
| 历史接收器 | `stale` | `opencode-observe` 的 3 条有效事件 | 只代表历史接收，不代表当前 Hook 生效 |
| headless CLI 插件 | `blocked_during_external_plugin_initialization` | 无插件基线成功；带插件对照 `/path` 超时 | 不把 serve 健康或静态部署当作插件已加载 |

### 当前可打开的绝对路径

- 页面：`http://127.0.0.1:8081/`；看板 PID `48028`。
- 隔离 workspace：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance`。
- 当前插件：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/asg-observe.js`。
- 当前 manifest：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/.asg-observe/manifest.json`，runid `d40626064e384a68`，active=true。
- 待接收事件：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/.opencode/plugins/.asg-observe/runs/d40626064e384a68/events.jsonl`，打开前 0 行。
- 准备摘要：`/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-hook-acceptance/readiness.json`。
- 历史接收器：`http://127.0.0.1:52708`，PID `24804`；workspace `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe`；其历史 events 文件为 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/opencode-observe/.opencode/plugins/.asg-observe/runs/660ad5f492e7ab91/events.jsonl`。

### 分类验收

- 单元/本地回归：原有 `87` 项保持通过；新增 3 项 route TLS 回归；当前三组完整回归共 `90` 项通过。
- 模拟集成：Node 子进程加载仓库真实插件并验证 `hook.loaded`、`tool.execute.before/after`，来源为 `goose-simulated`，不代表真实 OpenCode。
- 真实路由运行：`verify_llm.py` 通过，日志 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-cli-chain-20260910/verify-llm-route.log`；凭据只从明确指定的主仓库 `.env` 内存读取，没有写回任何配置。
- 真实 Goose：真实 MCP 证据已写入 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/real-cli-chain-20260910/goose-clean5/`，但没有 candidate recipe，按未完成处理。
- 真实终端扫描：8081 仍展示真实本机扫描和调查 disabled；MCP/Skill/规则/网络等未采集字段保持 `not_collected`，Hook 保持 `not_installed`。

### 安全与 review 结论

隔离 workspace 在用户打开前没有启动引擎或 receiver，不会触碰现用 Agent。8080 无监听，生产指纹库 SHA256 仍为 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2`。headless CLI 插件初始化阻塞已保留为未完成证据，不能用旧桌面实例的历史事件替代新实例验证。

本轮是状态真实性、route TLS 配置和隔离验收准备，不是 Stage1 闭环完成。尚不具备进入真实 Hook 安装与防控的条件；待用户打开 workspace 后，下一步只应绑定新 PID+create_time、启动独立 receiver 并验证新 run 的事件，再进入后续 review。


## 2026-09-10 review continuation：通用证据面与 Goose 自主调查

本轮实现提交 当前 HEAD（本轮独立提交），基线为 `246078c`，交付内容是 Goose 自主调查所需的通用、受限证据面和可审计调查生命周期。原始/解析启动路径、进程树、近旁 manifest 候选、冲突和不确定性由 `runtime/analyst_evidence.py` 提供；文件搜索和读取只在目标进程派生根目录内进行，并限制深度、数量、大小和读取字节，敏感路径与内容脱敏。Goose 负责从证据得出身份和资产结论，程序只校验结构、证据引用和目标绑定，不新增产品适配器。

状态语义：调查由 Goose 调度/生命周期记录提供；资产按 `collected`、`empty`、`failed`、`unsupported`、`unknown`、`not_collected` 区分，成功采集且为空才是 `empty`；Hook 仍来自安装 manifest 和绑定事件验证，本轮没有安装或验证，因此指纹和配方不能提升 Hook 状态。超时保存标准输出、工具调用和 evidence，结果标记 `timeout`/调查失败，不改写成 `unsupported`。

本轮全量回归为 `98` 项通过（原有 `90` 项保持通过，新增 8 项：通用证据 4、生命周期 2、原生/符号入口归属 2）。真实 Goose 当前使用隔离 run 目录 `/Users/mac/个人项目/asg-os-sensor-stage1/artifacts/stage1/goose-generic-assets-v4-20260910T083028Z/pid_1052_1789029028/`，实际完成 8 次工具调用、保存 8 份证据后于 300 秒超时，未产生候选配方；该结果按真实调查未完成报告，不以模拟结果替代。已检查本机 Goose CLI，没有可确认的原生 subagent 入口，本轮未宣称支持。

安全边界保持：8081 `http://127.0.0.1:8081/`、PID `48028` 继续使用隔离运行目录；8080 无监听；生产库 SHA256 `627c0d83b50b592a2b08a34901549402e43f36f553424e803ec4626daf07e2f2` 未变化；没有修改全局配置或现用 Agent。原有未跟踪文件 `docs/stage1/ADVISOR_REAL_HOOK_HANDOFF.md` 保留未提交。

本轮是通用调查证据与生命周期切片，不是 Stage1 闭环完成。需要重点 review：Goose 在更长预算下的停止条件和候选产出、局部证据覆盖范围、模型把配置事实映射为资产状态的准确性，以及下一轮 exact/similar/miss 与 revision 演进如何消费这些来源；Hook 安装与生效验证仍未开始。
