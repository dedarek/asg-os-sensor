# 协议优先接入（2026-09-15）

当前流程：发现目标 → 本机收集协议结构 → 可复用命令式 Hook 直接安装 → 读取当前实例真实事件自检 → 未识别、安装失败、120 秒未激活或覆盖不足时转 Goose。Goose 收到快速路径结果、缺口与既有安装信息，先补已有通道，必要时调查插件。

## 已落地的边界

- `runtime/protocol_fastpath.py`：在解析 Goose 可执行程序之前运行；按进程相关 JSON/JSONC/TOML 配置结构匹配，不按产品名匹配。只处理唯一、已解析、位于部署授权目录的顶层 command Hooks。保留旧回调，写入前核对文件哈希，留存可回滚事务。已有实例绑定不被覆盖。
- `runtime/command_protocol_hook.py`：复用命令式 JSON 输入与 PreToolUse 决策输出约定；只采集能定位到登记目标祖先进程的回调。拒绝、控制客户端错误或超时返回拒绝。模板实现等待不等于任意目标都会遵守，仍需独立效果验收。
- `runtime/integration_protocol.py`：新 Goose 配方必须先引用当前目标的协议检查证据；选择插件或跳过已有通道时必须说明缺口。旧配方可读取和走既有兼容复用路径。
- `runtime/protocol_events.py`：输出统一 envelope，保留原始数据，映射会话/调用/Agent 编号；缺失编号不补造。ACP 使用受管 stdio 桥接和 agent_servers 启动配置包装；OTLP/HTTP 接收 JSON/protobuf 日志与 traces。接入方法与限制见 [部署教程](../DEPLOYMENT.md)。
- 页面「Hook 实时数据」展示自动自检：加载、用户正文、助手正文、完整回合、工具配对、放行/拒绝实际效果、等待决定、来源完整性。API 同时提供 `normalized_records` 与 `coverage.integration_selfchecks`。

## 验收口径

自动自检 8/8 才显示该实例自检通过。正文必须非空；模型与工具调用必须同一实例/会话/回合对应，流式输出必须声明完成，事件编号唯一、序列连续、结束计数一致。控制必须有当前实例且 Hook 文件哈希仍一致的 `allow_effect`、`deny_effect`、`decision_wait` 独立验收项。没有真实活动是待验证；日志截断或解析/关联错误明确显示缺口。

本轮回归包含：未见过的名称但兼容协议结构、名称相似但无协议、不兼容结构、跨实例证据、ACP 片段、控制回执冒充效果、实际子进程执行已安装模板、原配置保留和回滚、快速路径绕过 Goose 以及失败转 Goose。

这些是协议模板与工程链路验证，不能当作真实 Agent 产品验收。自动自检不会擅自在用户会话发消息或制造文件副作用；真实目标的控制效果仍要通过受控测试登记。命令式 Hook 本身通常不提供完整模型传输，缺少这部分会转 Goose，而不是显示全部接通。

参考：[命令式 Hooks](https://code.claude.com/docs/en/hooks)、[ACP 工具调用与可选权限请求](https://agentclientprotocol.com/protocol/v1/tool-calls)、[OpenTelemetry GenAI](https://opentelemetry.io/docs/specs/semconv/gen-ai/)。
