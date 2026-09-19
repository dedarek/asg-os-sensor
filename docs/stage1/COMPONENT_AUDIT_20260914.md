# 当前 Codex 与 ASG 组件审计（2026-09-14）

## 检查范围

只读核查当前会话进程祖先、用户 Hook 配置和脚本、Codex 原生 hooks/list、Codex logs_2.sqlite 中当前主进程启动后的 WARN/ERROR、ASG 服务日志、最近 12 项调查生命周期和 stderr、5 个目标的绑定日志窗口、控制事件尾部 200 条，以及 8081/10100/65192 监听情况。不是全盘历史日志审计。本轮未改动用户 Hook 信任、未重启当前 Codex，也未发测试会话。

## 当前 Codex 不触发回调的原因

当前命令的父进程为 Codex 55875，其父为 ChatGPT 55500。ASG 注册的共享日志仅有测试实例 45137 的 49 条记录，当前 55875 的绑定将它们全部按 pid_mismatch 排除；这是正确的实例隔离。

使用同一个本地 Codex 二进制启动只读 app-server，以当前用户配置、工作目录 /Users/mac/Documents/ChatGPT/asg 调用原生 hooks/list，得到：

- config.toml 的 7 个 ASG Hook：enabled=true，trustStatus=untrusted。
- hooks.json 的旧版 3 个 ASG Hook：同样 untrusted。
- git-ai 的 3 个 Hook：trusted。
- 原生警告：同一配置层同时加载 hooks.json 和 config.toml，建议使用一种表示。

该检查是新建只读进程对当前磁盘配置的原生解析，不是读取 55875 的内存配置。它明确证实当前 ASG 配置尚未通过信任准入；即使重新加载同一配置仍会跳过，不应把重启当作修复。

[官方 Hook 文档](https://learn.chatgpt.com/docs/hooks)明确说明：非托管 Hook 必须审阅并信任当前定义哈希，否则跳过。当前阻断首先是信任准入，不能仅标为“等待加载”。

## 发现的问题

| 优先级 | 组件 | 有证据的问题 | 影响/处理方向 |
| --- | --- | --- | --- |
| P0 | 当前 Codex Hook | 7 个新注册、3 个旧注册均 untrusted | 当前真实会话无 ASG 回调；应先审阅唯一有效方案，再走原生信任流程 |
| P1 | 安装/激活状态 | 页面显示已安装等待加载，没有显示原生信任阻断 | 安装完成被误解为将自动生效；需增加原生准入检查及状态 |
| P1 | 重复注册 | hooks.json 和 config.toml 同时存在不同版本 ASG Hook | 两套都信任后可能重复执行；旧版还只保存工具输入输出哈希，不能提供正文 |
| P1 | 事件归一化 | 已有 Codex 输入事件为 user.prompt.submitted；正文提取及新验收仅认 user.input | 测试实例确有 6 条输入事件，但新展示/验收会遗漏；不能给当前 55875 补记这些事件 |
| P1 | 完整性验收 | 缺少独立入口对账，end_to_end_verified 固定为 false；旧 Hook 字段与新契约未迁移 | 目前只能发现已上报窗口的问题，不能宣称完整接通 |
| P2 | Hook 错误分类 | 测试日志两条 control.decision / policy 被记录为 severity=high 的 error | 正常拒绝与执行失败混淆，应区分决策结果和系统错误 |
| P2 | HTTP 服务 | service.log 有 3 次 BrokenPipeError，发生在响应写入阶段 | 客户端先断开导致异常堆栈；应局部处理断连，不能吞掉其他服务错误 |
| P2 | Codex 配置 | 启动后 100 条 unknown feature key: knowledge 警告 | 当前版本不识别该配置项；不是 Hook 无事件的直接原因 |
| P2 | Codex 请求链路 | 39 条 attestation generation request timed out、一次网关断流重试 | 请求侧有告警；聊天仍在运行，不能据此认定持续不可用 |
| P3 | 插件界面资源 | 4 条图标路径越界警告 | 图标被忽略，不代表 Skill 全部不可用 |

## 没发现持续故障的部分

- 页面与 API 可访问，8081、10100、65192 均接受 TCP 连接；TCP 成功不等于模型请求完整验收。
- 最近 12 项调查中有完成及主动取消记录，抽查 stderr 未命中新的 error/failed/timeout/429/401/certificate；取消产生的 -15 不等于崩溃。
- dsh 当前只有 2 条 hook.loaded，不能作为用户输入、模型输出、工具动作已捕获的证明。
- ZCode 当前目标无匹配事件，19 条其他 PID 记录被正确排除。

## 建议修复顺序

1. 核对并保留单一 ASG Hook 方案，按原生流程审阅信任；不直接跳过安全检查。随后验证当前真实会话的新回调。
2. 统一事件名称/角色/内容字段，保持原始事件不变，补齐原生准入与完整性状态展示。
3. 完成独立入口对账，再处理历史告警分类和 HTTP 断连噪声。

这份文档是问题审计，不表示上述修复已经完成。
