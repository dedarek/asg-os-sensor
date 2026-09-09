# Stage1 WORKLOG

## 2026-09-09 S0 — 基线核查与计划
- 基线 `f4194d3` (main, clean)，分支 `work/discovery-goose-fingerprint`，worktree `/Users/mac/\u4e2a\u4eba\u9879\u76ee/asg-os-sensor-stage1`。
- 原 8080 服务 (PID 89739, ASG_AUTONOMOUS_ANALYSIS=0) 保持运行，不触碰。
- 读完 asg_os_sensor.py / identity.py / analyzer.py / matcher.py / analyst_tools.py / runtime_analyst.yaml / llm_config.py / llm_proxy.py / stream_parser.py / preload.js / sitecustomize.py / test_discovery.py / verify_llm.py / e2e_unknown.py + README + dashboard 全量 (1249 行)。
- `test_discovery.py` 8 用例通过；openssl 确认上游证书 notAfter=Sep 9 04:59:59 2026 GMT（已过期）。
- 写 `docs/stage1/PLAN.md`（104 行）。
- 决策：`match()` 副作用是第一修复点（看板每轮调用即膨胀 match_count）；e2e_unknown 默认清库，不直接运行旧脚本。
- 未启用 `ASG_ALLOW_INSECURE_ANALYST=1`；真实 Goose 验收前复查连接条件。
## 2026-09-09 S0b — 最小可验收改动: 指纹库隔离 + 纯读匹配 + 单写者持久化
- 改动范围（独立提交，不含 exact/similar/miss 与版本演进）：
  1) runtime/matcher.py：
     - db_path() 支持 ASG_FINGERPRINT_DB 环境变量隔离指纹库（默认不变）；
     - match() 去副作用 → 纯读（不再 match_count+1 / last_seen / 隐式保存）；
     - 新增 record_hit(entry_id) 显式计数；
     - 持久化选择【跨进程文件锁 + 锁内原子读改写】：fcntl/msvcrt 排他锁覆盖
       整个 读→改→fsync→os.replace 临界区（_locked_update），线程锁叠加防同进程竞争。
       依据：原子替换只防半写文件，不能代替并发控制；读改写不整体持锁会丢更新
       （首版只锁 load/save 的并发测试实测丢计数/丢条目，重构后全过）。
  2) monitor_dashboard.py：3 处硬编码 runtime/fingerprints.json → matcher.db_path()。
  3) e2e/e2e_unknown.py：main() 重置库改用 matcher.db_path()，不再触碰生产库。
- 测试区分：
  * 原有测试（未改动，仍通过）：test_discovery.py 8 用例（unittest，覆盖发现/归属/负对照）。
  * 新增测试：test_matcher_stage1.py 9 用例
    （env 隔离、match 纯读逐字节不变、load 深拷贝、remember 新增/演进、原生二进制无
    entry_token、record_hit 显式、4 线程×25 并发计数=101、跨进程并发演进计数=4、
    跨进程并发新增 4 条不丢）。
- 结果：python3 -B -m unittest test_discovery test_matcher_stage1 → 17 tests OK。
- 未做（下一提交）：exact/similar/miss 三态、指纹/配方版本化、期望演进、UI 状态文案修正。
