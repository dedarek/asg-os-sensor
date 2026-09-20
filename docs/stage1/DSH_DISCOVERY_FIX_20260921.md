# DSH 自动发现漏报修复（任务 3 / G-发现）2026-09-21

问题复核：DSH（pid 70828，node .../@deepseek-ai/dsh/lib/bin.js web --no-open --port 43123）
在 discovery_audit 中状态 below_threshold、score 0、evidence 空——两个入口都没接住：
agent_score 的 Intent 门禁（它没有 --model/--prompt 等参数）不通过，且
runtime_discovery_candidate 的全部信号族（模型 SDK 声明、模型/任务/治理参数、
打开标准资产、MCP 子进程、CLI 子进程、桌面归属）它对不上任何一个：
它的 72 个依赖里没有一个在 model SDK 名单里，web 模式也没有子进程。

修复（runtime/identity.py，通用信号，不写任何厂商名）：
metadata_identity 读取归属包 package.json 时顺带收集"协议生态依赖"——
依赖名按分隔符切 token 后命中 mcp / acp / modelcontextprotocol 整词才算
（declares_protocol_ecosystem；'mcpkit'、'acpx-tools' 这类近似名不匹配）。
runtime_discovery_candidate 新增信号 protocol-ecosystem-dependency：
安装包声明 MCP/ACP 协议栈依赖 = 具备协议生态能力证据，仅准入"待调查角色"，
绝不判定 Agent。DSH 恰好命中 @deepseek-ai/dsh-mcp-client 与 @deepseek-ai/dsh-acp-app
——这是它作为 MCP 客户端 + ACP 应用的真实能力声明，不是为其定制的名单。

不误报的保证：
- 该信号只在 0<=score<threshold 的进程上评估；agent_score 已对 MCP 服务端、
  Shell 包装器、Chromium/Electron helper 等返回 -1 的进程不会再进这个口。
- 普通软件几乎不会在归属包 dependencies 里整词声明 mcp/acp；测试含反例
  （纯 commander 依赖的 CLI 不进入，近似 token 不匹配）。
- 命中后仍是"候选/待确认 + 待调查角色（非 Agent 确认）"，分类必须走调查。

证据（本机实时进程，2026-09-21 03:4x）：
- metadata_identity(70828).protocol_dependencies = ['@deepseek-ai/dsh-acp-app', '@deepseek-ai/dsh-mcp-client']
- runtime_discovery_candidate(70828) signals = [package-bin-mapping, protocol-ecosystem-dependency]
- 以 monitor_dashboard 扫描循环同构代码实时全机跑一遍：候选集合含 DSH（score 0，靠信号准入），
  其余候选仍为 ZCode/Codex/OpenCodex/OpenCode 等，无新增普通软件误报。
- test_discovery 16/16 通过（含新增通用协议信号正反用例）；
  integrations.soc_inventory 13 模块 58 项回归通过。

边界（如实）：8081 面板进程仍加载旧代码，页面上 DSH 出现需要该开发实例下次重启后
手动扫描确认；本修复的验收证据是上述实时全机扫描与单元测试，页面确认归入重启窗口。
固定名单 5 Agent 0 漏报与 10 个普通软件反例的系统化跑批仍按任务 3 验收标准待做。

系统化矩阵验收补充（同日晚）：e2e/verify_discovery_matrix.py 实时全机跑通——固定名单 5 个真实 Agent（OpenCode 57879、桌面 Codex 41158、DSH 70828、ZCode 999、OpenCodex 43921）全部进入候选，漏报 0；10 个普通程序反例（纯 CLI、近似 mcp 依赖包、sleep、zsh 包装器、伪 MCP 服务端、5 个桌面 Helper 子进程）全部不误报。报告 artifacts/acceptance/discovery-matrix-20260921.json。
