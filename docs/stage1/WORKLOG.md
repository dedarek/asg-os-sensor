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

## 2026-09-10 A 阶段证据纠正（不再把"未观测"当"未加载"/"已加载"）
- NodeService 引擎证据（实测）: 45780 子进程 46482 命令行含
  --utility-sub-type=node.mojom.NodeService，运行用户目录数据
  --user-data-dir=.../Application Support/ai.opencode.desktop —— 引擎在 Electron
  的 NodeService utility 进程中，非独立 CLI。
- 配置加载链证据（实测）: 进程实际打开 Application Support/ai.opencode.desktop
  （drafts.sqlite/Local Storage/leveldb），未观测到打开 ~/.config/opencode/
  opencode.jsonc 或 plugins/ 或 /Users/mac/CLAUDE.md。注意: 读取后关闭 FD 常见，
  "未观测到打开"不能证明"未加载"，只能作为未观测证据记录；CLAUDE.md 存在(379B)
  作为文件存在证据，不构成生效规则。
- 插件机制证据（实测）: grep app.asar 全部 out/main、out/preload、sidecar.js ——
  不含 tool.execute.before / .opencode/plugins / plugin / hook / config.json 字符串。
  结论: 桌面 1.18.25 包内无官方文档描述插件扩展点的实现证据；.opencode/plugins
  仅是设计文档接口，本机包无该机制字符串。待确认适用于本机引擎版本的加载机制。
- 因此真实 Hook 接入点尚未证实；先记录 观测面（NodeService 引擎、Application
  Support 配置、CLAUDE.md 存在）与合成验证，不宣称真实安装器/握手已完成。

## 2026-09-10 合成 SDK 钩子集成测试（纳入版本管理, 可复现）
- 文件: test_synthetic_hook_integration.py（根目录, 版本管理内; 旧 gitignored
  artifacts 版已废弃）。
- 修复（按 review）：每次 temp 目录 + secrets 随机 nonce；finally 关闭 socket；
  安装/卸载两次运行均断言 returncode==0；stub 收到 2 次请求（安装后+卸载后）,
  卸载后事件文件为空 → 证明无事件非因调用失败假通过；fake openai SDK 包在 temp
  动态生成，fixture 纳入版本管理（测试内 build_fake_sdk）。
- 结果: synthetic hook integration OK: request+response=1/1, stub_reqs=2。
- 定位: 仅合成 SDK Hook 集成测试，不称真实 Agent B 阶段验收；真实 OpenCode
  Hook 尚未安装。

## 2026-09-10 A 阶段结论修正（依据实测，区分"已找到/未找到"证据）
- 修正1：此前"asar 无字符串→桌面未实现插件机制"是错误推断。引擎 bundle
  out/main/chunks/node-Cuna2N2U.js (33MB) 内实际包含：tool.execute.before ×9、
  .opencode ×41、plugin(s) ×656、hook ×329；插件加载 Glob.scan("{plugin,plugins}/
  *.{ts,js}", cwd=configDir) + pathToFileURL 动态导入；plugin 契约 tool.execute.before
  (input, output) 可改 output.args、config(cfg) 改合并配置（与官方文档一致）。
  结论改为"在已检查范围内找到插件实现证据"，不再说"未实现"。
- 修正2：NodeService 参数只证明 Electron Node 服务进程；它 fork 的 sidecar.js
  加载上述 chunk 的 Server —— 证据链为 main/index.js -> utilityProcess.fork
  (sidecar.js) -> import chunks/node-Cuna2N2U.js Server。引擎确实运行在 NodeService。
- 配置链：globalConfigFile() 搜索 opencode.jsonc/opencode.json/config.json；
  支持 OPENCODE_CONFIG_DIR 环境变量（config: Flag.OPENCODE_CONFIG_DIR ??
  Path.config）与 OPENCODE_CONFIG=<file> 追加配置 —— 官方隔离配置入口。
- 未知保持未知：桌面 app 无 CLI 入口（which opencode 无）；启动仅处理 opencode://
  协议与文件选择器；插件需在应用打开工作区时由引擎加载。

## 独立测试工作区方案（B 阶段真实接入，尚需用户操作）
- 引擎/插件证据已明确；缺失的关键一步是"用户通过桌面 UI 打开一个独立测试工作区"
  —— 桌面实例不接受 CLI 项目参数，插件在打开工作区时加载。自动化无法替代该 UI
  操作；将隔离工作区目录 + .opencode/plugins/asg-observe.js（写入该工作区, 非全局）
  作为插件搜索路径（cwd=configDir 或工作区 .opencode）。
- 影响评估：插件仅写入独立测试工作区目录；不触碰用户全局配置、不修改 app.asar、
  不重启真实工作实例（OpenCode 45780/桌面进程未动）。打开工作区需用户最小操作。
- 需用户决策：是否在桌面打开测试工作区；打开后若引擎/会话未加载插件，由用户重启
  该工作区引擎（非全局应用重启）。此操作需顾问/用户确认后实施，不擅自执行。
- 合成 SDK 集成测试保持独立（可复现、随机 nonce、双次请求、卸载验证），不冒充真实
  验收；其随机文件名不是实例握手能力——真实接入仍需 nonce+PID/create_time 握手、
  事件关联与 API。


## 2026-09-09 隔离验收准备完成（HEAD 5d37c8c）
- runtime/opencode/asg-observe.mjs：纯观测插件（tool.execute.before/after +
  hook.loaded）。只记录 ts/event_type/adapter_source/nonce/pid/call_id/tool/
  outcome；不记录参数内容与密钥；fail-open 不影响工具执行。
- runtime/opencode/event_api.py：EventVerifier 接收端验证 nonce + psutil
  (pid).create_time() 与预期快照比对（拒绝错误 nonce/PID 复用/缺 pid），
  健康 = hook.loaded 且绑定有效；未握手不显示已安装生效。
- runtime/opencode/ghost_install.py：plan/preflight/install/uninstall 隔离工作区
  （默认 artifacts/stage1/opencode-observe, gitignore）；幂等拒绝覆盖、记录
  sha256+nonce、卸载回滚；preflight 打印全局配置隔离局限与 UI-open 需求。
- 测试：test_opencode_plugin.py(+ .js fixture) 真实 node 子进程加载插件，
  <1s 通过，进程清理；无参数/密钥泄漏、字段白名单、绑定正反例、健康、
  卸载无事件。全量 51 tests OK。
- 待用户步骤（真实加载触发）：在桌面打开隔离工作区；若引擎未加载插件由用户
  重启该工作区引擎（非全局）。未打开 UI、未重启/终止现有 Agent、未改全局配置
  或现有项目、未部署到真实实例。

## 2026-09-09 隔离验收准备第二轮修复（评审 6 点逐条落实）
1) ghost_install ROOT 改为向上查找含 runtime/opencode/asg-observe.js 的仓库根
   （任意 cwd 可用），已在 /tmp 实测真实 CLI install/preflight/uninstall。
