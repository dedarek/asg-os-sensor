# 本批验收总表（2026-09-21，批次冻结点）

统一绑定：主仓 b4b8080（work/advisor-real-investigation-20260911，GitHub beta/asg-soc-direct-20260921 同点）；
soc-open-api 70747f7（运行网关二进制 sha256 前缀 c7696c04）；poc-soc-mngt 0ac5fe8；
确定交付包 0.9.5（manifest b10d0987，构建自 a72db73）。
历史结果不自动继承到本批；下表仅列本批或明确标注批次内可追溯证据。

| # | 门槛 | 本批结果 | 绑定证据 |
| --- | --- | --- | --- |
| 1 | 干净目录安装 60s 健康 | 通过 11.4s | terminal_lifecycle_0_9_5_readme_report.json（0.9.5 独立复跑） |
| 1 | 删解压目录后存活/重复安装幂等 | 通过 | 同上（step 2-3） |
| 1 | 缺文件/篡改摘要拒装、坏凭据 rc5 | 通过 | 同上（negative_installs / bad_credential_rc5_and_repair） |
| 1 | doctor 四故障分别报因 | 通过 | 同上（doctor_fault_injection） |
| 1 | 升级 B 成功；故障升级 90s 内回滚 | 通过（回滚 143s 含 90s 健康恢复门槛内） | 同上（upgrade_a_to_b / upgrade_failure_rolls_back_within_90s） |
| 1 | 卸载保数据保 Hook；按范围卸载 Hook | 通过 | 同上（uninstall 两步） |
| 2 | 桌面 Codex 逐字对账 ≥10 回合 | 通过（16 回合/37 文本/0 不一致，实例 41158，通道 api） | artifacts/acceptance/normal-conversation-41158.json（09-20 批次，实例仍存活；不继承给新实例） |
| 2 | 断网补报 1000 事件缺失 0 重复 0（原生通道） | 通过 | terminal_lifecycle_0_9_3…（含于 GAP G08）；本批网关重启期间 12 事件自动补报复核 |
| 2 | 严格模式决策超时真实阻断 | 通过（2.1s 阻断、文件未建、会话内提示原因） | asg-noutbox-phase1.json + GAP G08 追加节 |
| 2 | 事件幂等（重放不重复） | 通过（opencode/hermes 功能验收；openclaw 仅语法） | GAP G08 / TERMINAL_LIFECYCLE_ACCEPTANCE_20260921 追加节 |
| 3 | 5 真实 Agent 0 漏报、10 普通程序 0 误确认 | 通过（DSH 修复批次，矩阵含反例） | DSH_DISCOVERY_FIX_20260921.md |
| 3 | 页面状态与实例事件一致 | 通过（DSH 70828/Codex 41158 实测；矛盾组合被 43 项测试覆盖） | 提交 9613d5e 说明 + 本批实测记录 |
| 4 | 陌生 Agent 全链（发现→调查→包→安装→回调） | 未验收——等待无配方历史的新目标 | GAP G07 行如实记录 |
| 4 | 兼容实例零模型复用 | 部分（同机零模型选包安装已实测；跨实例新事件复验受 G07 同门槛） | SOC_DIRECT_INSTALLATION_20260919.md + GAP |
| 5 | 3 Skill/2 MCP 清点一致、增改删可读、失败保留旧值 | 通过（测试资产链路）；真实办公 Agent 逐项待样 | GAP G09/G05 行 |
| 5 | Guardian 真实扫描 | 未接（替身标签 UNVERIFIED_TEST 保留，未冒充） | GAP G11 行 |
| 6 | 页面可见延迟 ≥30 样本 P95≤5s | 通过（30/30 样本，P95 2.73s，max 3.07s，浏览器 DOM 实测） | browser-page-latency-opencode.json + PAGE_LATENCY_20260921.md（绑定 demo 实例拓扑） |
| 6 | 60 分钟连续运行 | 通过（121/121 健康检查；11 次探针失败留档） | GAP/验收文档对应节 |
| 6 | 凭据不落日志 | 通过（新网关：正文回显默认关、心跳不打 token，新日志 token 0 命中） | soc-open-api 70747f7 + 验收文档"网关日志整改"节 |
| 7 | 版本固定+可重复打包+说明 | 通过（0.9.5 = a72db73 = manifest b10d0987 = README 说明 = 17 步报告） | terminal_lifecycle_0_9_5_readme_report.json |
| 7 | 远端 beta 发布 | 主仓 GitHub 完成；GitLab 三个仓库待内网恢复（DNS 不可解析） | 本批推送日志 |

## 待外部条件（非本机可闭环）
1. 内网恢复 → 推 soc-open-api / poc-soc-mngt / incubator/asg-os-sensor 的 beta refs。
2. 桌面 Codex /hooks 用户信任后新实例 10 回合对账（当前 41158 结果不继承新实例）。
3. 无配方历史的新 Agent → G07 全链一次。
4. 第二终端 / 真实 Guardian / AI Trust 平台。
