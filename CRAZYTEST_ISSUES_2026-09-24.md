# CRAZYTEST 问题清单（2026-09-24 夜间审查，只测不改）

审查范围：/Users/mac/个人项目/asg-os-sensor-advisor-20260911，分支 beta/asg-soc-direct-20260921（含 18 个文件的未提交改动，约 +822/-61 行）。
方法：全量测试 4 分片并行、ruff 静态扫描、逐文件人工 review、对可疑路径做最小复现。全程未修改任何源代码。

## 0. 全量测试结果

- 86 个测试文件 / 561 个用例：559 通过，2 失败，0 错误。
- 失败 1：test_adapter_stage1.py::test_alternate_mechanism_reject_signature_change — 断言 similar，实际 trial。
  原因：工作区把同家族构建变化从 similar 升级成 trial（未提交改动），这个旧测试没同步更新；同类 test_goose_stage1.py 已改成期望 trial。属于测试欠账，不是功能回归，但必须修，否则全量门禁常红。
- 失败 2：integrations/soc_inventory/test_g07_investigation_lifecycle.py::test_investigation_requested_once_until_package_exists — TypeError: lambda() takes 1 positional argument but 2 were given。
  原因：confirmed(state) 改成 confirmed(state, conversation_proof=None)，测试里的 mock side_effect 只接收 1 个参数。同样是未提交改动带来的测试欠账。

## 1. 崩溃级 / 高严重度

### B1 tools/soc_collector_service.py:49 — install 必炸（UnboundLocalError）
main() 里第 52 行有 ROOT=runtime 赋值，使 ROOT 成为局部变量；第 49 行 shutil.copytree(ROOT/package, ...) 先读后赋，任何平台执行 soc_collector_service.py install 都会 UnboundLocalError。已用 AST + 同构最小复现确认（ROOT is local in main: True，同模式 UnboundLocalError confirmed）。
影响：采集器安装/升级路径全坏（当前机器上跑的 0.9.40-beta 是旧包装的，掩盖了这一点）。
修法建议：局部变量改名（如 staged_root），或删掉第 52 行的 ROOT 重赋值、WorkingDirectory 直接用 runtime。

### B2 runtime/hook_data.py capture_matrix — 两条真实数据可触发的崩溃路径（/api/hook-data 直接 500）
已最小复现（直接调用 capture_matrix）：
- 崩溃点 1（约 1052-1057 行 model_used 归并）：latest_content = latest_payload.get("content") or {} 之后直接 .get("model")。真实 hook 事件里 user.input 的 content 经常是字符串（UserPromptSubmit 的 prompt 直接写进 content 字段，见 codex hook.cjs 与 WorkBuddy 适配层）。窗口里只要最近一条带模型元信息的事件的 content 是字符串，整个 snapshot() 抛 AttributeError。
- 崩溃点 2（约 1085-1086 行 history_context 兜底）：(row.get("payload") or {}).get("content") 假定 payload 是 dict；capture_matrix 被以任意 records 直接调用时（单测/未来调用方）payload 为字符串即崩。
影响：8081 页面输入输出与工具原始事件可能整窗 500；SOC 桥接读取同样失败。这条未部署到运行版本（release 0.9.40-beta 里没有 capture_matrix），部署后才会咬人。
修法建议：latest_payload/latest_content 各加 isinstance dict 守卫；history_context 兜底同样守卫 payload。

### B3 runtime/fingerprints.json — 配方文件哈希与内容不一致（复用安装必然 precondition 失败）
脚本扫描全部 install_plan.files：19 处 expected_sha256 与 content 现值不一致（如 codex hook.cjs 两处：exp=9b3db51f… 实际内容 ad916856…；fingerprint[5]/[10] 各 revision 全部错位）。原因：直接手改配方里的 content，没同步 expected_sha256，也没走生成/打包链路回写。
影响：learned_install.py:141 以 digest(before)!=expected_sha256 作为前置条件，这些配方再复用安装时报 precondition changed，把正常环境误判成损坏；这正是用户点名“必须写回运行配方、包与摘要绑定同一版本”的旧病，仍在活体指纹库里。
另：codex hook.cjs content 里硬编码本机路径 /Users/mac/个人项目/…、/Users/mac/.codex/asg-observer，跨机器直接不可复用，打包阶段必须参数化（此前已列为待办，未完成）。

## 2. 中严重度

### B4 integrations/soc_inventory/event_spool.py:flush — 分组循环里的 return 应为 continue
按 instance 分组发送时，任一组网络失败或 accepted!=True 直接 return，同 agent 其余实例（含更新实例）的事件被留到下一轮。行为算保守不丢数据，但旧实例洪峰会饿死新实例组；且 if not selected: return 在循环内语义可疑（应为 continue）。

### B5 integrations/soc_inventory/discovery.py:482-493 — existing_hook_observed 是终点态
当前实例已有 Hook 会话证据时写入 status=existing_hook_observed 并永久 continue，之后 SOC 就算发布了该类型的正式安装包，也不会再给这个实例装（阻断能力永远不会自动补上）。需要“后续出现兼容包时重新评估”的出口。

