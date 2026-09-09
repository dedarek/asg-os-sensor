> 唯一最新日志入口：docs/stage1/WORKLOG.md（本文件）。根目录同名文件只存历史轮次概览。
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
## 2026-09-09 中断检查点 — 502 中断的未提交批次（先记录，再继续）

上一轮实现批次因推理提供商 http://127.0.0.1:10100/v1/responses 502 中断，未提交。
中断时刻 git status 为 8 改 5 增（monitor_dashboard/recipes/analyst_tools/analyzer/
identity/llm_proxy/matcher + collection/compatibility/recipe_validation/
test_goose_stage1/HOOK_INSTALL_PLAN.md），基线复跑 40 tests OK。8081 演示服务
（PID 61613, authorized-goose 隔离库）运行中（旧代码）。

顾问中途审核（待本轮落实）：
1) recipe_validation.validate 只验证证据文件存在与工具名，无 result 内容也通过；
   必须绑定本次目标 PID+create_time、真实成功结果；结构校验≠Hook 建议有证据支持；
   未知接入点不可标为已证实。
2) /api/state 实时覆盖按 PID 取值绕过 scan 的 create_time 校验；_record_
   investigation_result 结束时重新取 PID 创建时间会给复用 PID 误归属；生命周期
   必须绑定调查启动时实例 ID（running/retry/result/API），补 PID 复用负例。
3) remember_verified 演进只校验同 exe/runtime 不足判断同家族（共享 node/python）；
   similar 只是调查参考，演进需要入口/包身份等证据，不能凭模型给旧 id 就合并；
   exact 需讲清配置/依赖变更未覆盖的边界。
4) collect 只支持有限 JSON 文件和 CWD 规则，不能称资产采集完整；保留已采集成功
   项及局部失败；"No raw config ... read" 与实际 read_text 不符，应区分本地读取
   和脱敏外发。
5) WORKLOG/REVIEW 仍旧且未记录本批修改，先补中断检查点再继续。

## 2026-09-09 本轮收口（提交 81ced12, 后经中途 review 修正）
- recipe_validation.validate 强制证据绑定冻结实例 (target.pid+create_time)、真实
  成功结果、观测工具白名单；返回 {'evidence':[...], 'hook_evidence_supported':bool}。
- 生命周期绑定 instance_id=pid:create_time（running/retry/result/API 一致）；
  _record_investigation_result 不再结束时重读 psutil，PID 复用不串；补负例测试。
- remember_verified 演进门禁：同 exe/runtime 不足（node/python 共享），需可执行
  digest+入口身份（entry digest 或 entry_path）匹配；模型给旧 id 无证据即拒绝；
  classify exact 附 bounds（配置/依赖/插件/模型路由不在 exact 覆盖）。
- collect 局部失败保留成功项（全失败才 failed）；文案区分"本机读取解析字段"与
  "不外发原始内容/凭据"，移除 "No raw config ... read" 误导措辞。
- Goose 未产 recipe 时附 stderr 尾部（脱敏），区分上游 502 与 Goose 自身失败。
- 测试：全量 45 OK（8 discovery + 15+8 matcher + 6+4 status + 10+2 goose）。

## 2026-09-09 中途 review 修复（顾问复核后独立提交）
- P1 recipe_validation：hook_evidence_supported 不再由"不在负面名单"推断，恒为
  False（proposed/unverified）。结构校验通过只证明证据存在且绑定实例；无任何证据
  结构能核对 hook.method 接入点。任意外部 method（含虚构）都不得 supported。
- P1 matcher：_family_identity_matches 重写为"身份 vs 构建兼容"两段式，不把
  entry='native' 常量当身份；同路径原生直接同族；.app/.exe 包名一致允许升级演进；
  同壳不同 app 不合并；脚本入口 basename 一致允许升级；显式区分身份（允许演进）
  与 compatibility digest（决定 exact，升级后 digest 变≠ exact）。
- P1 呈现层：runtime/status.py 对 succeeded 消息保守化，历史持久化
  "接入点建议有证据支持" 一律改写为 proposed/unverified，重启不恢复虚假结论。
- P2 测试：更新 test_validate_ok_and_structure_only（恒 False + 4 种虚构 method
  负例）；新增 family_identity（同壳不同 app / 原生升级 / 共享 node 不同入口 /
  同脚本升级）与 cross_instance_reuse_no_reinvestigation 用例。
- 结果：47 tests OK（8 discovery + 39 stage1）。


## 2026-09-09 泛化性约束落实（用户最高优先, 与顾问路径一致）
- 核心代码审计结果：matcher/collection/compatibility/recipe_validation/status 全部无
  产品名特判；runtime/analyzer.py 曾有产品名单(argv_shape 打标签)已移除，改为通用
  basename 保留——"不开名单过拟合"实现落地，任何 Python 源码中不再含
  pi-coding-agent/piagent/claude-code/codex/opencode/goose 产品词组。
- 新增第二种接入机制的隔离适配测试 test_adapter_stage1.py（合成/mock，不冒充第二
  个真实 Agent 验收）：Python sitecustomize 运行时钩子机制（与 OpenCode 的 Node
  桌面机制不同）走同一核心契约——collect→validate→remember_verified→classify 复用
  与签名变化拒绝，全程无产品特判；证明核心无需改写即可更换接入配方。
- 全量 49 tests OK（8 discovery + 41 stage1）。
- WORKLOG 仍为唯一最新日志入口；REVIEW 同步更新。


## 固定协作规则（用户要求，写入 WORKLOG 作为长期约定）
- 每轮里程碑完成、最终提交、遇到阻塞或需要用户操作时，必须调用
  send_message_to_thread 给顾问任务 01a0843f-7250-7591-b63b-e46e5fa83725。
- 汇报内容必须包含：commit/工作树状态、真实与 mock 分开的证据、8081 状态、
  未完成项与需要决策的事项；调用后停 review（不要只在本地任务结束）。
- 不创建自动轮询；不做无汇报的静默结束。

## 2026-09-10 B 阶段真实验收（非纯 mock，合成元素已标注）
- 目标：最小纯观测 Hook 的隔离测试工作区闭环：安装→握手→事件→卸载。
- 真实元素：真实 python 目标进程；真实 PYTHONPATH 注入安装 sitecustomize 钩子；
  真实事件文件写盘（llm.request/llm.response 成对，adapter_source=auto-runtime-v2，
  sdk=openai-chat）；真实 stub 端点收到 1 个 HTTP 请求；卸载后同调用不再产事件。
- 合成元素（明确标注）：fake openai 包（仅 create 方法）+ 回环 stub 端点——本机
  无 openai/litellm 且离线装不上，用于激活 sitecustomize 包裹面；不冒充第二个
  真实 Agent 已验收。
- 脚本: artifacts/stage1/reuse-e2e/acceptance_loop.py（gitignore）。
- 结果: single unittest OK；B-loop request+response=1/1, stub_reqs=1。
- 未改动用户全局配置/项目、未重启真实工作实例（OpenCode 45780 原样保留）。
