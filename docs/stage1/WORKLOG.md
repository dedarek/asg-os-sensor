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

## 2026-09-09 S0c — 修复提交(review P1/P2)
- P1-1 E2E 默认隔离: e2e/e2e_unknown.py 新增 make_run_root(), 每次运行创建
  独立目录 e2e/artifacts/unknown-runtime/<ts>-<uuid>/ 并把 ASG_FINGERPRINT_DB
  指向该目录下的新库; main() 不再 rmtree(RUN_ROOT), 不再写/清空任何指纹库文件。
  子进程(sensor/goose 扩展)经 env 继承同一隔离库。
- P1-2 损坏库禁止静默清库: runtime/matcher.py 的 load()/ _locked_update() 仅
  FileNotFoundError 才初始化空库; JSON 损坏或读取失败一律 raise 且不落盘,
  原文件逐字节保留。record_hit() 未命中返回 _NOCHANGE 哨兵, _locked_update
  检测到未修改则不写盘(修复前未命中也会整库重写)。
- P2 Goose prior 隔离: runtime/analyst_tools.py load_prior() 的默认库 fallback
  改走 _prior_db()(即 matcher.db_path(), 读取 ASG_FINGERPRINT_DB); 子进程
  继承 env 获得同一配置, 不会读到另一份历史。
- 测试(区分): 原有 test_discovery.py 8 用例未改动; test_matcher_stage1.py
  现 15 用例: 上轮 9 + 本轮新增 6 (未命中 mtime/内容均不变、损坏库 load/
  record_hit 均 raise 且原文件保留、缺失库惰性初始化(纯读/未命中不建文件)、
  spawn 跨进程并发新增 4 条不丢、analyst_tools prior 指向隔离库、
  e2e make_run_root 两次调用目录不同且生产库 mtime/内容不变)。
- 全量: python3 -B -m unittest test_discovery test_matcher_stage1 → 23 tests OK。
- 已知限制: msvcrt(Windows)锁分支本机无法执行, 仅静态审查; 跨进程锁语义由
  POSIX flock + spawn 测试覆盖。未运行旧 E2E(需真实 Goose 与网络, 且本机 TLS
  证书过期); 原生二进制正例通过不代表兼容性判断已完成。

## 2026-09-09 S0d — 修复提交(review: 脚本启动导入路径 + 子进程 MCP 隔离验证)
- P1 脚本启动导入路径: runtime/analyst_tools.py 在 ROOT 定义后把项目根挂入
  sys.path(仅当未包含), 保证 `python runtime/analyst_tools.py` 启动时
  `from runtime import matcher` 可用; _prior_db() 移除宽泛 try/except,
  显式指定隔离库后任何导入/读取错误都会显式报错, 禁止静默回退默认库。
- P2 子进程 MCP 回归测试(测试进程内 import 无法覆盖的问题):
  * test_subprocess_mcp_prior_isolated_db: 去 PYTHONPATH 真实脚本启动,
    ASG_FINGERPRINT_DB 指向带 ISOLATED-DB 标记的临时库, 走 MCP initialize +
    tools/call get_prior_recipe, 断言返回 path 与 value.marker 均为隔离库内容,
    且生产库 mtime/内容不变。
  * test_subprocess_mcp_prior_default_path_without_pythonpath: 无 PYTHONPATH、
    未设隔离库时读到默认库(导入路径修复生效), 而非导入失败回退。
- .gitignore: 新增 `e2e/artifacts/unknown-runtime/[0-9]*` 精确忽略时间戳 run-id
  目录(已验证 git check-ignore 命中、旧 first/second 证据不受影响);
  测试子进程产生的 e2e/artifacts/evidence 与 analyst_tool_calls.jsonl 已清理。
- 全量: python3 -B -m unittest test_discovery test_matcher_stage1 → 25 tests OK
  (原有 8 + test_matcher_stage1 17: 上轮 15 + 本轮新增 2)。
## 2026-09-09 中断检查点 + 本轮收口 → 独立提交
- 中断检查点: 上一轮实现批次在推理提供商 http://127.0.0.1:10100/v1/responses 502 时中断,
  当时已有 8 改 5 增(调查/采集/校验/生命周期)未提交, 基线复跑 40 tests OK。
  本轮先补记录, 再按顾问意见收口, 独立提交。