2) 插件由 .mjs 改为 .js（CJS）：引擎 glob"{plugin,plugins}/*.{ts,js}" 才会发现；
   测试用与引擎一致的 glob（展开 ts/js）验证插件被扫描发现，不再用 node 直接
   import 掩盖加载路径。
3) 安装不再只打印 nonce：nonce 与事件文件写入插件同目录 .asg-observe/
   （nonce、events.jsonl, 0600），用户打开工作区即可加载——不依赖启动脚本 env，
   不要求全局重启。事件文件权限强制 0600。
4) install/uninstall 加固：name 仅允许纯文件名（拒绝绝对路径/../ 穿越）；
   workspace 必须存在且为目录；拒绝 symlink；插件文件用 O_CREAT|O_EXCL 原子
   独占创建（已存在拒绝覆盖、幂等）；持久 manifest（name/sha256/nonce/
   workspace/installed_at）记录；卸载校验 manifest+hash，修改后拒绝删除。
5) 事件契约最小化：hook.loaded 不再输出 directory；事件仅 ts/event_type/
   adapter_source/nonce/pid/call_id/tool/outcome；无参数内容与密钥。
6) EventVerifier 区分 loaded_observed 与 current_health：health 要求新鲜
   （ttl）、绑定有效、有关联事件；revoked/stale 不健康；api_status 明确
   为库且未接线（HTTP/events not_wired）。
- 测试：test_opencode_plugin.py 重构为引擎长驻 Popen + 真实 glob 发现 + 绑定
  正反例 + 健康语义 + 卸载保留历史；全量 51 tests OK。

## 2026-09-09 第三轮：3 组闭环阻断修复（HEAD caa2835 后）
1) 事务化安装：独立 run 目录 + manifest 原子发布（.asg-observe/manifest.json 为 active 指针，
   runs/<runid>/{nonce,events.jsonl} 0600, 目录 0700）；任一步失败回滚本 run（无残留有效插件）；
   install->uninstall->reinstall 可重复且 runid 不同；旧历史（events.jsonl）保留。
2) 目录安全：对 workspace 及所有路径组件做 symlink 检查；仅接受授权隔离根（系统临时目录或
   artifacts/stage1/**）；name 纯文件名；O_CREAT|O_EXCL 独占创建；卸载校验 manifest+hash 后拒绝删除；
   外部 sentinel 文件保留负例（.opencode symlink 指向外部目录时拒绝安装且不触碰目标文件）。
3) 健康：current_health 先 schema+绑定过滤（旧合法 loaded 不能掩盖新非法 nonce/未来 ts）；
   撤销由安装器 manifest 驱动（active=False/缺失 -> revoked）；空闲无事件 -> unknown 非故障；
   read_raw 稳健拒绝数组/数字；loaded_observed 与 current_health 分离。
4) HTTP 接线：新增 runtime/opencode/server.py（127.0.0.1 随机端口）暴露 /health /events，
   把已验证 event/health 接到隔离 HTTP 接口，不再停留于库对象。
5) 插件：nonce 缺失（卸载后）立即停止写事件，不重启引擎的再次触发也不产事件（同进程内验证）。
- 测试：test_opencode_plugin.py 扩展为 10 用例（事务 4+事件 2+健康 3+HTTP 1），全量 60 tests OK。