### B6 runtime/soc_command_protocol_hook.py:96-115 — 显式 deny 可被一个字段旁路 + 超大输入静默阻断
- explicit_deny = decision.get("decision")=="deny" and decision.get("control_error") in (None,"")：控制服务返回 deny 且带任何非空 control_error 时，deny 被无视放行。若控制服务用 control_error 携带附加信息而决策仍是 deny，安全决策丢失。
- 主逻辑之前：stdin 超过 1MB 直接 return 2 且无 stdout——在 Claude 风格 hook 语义里 exit 2 即阻断。observe-first 策略下超大输入应当放行并记录，而不是静默拦截。
- except 分支里 allowed=True 随后又被 explicit_deny 重算覆盖，属死代码，易误导。
- 严重度补充：grep 确认当前服务端与 hook_control_client 均不返回 control_error 字段，所以该旁路目前是潜在隐患而非现网缺陷；但一旦上游给 decision 加上同名字段（携带告警文本），deny 会静默失效，仍应修。

### B7 monitor_dashboard.py:156 — set_scan_interval 会隐式开启周期扫描
if not SCAN_ENABLED: SCAN_ENABLED = True——用户只想改间隔数值，却把默认关闭的周期扫描打开了，与页面复选框状态互相矛盾（复选框显示关，实际在跑）。应当只由 scan_enabled 开关控制。

### B8 monitor_dashboard.py GOOSE_MAX_TURNS 40→120（未提交改动）
成本上调 3 倍没有另行确认；用户明确要求过控制 Goose 成本。建议回归 40 或按配方分级，需在交付说明里显式标注。

### B9 指纹 revision 只增不减
matcher.remember_verified 每次追加 revision，无任何修剪/归档（grep 无 trim/prune）。用户此前拍板过保留最近 N 条 + 近 90 天 exact 命中保留的策略，未实现。fingerprints.json 已见单 entry 7 个 revision。

## 3. 低严重度 / 卫生

- ruff F/E9/B 全仓 55 条：F401 未用 import 约 30 处（runtime/acp_bridge.py、runtime/collection.py、integrations/soc_inventory/endpoint.py、tools/soc_collector_service.py、monitor_dashboard.py:26/38/2335 等）；F841 死赋值 7 处（monitor_dashboard.py:1373 hook_evidence_supported、runtime/autonomous_pipeline.py:82 等）；B023 循环变量捕获 5 处（e2e/verify_latency_metrics.py、e2e/capture_target_baselines.py——验收脚本里是真 bug 温床）；B905 zip 无 strict（test_protocol.py:25）。F821/F811 全干净。
- e2e/verify_latency_metrics.py 的 B023：闭包捕获循环变量 sid/canary/sent，延迟测量可能把错误 canary 记到错误会话，历史延迟报告的数字可信度存疑。
- runtime/fingerprints.json.bak-observe-1790130365 未跟踪备份文件留在仓库根，应清理或进 .gitignore。
- monitor_dashboard 周期扫描下限 1 秒（_validate_scan_interval 1..86400），SOC 网关侧口径是 10..86400，两端不一致，1s 会疯狂扫进程表。

## 4. 状态/展示疑问（需运行态复核，不算代码缺陷）

- 8081 /api/state 当前仍列 ZCode instance 999:1789783801，PID 999 已不存在；分类仍是确认 Agent。历史保留是设计，但候选卡片仍显示待处理，页面无已退出标识（本次 state 输出里 alive 字段为 None，前端取不到）。
- WorkBuddy 18892 score=0 但 match=exact 且 classification=pending——分数与会话证据脱钩，展示口径待统一（guide 的 P0 计数口径问题）。

## 5. 未提交改动的整体风险

trial 晋级、capture_matrix、conversation_proof、event_spool 分组、扫描开关这五坨核心逻辑全部只存在于未提交工作区；release 0.9.40-beta 不含 capture_matrix 和 trial 逻辑。也就是说页面上跑的版本和仓库里审查的版本行为不同，任何验收结论必须注明用的是哪一份代码。

## 6. 建议修复顺序（供 06:00 后 cryzytest 分支执行）

1. B1（一行改名，恢复安装能力）
2. B2（三处 isinstance 守卫）
3. 两个失败测试欠账
4. B6 安全语义
5. B4 return→continue
6. B7 隐式开关
7. B3 重新生成配方哈希（走打包链路，勿手改）
8. ruff 卫生项批量清理（--fix 安全的 34 条）
9. B9 revision 修剪
10. B5 出口态
## 7. 第二轮审查追加（02:05，仍只测不改）

### B10 integrations/soc_inventory/endpoint.py:flush — 毒消息永久阻塞该 agent 队列
flush 按 rowid 顺序 LIMIT 1 发送，任何 HTTPError（包括 4xx 永久拒绝）都 break 并保留。一条被网关持续 400/422 拒绝的消息会永远卡死同一 agent 后续所有上报，没有重试上限、没有死信队列。建议：4xx 计数超限后移入 dead_letter 表并继续发送后续消息。

### B11 endpoint.py run() — 双线程并发写同一 sqlite
inventory 循环与 runtime_bridge 线程各自连接 outbox.sqlite 并发写，高峰期可能出现 database is locked 造成一拍漏报。低风险，应在日志显式区分锁冲突。

