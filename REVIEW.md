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
