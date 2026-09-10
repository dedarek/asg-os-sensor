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