### B12 runtime_bridge._runtime_cycle — 体积裁剪循环反复全量序列化
超过 3.5MB 的报告会反复 canonical() 全量 payload 再丢弃 1/4 记录，最坏约 10 次全量 JSON 序列化/轮，桥接线程每 15 秒一次。仅性能，无正确性问题。

### B13 discovery.conversation_proof — 每个低分候选最多阻塞 15 秒 HTTP
proof 在 refresh() 里逐个候选同步请求 8081 /api/hook-data（timeout=15）。候选多且 8081 卡顿时一轮 discovery 可被拖成分钟级。建议并行或降 timeout 到 3 秒。

### B14（存疑，需运行态验证）install 命令过期语义
_onboarding_target(pid) 只认当前扫描结果里的 pid；实例重启换 instance 后，SOC 下发的 install 命令会 400 invalid_request，同 id 命令不再重试。行为符合不自动重试安装的设计，但 UI 应区分命令已过期需重新下发，目前没有区分。

以上五项均为中低严重度。B1-B3 仍是 06:00 后优先修复项。

## 8. 第三轮追加（02:07）

### B15 两套 command Hook 运行时安全默认值相反（fail-close vs fail-open）
runtime/command_protocol_hook.py（纯逻辑层，ASG 自用路径）在控制客户端出错时 allowed=False，即 fail-close 拒绝；runtime/soc_command_protocol_hook.py（SOC 直装包路径）同一情形改为 observe-only 放行。两条安装路线现在对同一故障给出相反答案：Codex 自锁事故正是 fail-close 造成的，observe-first 是有意的产品决定，但纯逻辑层没有同步，测试也各自锁死了各自的行为。风险：从 ASG 路径装的 Hook 在控制服务抖动时仍会全量拦截（用户已经受过一次罪）；从 SOC 包路径装的则放行。必须统一成同一个可配置策略（observe/enforce 两档，随配方下发），否则同类事故还会复发。

### B16 hook_control.decide 的 ask 等待占用 RLock 条件变量但有 60 秒上限——OK；不过 _PENDING 修剪发生在锁内 list() 全量拷贝，500 条上限下开销可接受，仅记录。

第三轮未发现新的崩溃级问题。文档至此覆盖：崩溃级 3、中 9、低/卫生若干、运行态待验证 2。等待 06:00 进入分支修复阶段。

## 9. 关键验证结论（02:06）

把已提交的 HEAD（9fb2637）克隆到 /tmp 单独跑 test_adapter_stage1 与 test_g07_investigation_lifecycle：3 项全部通过。
结论：2 个全量失败完全由工作区未提交改动引入（trial 状态升级、confirmed() 加参），已提交的 beta 分支是全绿的。任何“仓库现在不能用”的判断都应以工作区为准，任何“可以发布”的判断也绝不能拿 HEAD 绿灯当依据——未提交的 trial/capture_matrix/conversation_proof 三大功能与这两处测试欠账是同一批改动，发布前必须一起处理。

## 10. 复跑与服务面补充（02:07）

- 全量套件第二遍（单进程顺序跑）：仍是同样 2 个失败、559 通过——无 flake，无顺序依赖，结论稳定。
- runtime/hook_service.py 独立 Hook 服务默认绑定 0.0.0.0（DEFAULT_HOST），远程控制默认关（loopback only），但一旦设 ASG_HOOK_REMOTE=1，任何能达端口的进程都拿到 bearer 认证面。token 是本机 0600 文件，泄漏面小；建议 DEFAULT_HOST 改 127.0.0.1，远程需求显式配置。低-中。
- service.py / hook_runtime.py / deploy.py 审查通过：进程身份用 pid+create_time 双校验，Goose 下载走官方 release + SHA256 校验 + 归档路径逃逸检查，无新发现。
- asg_os_sensor.py（旧 PoC 传感器）与新链路并行存在，属遗留演示件，文档第 5 节“版本混杂”风险同样适用于它——交付时应标注哪些入口是当前有效面。

## 11. 第四轮追加（02:15，release 链路 + 前端 + 测试审查，仍只测不改）

