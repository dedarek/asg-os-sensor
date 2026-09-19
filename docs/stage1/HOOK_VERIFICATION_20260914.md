# 2026-09-14 Hook 独立验收与模型传输验证

Hook 由 Goose 根据目标代码与接口证据生成，经通用安装器写入。验收驱动只调用真实 Agent，并检查实际文件效果，不直接调用 Hook、不插入观测事件。产品运行时没有新增按品牌分支的 adapter；各产品的测试驱动仅用于独立验收。

| 目标与范围 | 本轮实测 | 尚未证明 |
|---|---|---|
| 隔离 OpenCode 服务 | 新实例重新执行 allow / deny，文件效果符合决策；HTTP 请求体、完整 SSE 响应与 Agent 回复包含同一唯一标记 | 任意新 Agent 自动改路由；原生 Hook 捕获全部网络 |
| 隔离 Codex app-server | 原生 Hook 加载，实际 allow / deny 文件效果通过，事件关联真实服务实例 | 当前桌面 Codex 自动加载；无需人工原生信任的全自动接入 |
| 隔离 ZCode CLI | 显式加载 Goose 生成的原样注册后，独立 allow / deny 文件效果通过；用户输入、工具调用、助手输出有真实事件 | 桌面 ZCode 加载；工作区信任后 CLI 自动采用项目 Hook |

ZCode 的实际加载器存在工作区信任门控。测试使用原生信任命令后，项目作用域仍未出现事件；将原样生成的注册复制到隔离用户配置才实际加载。这属于明确的测试设置，不算自主接入成功。测试 CLI 结束后，其记录不能授予桌面实例成功状态。

模型传输记录默认关闭，仅对明确配置的测试网关开启。每次捕获验证本地连接归属、PID 与创建时间，以请求编号配对 Agent 面向网关的 JSON/SSE 字节内容。内容有脱敏与大小上限，截断或未完成响应明确标记，不算完整覆盖。该证据不代表 TLS 报文、供应商内部请求，也不把原生 Hook 的模型参数或助手文本等同于完整 HTTP 响应。

页面进入目标的 Hook 原始数据抽屉，可查看绑定来源、输入输出、工具参数结果与网络覆盖；可下载原始记录。网络摘要属于当前日志窗口，和独立执行控制验收分开。

本地验收报告：
- `artifacts/autonomous-demo/control-verification.json`
- `artifacts/autonomous-demo/model-transport-verification.json`
- `artifacts/codex-acceptance/control-verification.json`
- `artifacts/zcode-acceptance/control-verification.json`

回归检查：333 项测试，4 项跳过，其余通过。报告、原始事件、隔离认证配置和学习指纹不随源码发布。浏览器视觉复查因本机锁屏未完成；HTTP 数据与回归检查另行验证。