- 本轮修复:
  1) recipe_validation: validate() 只验证证据文件存在与工具名的漏洞已修——
     现在强制 证据绑定调查启动时冻结的实例 (target.pid + target.create_time)、
     结果必须含真实数据(占位 status/message 不算)、观测工具白名单,
     跨实例/PID 复用的历史证据一律拒绝; 返回 dict 区分 evidence 与
     hook_evidence_supported(结构校验通过 ≠ Hook 建议有证据支持;
     method=unsupported/unknown 等不算已证实接入点)。
  2) 生命周期实例绑定: INVESTIGATING_PIDS/investigation 结果与 retry 从 pid 键
     改 instance_id = pid:create_time, _record_investigation_result 不再在结束时
     重新读 psutil(避免复用 PID 误归属); scan 与 /api/state 均按 instance_id
     查询, PID 复用负例测试覆盖 result/running/retry 三路不串。
  3) matcher: remember_verified 演进门禁 -- 只同 exe/runtime 不视为同家族
     (node/python 共享解释器), 需可执行内容 digest + 入口身份(entry digest 或
     entry_path)之一匹配才允许演进; 模型随便给旧 id 不通过证据校验即拒绝合并;
     classify exact 附 bounds 说明覆盖边界(配置内容/依赖版本/插件/MCP/模型路由
     不在 exact 覆盖内), 避免过度宣称。
  4) collection: 局部失败保留已成功项(全部失败才 failed), limitations 文案
     区分"本机读取解析字段"与"不外发原始内容/凭据", 不再出现
     "No raw config ... read" 的误导性说法(实际代码 read_text 读取本地文件)。
  5) 失败归因: Goose 未产 recipe 时附带 stderr 尾部(脱敏), 便于区分上游 502
     与 Goose 自身失败, 不再只报 returncode。
- 测试区分:
  * 原有(未改动): test_discovery.py 8 用例。
  * 新增/更新(本轮): test_goose_stage1.py(证据绑定 2、unsupported hook 1、
    演进门禁 1、collect 局部失败 1、proxy 1 等) + test_status_stage1.py
    (PID 复用不串生命周期、result 用冻结 create_time)。
  全量 45 tests OK: python3 -B -m unittest test_discovery test_matcher_stage1
    test_status_stage1 test_goose_stage1
- 已知限制: Windows msvcrt 锁分支无法本机执行(静态审查); 真实 Goose 本轮是否重跑
  取决于 LLM 密钥与授权 TLS(ASG_ALLOW_INSECURE_ANALYST), 见 REVIEW。
 ## 2026-09-09 中断检查点 + 本轮收口 → 独立提交
- 中断检查点: 上一轮实现批次在推理提供商 http://127.0.0.1:10100/v1/responses 502 时中断,
  当时已有 8 改 5 增(调查/采集/校验/生命周期)未提交, 基线复跑 40 tests OK。
  本轮先补记录, 再按顾问意见收口, 独立提交。
- 本轮修复:
  1) recipe_validation: validate() 只验证证据文件存在与工具名的漏洞已修——
     现在强制 证据绑定调查启动时冻结的实例 (target.pid + target.create_time)、
     结果必须含真实数据(占位 status/message 不算)、观测工具白名单,
     跨实例/PID 复用的历史证据一律拒绝; 返回 dict 区分 evidence 与
     hook_evidence_supported(结构校验通过 ≠ Hook 建议有证据支持;
     method=unsupported/unknown 等不算已证实接入点)。
  2) 生命周期实例绑定: INVESTIGATING_PIDS/investigation 结果与 retry 从 pid 键
     改 instance_id = pid:create_time, _record_investigation_result 不再在结束时
     重新读 psutil(避免复用 PID 误归属); scan 与 /api/state 均按 instance_id
     查询, PID 复用负例测试覆盖 result/running/retry 三路不串。
  3) matcher: remember_verified 演进门禁 -- 只同 exe/runtime 不视为同家族
     (node/python 共享解释器), 需可执行内容 digest + 入口身份(entry digest 或
     entry_path)之一匹配才允许演进; 模型随便给旧 id 不通过证据校验即拒绝合并;
     classify exact 附 bounds 说明覆盖边界(配置内容/依赖版本/插件/MCP/模型路由
     不在 exact 覆盖内), 避免过度宣称。
  4) collection: 局部失败保留已成功项(全部失败才 failed), limitations 文案
     区分"本机读取解析字段"与"不外发原始内容/凭据", 不再出现
     "No raw config ... read" 的误导性说法(实际代码 read_text 读取本地文件)。
  5) 失败归因: Goose 未产 recipe 时附带 stderr 尾部(脱敏), 便于区分上游 502
     与 Goose 自身失败, 不再只报 returncode。
- 测试区分:
  * 原有(未改动): test_discovery.py 8 用例。
  * 新增/更新(本轮): test_goose_stage1.py(证据绑定 2、unsupported hook 1、
    演进门禁 1、collect 局部失败 1、proxy 1 等) + test_status_stage1.py
    (PID 复用不串生命周期、result 用冻结 create_time)。
  全量 45 tests OK: python3 -B -m unittest test_discovery test_matcher_stage1
    test_status_stage1 test_goose_stage1
- 已知限制: Windows msvcrt 锁分支无法本机执行(静态审查); 真实 Goose 本轮是否重跑
  取决于 LLM 密钥与授权 TLS(ASG_ALLOW_INSECURE_ANALYST), 见 REVIEW。
 