# Stage1 WORKLOG

## 2026-09-09 S0 — 基线核查与计划
- 基线 `f4194d3` (main, clean)，分支 `work/discovery-goose-fingerprint`，worktree `/Users/mac/\u4e2a\u4eba\u9879\u76ee/asg-os-sensor-stage1`。
- 原 8080 服务 (PID 89739, ASG_AUTONOMOUS_ANALYSIS=0) 保持运行，不触碰。
- 读完 asg_os_sensor.py / identity.py / analyzer.py / matcher.py / analyst_tools.py / runtime_analyst.yaml / llm_config.py / llm_proxy.py / stream_parser.py / preload.js / sitecustomize.py / test_discovery.py / verify_llm.py / e2e_unknown.py + README + dashboard 全量 (1249 行)。
- `test_discovery.py` 8 用例通过；openssl 确认上游证书 notAfter=Sep 9 04:59:59 2026 GMT（已过期）。
- 写 `docs/stage1/PLAN.md`（104 行）。
- 决策：`match()` 副作用是第一修复点（看板每轮调用即膨胀 match_count）；e2e_unknown 默认清库，不直接运行旧脚本。
- 未启用 `ASG_ALLOW_INSECURE_ANALYST=1`；真实 Goose 验收前复查连接条件。
