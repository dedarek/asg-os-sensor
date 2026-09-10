# 本轮计划
基线 41a53b5，分支 work/discovery-goose-fingerprint。
沿用 docs/stage1/PLAN.md 的总范围，本轮仅：共用状态映射 → 页面/API 修正 → prior 错误与测试隔离 → 回归测试 → 隔离 8081 验收。
验收：禁用不报处理中，采集空/未采集/失败分离，进程与事件分离，指纹不冒充 Hook，手动入口禁用一致。完成停在 review，不代表 Stage1 闭环。

本轮补充：以 `cd47ea2` 为基线，增加撤销状态的已知实例绑定、回环观测请求的重定向/大小边界、异常事件计数降级和隔离 install→uninstall 回归；不推进匹配分档、版本演进、真实 Hook 或 8080。

## 追加：受控 onboarding 纵向切片

以 `f8af00e` 为代码基线，串接已有发现实例、`matcher` 精确复用、Goose 候选配方、授权范围内的隔离 workspace-plugin 安装事务、真实事件验证和机器可读经验库。只支持已登记的 project 作用域 OpenCode workspace-plugin；其他接入方式保留为调查/不支持，不执行模型输出的命令。

验收只覆盖隔离临时工作区和已有真实观测接收器的只读核对；不启用真实 Goose 外发、不安装到现用 Agent、不改全局配置、不重启 Agent、不操作 8080。完成后停在 review，明确区分真实扫描/事件、模拟 Goose 配方和未完成的通用 Agent 闭环。

## 2026-09-10 review follow-up

以 `8dfe6a0` 为文档基线，本轮实现提交为 `a90e0c8`。范围是把受控 onboarding 的执行来源、prior 经验、exact 扫描路径和激活状态继续收紧：仅 supervisor 传入的 Goose 来源可进入可复用配方；经验读取只返回当前 PID+create_time 的生命周期摘要；exact 扫描走与新调查相同的授权、幂等安装和验证路径；manifest 撤销或 runid 改变不会被历史事件提升为 Hook 已验证。

验收保持隔离：8081 单独运行目录和指纹库，观测接收器使用既有隔离 OpenCode workspace；不动 8080、全局配置和现用 Agent。真实 Goose 仅在随机合成目标上尝试，凭据缺失时在模型请求前明确失败，不能计为调查成功。完成后停在 review，不进入 Hook 安装阶段。

## 2026-09-10 真实 Goose 复核追加

在已授权配置源 `/Users/mac/个人项目/asg-os-sensor/.env` 做内存加载后，使用 `ASG_INSECURE_SSL=1` 与 `ASG_ALLOW_INSECURE_ANALYST=1` 仅对指定 Lenovo gateway 的隔离调查完成两轮真实 Goose 调查。目标、指纹库、经验、审计和证据均位于新的 `artifacts/stage1/real-goose-live-6UDd4Dsj/`；自动安装保持关闭，现用 Agent、全局配置和 8080 不动。第一轮成功保存候选配方，中间真实 MCP 读取到同一实例 prior，第二轮在相同 PID+create_time 上成功读取 prior 后再次完成调查。此结果证明真实调查和 prior 读取链路，不等于通用 Agent 或 Hook 闭环完成。

## 2026-09-10 review milestone：兼容历史与真实 OpenCode 观测证据

本次基线为 `8dc06d9`，范围仅包含兼容条件下的跨实例 prior 查询、回环观测证据工具、脚本启动的隔离路径边界及对应回归；使用现有真实 OpenCode 实例进行只读 Goose 调查。自动安装保持关闭，不重启或重新安装现用 Agent，不操作 8080。交付后停在 review 点，完整 exact/similar/miss 调度和 revision 演进留待下一步。

验收标准：兼容 executable/entry/build/launch 的新 PID 可读历史并保留 revision/source；入口或构建变化不命中；MCP 子进程不回退默认 recipe/fingerprint 库；Goose 可看到绑定 PID+create_time 的观测健康和事件类型，但不会把 stale 或事件历史当作当前 Hook 生效；原生二进制仍不要求脚本 entry token。

## 2026-09-10 review follow-up：加载器证据与隔离验收准备

基于 `b00bf10`，本次只补充加载器/配置/插件作用域的只读证据、隔离验收工作区和真实启动可行性核对，不把当前工作区 CWD 当安装范围，不重启现用 Agent，不操作 8080。验收交付为：用户可打开的独立工作区、明确的插件部署路径、已有真实事件接收路径，以及启动探测是否安全可复现的证据。

范围外仍包括真实 Hook 推广安装、阻断、完整资产采集和 Stage1 的 exact/similar/miss 与 revision 闭环；新工作区在用户打开前应保持无事件，不能用旧实例历史冒充新实例握手。

## 2026-09-10 review continuation：路由级 TLS 配置与隔离验收边界

基于实现提交 `dac0a8f`，本次增量只收紧自定义 LLM 的 TLS 兼容路径、插件导出合同测试和隔离验收证据。TLS 例外由 `llm.yaml` 当前选中 route 的 `tls.verify=false` 与 `transport=loopback-proxy` 决定；默认 route 仍校验证书，代理锁定当前 route 解析出的 origin、拒绝重定向和跨 origin 重配置。代码不硬编码供应商、route 名或上游地址，也不设置 Python/系统级 TLS 绕过。

隔离验收工作区已刷新为当前插件版本，用户打开前保持 `0` 条事件；打开后才取得新实例 PID+create_time，再启动绑定接收器。独立 headless CLI 对照证据保留在 `artifacts/stage1/`：无插件基线可用，带外部插件的实例初始化在有界时间内未完成，故不把 CLI 端口启动或静态部署写成插件已加载。

本次仍不进入真实 Hook 安装、阻断、8080 或 Stage1 闭环完成；完成后停在 review。
