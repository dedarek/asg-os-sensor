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
