# SOC 优先安装与直连控制：当前改动与验收

目标：ASG 在终端负责发现、清点、登记、选包、安装及升级；Agent Hook 运行时直接请求 SOC，SOC 检测后由目标原生扩展点执行放行或拒绝。资产清点继续按需触发。

## 已实现代码

- SOC 网关增加经 Agent key 鉴权的 `/api/asg/artifact/catalog` 和 `/api/asg/artifact/:id/download`，按 Agent 类型选包，并复核对象 SHA-256。
- ASG `soc_onboarding.py` 无模型选包，检查平台、架构、可执行文件及 bundle 构建摘要；不兼容返回调查路由。下载后使用全新私有目录解包，拒绝越界/链接/重复条目，并在写入前重新核对目标进程启动时间、可执行文件、工作区。
- 新安装包携带独立 Python SOC 客户端。通过 `--backend-url --agent-id --platform --token-file` 安装，密钥只在终端生成，权限 0600。无需 ASG 服务参与工具前置决策。
- 安装记录绑定 SOC 地址与 Agent 身份；不同身份不能静默覆盖。支持文件摘要检查、重复安装、冲突拒绝、回滚及当前 PID 的加载验证。
- Discovery 增加 `soc_installation` 开关后的选包流程。当前没有在用户正常实例上全局启用。

## 本轮真实验收

报告：`artifacts/acceptance/soc-direct/report.json`。

1. 本地 SOC/MinIO 保存实际安装包，目录查询、下载校验和隔离工作区安装通过；此路径模型调用数为 0。
2. 真实 OpenCode 新实例加载该安装包，通过进程启动时间、工作区、已安装文件摘要和新 `hook.loaded` 记录核验。
3. ASG 8081、8099 均未监听时，安装后的独立客户端直接请求 SOC，获得带 request_id 的 allow。此项是客户端联通证据，不是目标工具阻断证据。
4. OpenCode Shell 接口执行成功，但该接口未触发 tool.execute.before，不能用于证明工具决策闭环。
5. 实际模型工具调用未完成：7777 Chat Completions 返回空 delta.role，Responses 路径触发 summaryParts 解析异常。没有把这两次失败算作控制通过。
6. Python 五项针对测试通过；Go controller/util 测试通过。Python 包测试覆盖无 ASG 路径安装、权限、身份冲突、重复安装、修改冲突、回滚及构建不匹配。

## 未完成，不能宣布整体交付

- 原 SOC 四类当前发布包尚未统一纳入新目录选包；本轮目录只接收带 soc-direct-v1 元数据的新增安装包。
- 原发现调度器与新选包流程的全局优先级尚未统一，开关仍为关闭；不存在“正常安装后全自动已验收”的结论。
- 协议/Goose 调查后已有本地安装路径尚未全部切换成生成并安装 SOC 直连包。
- 当前独立客户端仅工具前置决策；用户输入、工具后置、会话结束等 SOC 通道还需统一接入。原事件日志仍在终端。
- 原 SOC 包发布/审批策略与学习包提升为共享包之间的准入规则需要接入；不能将候选包等同于已验证发布包。
- 真实 SOC 策略拒绝与目标未执行的联合验收尚未通过；原目标信任批准仍由目标应用管理。

Windows 本轮未修改或验收。用户正常 Agent 的全局配置未修改。
