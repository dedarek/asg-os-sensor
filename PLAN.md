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