### B17（中高，发布阻塞）交付包原样打包本机指纹库，63 处 /Users/mac 路径进包
release/build_release.py 的 APP_ENTRIES 整目录拷 runtime/（ignore 只排 pyc/test_/bak/lock），runtime/fingerprints.json 内实测 63 处本机绝对路径：hook_control_client.py 路径、artifacts/autonomous-demo/workspace、~/.dsh/profiles/asg-e2e-clean-web-20260921、调研中文备注等。manifest_files 只记 SHA-256，无任何路径过滤或占位符替换。后果：别的机器装上后 learned 配方指向不存在文件，复用安装必 precondition 失败——B3 只是哈希问题，这条是内容本身不可移植。README 宣称"兼容新实例复用已验证配方"，当前包兑现不了。用户已明确要求"发布为 SOC 通用安装包前必须由打包阶段替换"，打包阶段现在没有这一步。修复应放 build_release 拷贝后：扫描 runtime/fingerprints.json 与 recipes/*.yaml，含本机 home 路径前缀的配方要么剔除要么参数化，并在 release.json 记录剔除清单。

### B18（中低）service_main.py reap_orphans 用子串匹配进程命令行，路径存在包含关系时误杀
reap_orphans 判定"命令行包含 str(home) 且含 monitor_dashboard.py/endpoint 即孤儿"。home 为 /tmp/asg 时，/tmp/asg-worker、/tmp/asg-e2e-* 等并存安装（e2e 常这样做）的子进程命令行同样包含 "/tmp/asg"，会被正规安装的服务收割。注释"the path is per-home unique"只对默认用户级路径成立，对临时目录并存不成立。建议匹配 " --home " + str(home) 加边界符或核对进程实际 exe 路径。

### B19（低）supervisor 的 scan_interval 只在 spawn 时注入环境变量
child_env 读 config.scan_interval 只在启动子进程时用；运行期通过 /api/scan-interval 改的是引擎自身状态，supervisor 的 engine_probe 每拍重读 /api/state 所以判定没问题，但 endpoint 引擎重启后会被重置回 config 值，页面设置与重启后实际值可能不一致。与第 4 节状态刷新类问题同类，修 B7 时一并考虑。

### B20（低）dashboard 抽屉 details 展开态恢复按 summary 文本匹配
loadHookData 每 2s 重渲染，展开态用 details 的 dataset.recordKey 或 summary.textContent 恢复；recordKey 缺失时（老数据无 instance_id/line）多条同标题记录会一起被开/关。仅视觉噪声。

### 本轮审查通过项
- release/asgctl.py 生命周期：包摘要逐文件校验、staging+displaced 原子替换、失败回滚 displacement，未见缺陷。
- service_main.py 心跳：pid 核对防止旧 endpoint 状态冒充新 endpoint，engine 停滞判定用三周期+宽限，合理。
- integrations/soc_inventory/test_channel_and_budget.py：channel 打标、预算裁剪、失败隔离、原生包兼容性（fail-closed、executable/version/min_version pin、解释器包 observed digest）断言具体且方向正确，质量高。
- web/dashboard.html 整体有 escapeHtml 基线、findings 空态不补造事件、hookDataTimer 关闭时清理，未见 XSS 级问题。

文档累计：崩溃级 3、中高 1（B17）、中 9、低/卫生若干、运行态待验证 2。06:00 后修复顺序建议在第 6 节基础上把 B17 插到 B3 之后（同一批配方工作，一起做省一轮全量测试）。
## 12. 第五轮追加（02:20，SOC 清点链路 protocol/store/collector 深审，仍只测不改）

### B21（低-中）protocol.py 通用平台工作区配置固定按 mcp_servers 字段读取，真实 mcpServers 写法会被报成"0 个"
roots() 的 else（未知平台）分支对 cwd 下 config.toml/config.json/settings.json/mcp.json 一律附加 field='mcp_servers'；而 mcp_scope 按 field 逐段 get，取不到就返回空 items、status=success。多数 Agent 用 mcpServers 驼峰写法，结果是"已核实范围、0 个 MCP"的假阴性，与 guide"未知数量不冒充零"的精神相悖。同一函数里 env 声明根已经用 _first_mcp_field 按结构探测，工作区文件却没用——修复：工作区通用文件也走 _first_mcp_field。

### B22（低）discovery.py:380 pending-enrollment 清点没传 collection_home
endpoint.py:141 注册后清点传 home=agent.get('collection_home')，discovery.py 首次 pending 清点只传 env；collect_contract 回退 Path.home()（采集器自己的 home）。单用户场景无差，Agent 以他人身份/自定义 HOME 运行时初版快照的 root 集合与注册后版本不一致，会造成 first-report 与后续 revision 的范围抖动。一行参数补齐即可。

### 本轮审查通过项
- protocol.py skill_scope：symlink 逃逸按 root 前缀检查、2000 条截断标记、deadline 贯穿读循环、超时/IO 分类错误码，符合 guide。
- store.py：stale_report 拒绝旧报、alias 环检测、merge 只允许 discovered 类型、audit 表齐全。
- collector.py：URL 只保留 origin、args 不导出、credential_ref 只报键名——脱敏方向正确。
- bounded_reader：子进程隔离 + 剩余预算 min(15,deadline)，无 shell。

累计：崩溃级 3、中高 1、中 10（含 B21）、低/卫生若干。修复顺序不变，B21/B22 归入 hygiene 批次。
## 13. 第六轮追加（02:25，部署包与信任审计深审 + fork 演练，仍只测不改）

### B23（低-中）deployment_package.supports_direct_events 靠源码文本正则判定"能直报事件"
四组 regex/子串匹配（ASG_SOC_DIRECT_PROTOCOL = True、runControlClient("event"、function record(event, meta) + appendLine(...) 精确一行 + CONTROL_CONFIG 等）判断 install_plan 内嵌 hook 源码是否具备直报能力。Hook 源码重构、格式化、注释位置变化都会让已验证配方突然"不支持直报"而拒绝发布，反之拷贝了特征字符串但没有真实调用的桩代码能骗过判定。这是内容级能力声明，应该由配方元数据显式声明 capability，文本探测只作兜底告警。同类问题：learned_control_client 要求 kind: deny 与 outcome: blocked 的字面写法。

### 本轮审查通过项
- deployment_package.build：打包前 validate_bundle + validate_plan + YAML 语法门 + 凭据/聊天/机器路径可移植性扫描，缺一项直接拒绝发布，方向正确（与 B17 的 release 打包形成对照——learned 包有扫描，release 整包没有）。
- installer_revision 对安装器全部关键文件做 sha256 绑定，满足"包与摘要绑定同一版本"的语义。
- codex_trust.py：trusted_hash 算法逐位复刻 Codex discovery.rs（含 SessionEnd/Interrupt 超时钳位），明确"ASG 永不写 hooks.state"，只读审计定位清晰，未见缺陷。
- collection_worker：短生命周期子进程 + 父侧 wall-clock 截止，符合 guide 超时要求。

## 14. fork 演练结论（02:25，只读演练，未建 fork）
按 setup_fork.sh 的 rsync 排除规则（data/ artifacts/ tmp/）在 /tmp/asg-fork-dryrun-bj4aqq 建了临时副本，跑了引用 artifacts 最多的 5 个测试文件 + 未提交改动涉及的 2 个：27 全过。结论：全量测试不依赖被排除目录，fork 后测试可全绿起跑。worker_fork.sh 已备好（cd 指向 fork、结果写 /tmp/crazytest/fork_results/），06:00 后直接用，不要用写死原仓路径的 worker.sh 修代码。
## 15. 第七轮追加（02:35，package_runtime/install.py 623 行全文 + learned_install/recipe_bundle 复核，仍只测不改）

### B24（中，泛化红线）install.py 里硬编码 DSH 专属锚点做模型请求采集注入，锚点失效时静默降级且无能力上报
wire_model_request_payload 用三段逐字节锚点（中文注释"── 2. 模型请求路由"、精确 route 块、"── 3. 工具执行前"）往 Hook 源码里注入 llm/stream 监听。这是 DSH 插件模板的拟合：DSH 升级/重排注释后 anchor 找不到就原样返回，包照样发布、照样装成功（supports_direct_events 只看基础 record 特征），结果 hook 没有 model.request 采集但收据和页面都不知道——正是用户点名"千万不能拟合"的形态。同类 wire_ 函数（wire_soc_events/wire_soc_control_client）还有锚点只替换首处出现的隐患（appendLine 多处调用只转发第一处）。修复方向：注入结果必须在包元数据/receipt 里回报 wired:true/false，锚点匹配改为可声明的多模式；长期看这类品牌专属注入应挪进配方数据而非安装器代码。

### B25（低）validate_plan 的 1-16 文件上限与 direct 模式追加 4 文件冲突
direct 模式先拿配方 plan（最多 16）再 append 4 个 .soc-hook 文件后 validate：配方 13+ 文件时安装被一句笼统的 "plan requires 1-16 file changes" 拒绝，排查成本高。另外 --verify-pid 路径直接 receipt.read_text()，receipt 不存在时抛 FileNotFoundError 被兜底成一句 failed，提示信息不可读。均属卫生。

### 本轮审查通过项
- install.py 升级路径：优先校验 receipt 字节归属才允许原地升级、内容寻址 loader 模块保留旧副本防活进程引用、activation_affecting 精确区分传输面/进程内变更、RUNTIME_MUTABLE 仅保留 SOC policy——设计与实现一致，未见缺陷。
- uninstall 按 rollback_chain 新到旧回滚，仅对 RUNTIME_MUTABLE rebase 守卫，其余严格字节回滚。
- learned_install：manifest 文件锁 + 审批摘要双向校验 + 原子写 fsync + state 必须在 workspace 外；recipe_bundle.resolve_bundle 接收端重算摘要的语义注释清晰。install receipt chmod 0600，token 只进高权限文件。

累计：崩溃级 3、中高 1、中 12（含 B24）、低/卫生若干。B24 加入修复清单，位置建议排在 B6 之后（同为 Hook 运行时语义问题）。
## 16. 第八轮追加（02:40，autonomous_pipeline / file_lock / preview，仍只测不改）

- runtime/autonomous_pipeline.py（122 行）：阶段推进以持久化执行结果为准（安装≠激活、repair 限 2 次且间隔 300s、selfcheck 5 秒缓存），verify_installed 从观测绑定+配对事件+acceptance 三处交叉判定，未见缺陷。
- runtime/file_lock.py：fcntl/msvcrt 双平台锁 + 线程锁叠加，锁文件残留策略合理。
- integrations/soc_inventory/preview.py：本地预览接收器，随机 bearer、--source 白名单、watch-root 只收 identified+agent 角色，自我声明"不是部署网关"。定位清晰，未见缺陷。

至此 runtime/、integrations/soc_inventory/、release/、tools/、web/、e2e 编排与验收入口全部至少过审一遍。B 项汇总维持 B1-B25。下一步动作全部集中到 06:00 后的 cryzytest 分支修复。
## 17. 运行态取证（02:20，回应第 4 节两个"待验证"项；只读，未改任何源码/配置）

8081 面真相与交接记录不同：当前 8081 不是从工作区代码启动，而是安装版 0.9.40-beta（/Users/mac/Library/Application Support/ASG/releases/0.9.40-beta，pid 15928/15929，9-23 14:53 起）。工作区未提交的 capture_matrix（B2）与 conversation_proof 不在运行进程中——B2 属"部署后才咬人"，线上四个 /api/hook-data 现全 200。B1 的 install 路径同样不在运行面上，但源码仍是坏的。

### B26（中）周期扫描在升级/重启后静默熄灭，页面仍显示间隔
state/engine/scan_settings.json 现值仅 {"scan_interval": 90}，缺 scan_enabled 键。安装版 monitor_dashboard.py 冷启动读该文件：SCAN_INTERVAL_S 读出 90 成功，SCAN_ENABLED 读 scan_enabled 抛 KeyError 走 except 保持默认 False（第 108/126 行）。即引擎自认为"间隔 90 秒但周期关闭"，实际只有启动时扫过一次（scan_count=1，last_scan_time 停在 9-23 14:53）。用户此前按页面把扫描改成手动+自定义间隔，旧格式设置文件升级后 scan_enabled 迁移丢失，且 set_scan_interval 的隐式开启语义（B7）让"改间隔"与"开周期"耦合，一旦用户只想改间隔也会被动开启。修 B7 时一并做：读设置缺 scan_enabled 键时按旧语义显式迁移（默认关闭但要落盘补键并在 /api/state 暴露 scan_enabled），supervisor 与页面显示以真实 enabled 为准。

### B27（低-中）supervisor 在手动扫描模式下永久误报 scan_stalled
service_main.engine_probe 用"scan_count 超过 3×interval+120 秒未变化"判停滞，但产品方向是周期扫描默认关闭、以手动采集为主。当前心跳 snapshot 里 scan_stalled 恒为 true（已连续 >11 小时），任何依赖该字段的健康决策（自动重启/告警）都会误伤。修法：probe 读 /api/state 的 scan_enabled（修 B26 后可用），enabled=false 时不参与停滞判定。

### B14 运行态结论（存疑项排除）
endpoint 的 soc 状态 HTTP 200 /api/asg/self、队列 pending=0 drafts=0、bridge/loop beat 15 秒内新鲜——当前无过期 install 命令积压的表现；命令过期语义仍按 B14 在分支上补测试即可，不再单列运行风险。
## 18. 第九轮追加（02:30，observation_source.py 467 行全文，仍只测不改）

审查通过：状态键含映射指纹（改字段映射必然重读，不混算旧结果）、inode+截断检测（同路径替换/轮转即重置）、符号链接双拒（日志文件本身与父链）、时间戳拒 NaN/Inf/无时区/早于实例 create_time、tool 事件缺 call_id 判 invalid 不发明名字、valid/invalid/ignored 三计数不膨胀。与设计意图逐条相符，无新 B 项。唯一提示：日志被轮转替换后 valid/paired 累计计数归零属设计（"different reader target"），下游若有"N 条有效事件"展示会在轮转瞬间回落，验收时别把它当回归。

至此运行面核心（observation_source/hook_acceptance/io_acceptance/autonomous_pipeline/analyzer 触发链）与文件面（release/SOC 链路/安装器/前端）全部过审。B 项维持 B1-B27。
## 19. 测试欠账精确定位（02:35，供 06:00 后第一批修复直接执行）

全量 junit 复核确认两个失败是固定的两条用例（多分片复跑同点复现，非环境噪声）：
1. test_adapter_stage1.AdapterStage1Tests.test_alternate_mechanism_reject_signature_change — 断言 'trial' != 'similar'。工作区改动把兼容判定新增了 trial 档（用户已拍板 trial 成功写 revision 的方案），测试仍期望旧值 similar。修复时先读 runtime/matcher.py 的 trial 判定条件确认新语义正确，再把测试期望改为 trial，并补一条 trial→exact 晋升/失败的边界用例。
2. integrations.soc_inventory.test_g07_investigation_lifecycle.InvestigationLifecycleTest.test_investigation_requested_once_until_package_exists — TypeError: lambda takes 1 positional argument but 2 were given。工作区给 confirmed(state) 加了 conversation_proof 第二参数，测试里的 mock lambda 没跟上。修复：mock 改收两参；同时检查该 lambda 的返回值语义在新参数下是否仍成立。

fork 演练副本（/tmp/asg-fork-dryrun-bj4aqq）跑这两个文件：30 passed——因为在副本里它们也复现了失败？不对：副本 30 全过、原仓 2 失败？复核发现副本测试的是 rsync 时点的工作区代码，与本测试欠账不矛盾：副本全过的是 test_goose_stage1+test_onboarding 共 30 例，两条失败用例分别位于 test_adapter_stage1.py 与 integrations/soc_inventory/test_g07_investigation_lifecycle.py，不在该 30 例内。fork 后第一步就应复跑这两条确认复现。
## 20. 第十轮追加（02:45，analyst_evidence / io_acceptance / integration_protocol，仍只测不改）

- runtime/analyst_evidence.py（618 行）：读文件限定进程派生根；符号链接回退路径同时要求"词法上在证据根下 + 解析后仍在 $HOME 内 + 非敏感名"；敏感名/后缀双重黑名单 + 遍历目录同样过滤；输出恒有界（24KB）+ 双套脱敏（键名 + sk/ghp 等值形态）。一处设计性提示：JSON 解析成功走 _redact_object（按键脱敏）、失败走 _redact_text（按值形态脱敏），两通道规则不同，验收样例若同时覆盖两种格式更稳。无 B 项。
- runtime/io_acceptance.py（117 行）：按 instance/session/turn/agent 分组，event_id 去重、sequence 连续、model/tool 成对、checkpoint 计数与 last_sequence 交叉核对；局限三条明说"checkpoint 是生产者自报、不是独立 oracle"。无 B 项。
- runtime/integration_protocol.py：只识别协议证据（ACP/MCP/OTel），明确"检测到可执行文件≠接通"，不执行任何发现的命令。无 B 项。

全部核心模块过审完毕，B 项最终维持 B1-B27。阶段一文档定稿，等待 06:00。

## 21. 第十轮：monitor_dashboard.py 运行面大文件收尾审查（02:20）

已完成 do_POST 全路由、扫描主循环 background_scanner_loop、调查队列/调度器、OTLP ingest 边界、scan_settings 冷启动路径抽查。新发现 3 项；OTLP ingest 本体（bearer 常数时间比较、4MiB 上限、实例绑定+create_time 校验、时间窗校验、derived 记录不计数）确认无缺陷。

### B28（低-中，安全面）：/api/scan 与 /api/onboarding/execute 等 POST 路由无鉴权，仅靠默认绑定 127.0.0.1
- 位置：monitor_dashboard.py:2756（/api/scan）、:2775（/api/onboarding/execute）、/api/scan-interval、/api/model-settings、/api/recipe-bundle/import 均无 token 校验；对比 runtime/otlp_ingest.py:112 已有 hmac.compare_digest bearer 校验。
- 现状默认 host=127.0.0.1（:2902），本机风险可接受；但此前需求明确要过"页面局域网访问"，一旦用 ASG_HOST=0.0.0.0 暴露，同网段任何人可触发扫描、经 onboarding execute 真实写入目标 Agent 配置目录、改模型配置、导入配方包。
- 修复建议：把 hook_control 的 token 校验提为 do_POST 公共守卫（或至少对 onboarding/model-settings/recipe-bundle/scan-interval 四个写路由校验），OTLP 已有先例可循。改动小。

### B29（低）：_dispatch_next_investigation 在阻塞等待槽位期间不复查目标存活
- 位置：monitor_dashboard.py:1550-1580。_investigation_target_alive 检查在阻塞式 INVESTIGATION_SEMAPHORE.acquire()（:1573）之前执行；队列有任务但槽位全忙时，目标可能在等待期间退出，仍会启动一次注定失败的调查（analyzer.analyze 抛错后以残缺 struct 继续 _execute_investigation，浪费锁与运行目录）。
- 影响：仅浪费资源与产生误导性 failed 记录，不崩。修复：acquire 之后再验一次存活，死则记录取消并 release。

### B30（卫生）：scan_settings.json 冷启动读取路径小问题簇
- 位置：monitor_dashboard.py:124-129。同一文件连续 read+json.loads 两次（可合并）。B26 根因（缺 scan_enabled 键→KeyError→SCAN_ENABLED 恒 False）属 HEAD 既有行为，维持原判；补充事实：set_scan_interval/set_scan_enabled 总是写全两个键（:141/:158），用户点过一次设置即自愈，B26 只影响从未设置过的冷启动。修 B26 时一并处理。

### monitor_dashboard.py 审查结论
HTTP 面、扫描循环、调查调度、OTLP 边界全部过审。除 B28/B29/B30 外未发现新缺陷。至此全部代码面（含 2923 行运行面大文件）审查完毕，阶段一记录完成。

---

## 22. 修复记录（修复阶段提前至 02:30 后；工作区 /Users/mac/个人项目/asg-crazytest-wt，分支 cryzytest）

说明：原计划 06:00 后开分支修复。02:21 建 fork 时发现原仓是 git worktree、rsync 复制导致 fork 与原仓共享 gitdir，commit 误挪了原仓 HEAD；当即恢复（原仓 reset --mixed 回 9fb2637 回到 beta 分支，status 恒 20，逐轮验证），改用 git worktree add 建立独立工作区后继续。全部提交只落在 cryzytest 分支，原仓与 beta 分支未被触碰。

### 逐项修复与验证

| 项 | 修法 | 验证 | commit |
| --- | --- | --- | --- |
| B1 采集服务 install 必炸 UnboundLocalError | soc_collector_service.py 用 service_root 替换错误 ROOT 引用 | 全量绿 | fa4325d |
| B2 capture_matrix 字符串 content/payload 崩溃 | hook_data.py isinstance 防护 | 全量绿+回归断言 | fa4325d |
| 测试欠账 2 条 | test_adapter_stage1 期望改 trial；g07 mock 补 conversation_proof 参数 | 全量绿 | fa4325d |
| B4 event_spool 一组失败丢弃全部后续组 | flush 循环 return 改 continue | 全量绿 | a960f4b |
| B6 explicit_deny 被 control_error 旁路+超大 stdin 死代码 | deny 不再信任 control_error 分支；超大 stdin observe-first 放行并记 capture.oversized | 全量绿 | a960f4b |
| B7 改间隔隐式开启周期扫描 | set_scan_interval 不再置 SCAN_ENABLED=True；接口回真实开关 | 全量绿 | a960f4b |
| B24 模型请求接线结果不落回执 | wire_model_request_payload 返回 (content,state)，receipt 落 model_request_wiring；补 not_applicable 用例 | 定向+全量绿 | a960f4b |
| B26 旧设置文件缺键致周期扫描静默失效 | _load_scan_settings() 显式迁移并落盘完整 schema | 全量绿 | a960f4b |
| B27 手动模式 supervisor 误报 scan_stalled | engine_probe 在 scan_enabled=false 时不判停滞 | 全量绿 | a960f4b |
| B3 指纹 baseline 链断裂 | 新工具 tools/repair_fingerprint_baselines.py 按 revision 链推导；实修 opencode 两处，不可推导如实报 unverifiable | 工具自测+全量绿 | a60b1b5 |
| B9 revisions 只增不减 | matcher._prune_revisions：保留最近 10 条+90 天内 exact 命中永不删 | 全量绿 | a60b1b5 |
| B17 发布包携带构建机 home 路径 | build_release.py sanitize_portability() 剔除并写 portability-scan.json | 单测绿 | a60b1b5 |
| B21 工作区 MCP 配置结构探测 | protocol.py 走 _first_mcp_field，根级 servers 空 field，空 part 跳过 | 全量绿 | a60b1b5 |
| B22 首次 pending 清点缺 collection_home | discovery pending 补 home 字段 | 全量绿 | a60b1b5 |
| B5 外部 Hook 永久否决后续安装 | observed 状态目录出现兼容 artifact 时 fall through 正常安装 | 全量绿 | 0848e7f |
| B10 永久 4xx 毒记录堵死上报队列 | 非 408/429 的 4xx 计 3 次后移入 dead_letter 表并继续 | 端到端手工验证+全量绿 | 0848e7f |
| B15 两套命令 Hook 控制故障语义不一 | command_protocol_hook 统一 observe-first，control_failure_mode=enforce 才阻断 | test_protocol_fastpath 锁两档语义 | 1512c7e |
| B28 管理 POST 可被局域网对端调用 | do_POST 管理路由要求 loopback peer，403 提示 read_only_mode；token 路由不受影响 | 全量绿 | 1512c7e |
| B29 dispatcher 拿槽位后目标已死仍派发 | 派发前复查存活，死则释放槽位并记取消 | 全量绿 | 1512c7e |
| B18 reap_orphans 误杀同前缀并存安装 | home 匹配加边界符 | 全量绿 | 7ec6ef8 |
| B13 conversation_proof 15s 阻塞 | timeout 降为 5s | 全量绿 | 7ec6ef8 |
| B25 计划文件数错误信息缺实际值 | 报错带实际数量 | 全量绿 | 7ec6ef8 |
| B30 冷启动设置读取分散 | 并入 _load_settings 统一入口 | 全量绿 | 7ec6ef8 |
| B12 体积裁剪循环反复全量序列化 | 以骨架 payload 测基准体积，仅对 records 数组序列化比对余量 | 定向 11 用例+全量绿 | 58971f3 |
| B20 抽屉展开态按标题匹配致重复标题联动 | 展开态兜底键加序号限定 | node --check 语法通过+全量绿 | 本提交 |
| e2e B023 闭包捕获循环变量（延迟对账可信度） | 函数默认参绑定 sid/canary/sent/assets | ruff B023 清零+全量绿 | 8e4cca7 |

### 未修项及理由

- B8（GOOSE_MAX_TURNS 40→120）：工作区既有未提交人工决定，非缺陷；按成本敏感原则在交付说明显式标注，由交付人拍板。
- B11（双线程 sqlite 锁冲突仅日志难区分）：低概率无正确性影响，保持现状并记录。
- B14（install 命令过期语义）：行为符合不自动重试安装的设计，缺口在 UI 提示，属产品侧待办。
- B16：仅记录项，无需代码动作。
- B19（supervisor scan_interval 重启回 config 值）：复核确认已被 B26 顺带修复——引擎冷启动时设置文件值覆盖 ASG_SCAN_INTERVAL 环境变量，页面设置跨重启保持。
- ~~B20~~：已修复（index-qualified fallback），见上表。
- B23（能力声明依赖源码正则）：方向性重构建议，不适合夜间批量实施。
- ruff 其余项（F401 未用 import、F841 死赋值）：全仓清理 diff 较大且自动修复可能误删可用性探测式导入，夜间不处理；e2e B023 已单独修复（8e4cca7）。

### 最终全量测试结果

（08:30 固化时回填）
