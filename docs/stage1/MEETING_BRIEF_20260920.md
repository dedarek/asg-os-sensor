# ASG × SOC 一页汇报（2026-09-20）

## 一句话

终端装好采集器后：自动发现机器上的 Agent → 自动注册到 SOC → 自动选包安装 Hook（见过就用发布包，没见过先登记再走协议/Goose 调查）→ Hook 事件与工具决策直连 SOC → 页面可查看资产、事件与执行控制。

## 已验证闭环（本机 SOC POC 实跑，非演示脚本）

| 环节 | 真实证据 |
| --- | --- |
| 常驻上报 | 采集器 launchd 服务（keepalive+runatload）连续运行；SOC 运行心跳每 15 秒刷新，00:02/00:11/00:19 连续可查 |
| 自动发现+注册 | 23:57 新 OpenCode 实例出现后 <60 秒自动注册为 asg-a42ce…，无人工步骤 |
| 包统一复用 | 真实 Codex(9601) 自动装发布包 f232d067；真实 OpenCode(57879) 自动装发布包 be5c3152；均 model_calls=0 |
| 未见类型路由 | 真实 ZCode(999) 无兼容包 → needs_investigation → protocol_then_goose，不误装 |
| 事件直报 | OpenCode 实例 2404 条逐条对账：终端 2404 = SOC 2404（event_id 去重），缺失 0、重复 0；含 user.input、model.request/response、tool.*、control.applied 全类型 |
| 执行控制 | 策略 CAP-3ded6744：放行 10/10 文件真实创建、拒绝 10/10 文件未创建，20 条审计轨迹（artifacts/acceptance/soc-control/report.json） |
| 延迟 | 排空积压后事件从产生到 SOC 入库 P95 14.9s（15s 桥接周期主导）；采集通道延迟 P95 1.8s；页面数据为手动刷新获取 |

## 本轮修复的泛化缺陷

Codex 观察者 Hook 内嵌正则被 JSON 双重转义，09-16 起 pid 解析全部失效——事件照写、绑定全断，属于"静默失效"。已修复运行副本与指纹库两份内嵌内容（expected_sha256 同步），修复后 Codex 工具事件 77/77 与 SOC 逐条一致，OpenCode 全类型事件恢复直报。此缺陷与具体 Agent 无关，任何复用该指纹内嵌脚本的安装都会随下次安装自动获得修复。

## 与会议承诺的口径对齐

- 已兑现：装完即用（Hook 直连 SOC 检测→放行/阻断，控制不依赖 ASG 服务在跑）；见过就装、没见过先登记；学习包只能候选入库，发布需兼容与激活验证（管理端对 learned-* 一律拒绝直接提升）。
- 如实保留：正常桌面 Codex 会话 userPromptSubmit 已 enabled+trusted，修复后的首条 user.input 直报将在下一个用户回合出现（本轮验证窗口内该会话无新用户消息）；AI Trust 真实平台接管、第二台机器复用、整机重启恢复为待外部条件。

## 交付物状态

- 代码：本地 main=82bd9f2（含验收附录 SOC_RECON_20260920.md）；gitlab.xpaas.lenovo.com 当前 SSL 不可达，恢复后推送 incubator/asg-os-sensor。
- 部署：setup.sh/setup.ps1/setup.bat 一键安装（含 Goose 自动下载与哈希校验）；采集器 tools/soc_collector_service.py install|status|remove 三平台服务封装。
- 文档：docs/DEPLOYMENT.md、docs/stage1/SOC_DIRECT_INSTALLATION_20260919.md、SOC_RECON_20260920.md、ACCEPTANCE_TABLE_20260919.md。
