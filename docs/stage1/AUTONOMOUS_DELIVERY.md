# 本机自主接入交付

入口：http://127.0.0.1:8081/

## 使用方式

服务持续扫描新进程；候选自动进入资产调查，资产保存后自动进入 Hook 学习。角色被调查为基础设施的实例结束于资产阶段。生成的文件方案只在部署批准的工作区中自动安装。需要启动时加载的插件显示等待加载，正常重开 Agent 后按实测构建与启动参数复用、重新绑定。收到加载及工具前后事件后显示真实活动。

目前安装范围是本仓库 `artifacts/autonomous-demo/workspace`。现用 Codex、ZCode 等应用可自动只读调查，但不自动修改其全局配置。将其他项目加入范围前，需要明确批准该目录；不按模型提出的任意路径扩大范围。

## 运行与恢复

使用本机已安装的 Python 3.14，在仓库目录执行：

```sh
/opt/homebrew/bin/python3 service.py status --config artifacts/autonomous-service/settings.json
/opt/homebrew/bin/python3 service.py start --config artifacts/autonomous-service/settings.json
/opt/homebrew/bin/python3 service.py stop --config artifacts/autonomous-service/settings.json
```

`start` 对已运行的本服务幂等。状态、资产阶段进度、指纹、安装事务和每个实例的观测绑定保存在 `artifacts/autonomous-service`，服务重启后恢复。退出实例保留事件历史，不借给新 PID。

## 重复真实工具调用

测试驱动调用原版 OpenCode API，任务是读取演示目录的 `demo.txt`，不写入 Hook：

```sh
/opt/homebrew/bin/python3 e2e/real_autonomous_demo.py ask
/opt/homebrew/bin/python3 e2e/real_autonomous_demo.py restart
/opt/homebrew/bin/python3 e2e/real_autonomous_demo.py ask
```

重开后需等待服务扫描完成。接入方案与观测配置由服务处理，不需要手工填写 PID 或点击调查。新实例的资产独立调查；已兼容的 Hook 不重复完整学习。

验收脚本 `e2e/verify_autonomous_delivery.py` 核对当前 PID/启动时间、加载事件、真实 read 前后配对、安装文件与 Goose 候选内容完全一致，以及重开实例无新增 Hook 调查。

## 边界

- 当前是本机 PoC，不保证所有 Agent 都存在可用扩展点，也不承诺第一次启动后立即热加载。
- 文件观测显示已发生的工具事件及进程存活，不代表完整行为覆盖、阻断能力或持续心跳健康。
- 精确复用范围包含构建、启动参数和工作目录；版本、参数或目录变化会重新调查，尚不推广跨工作区方案。
- 模型调用与原版 SDK 是验收环境配置；不算自动发现的接入答案。Hook 内容必须来自真实 Goose 调查。
- 原始调查、模型输出和事件保留在本地忽略目录，不纳入源代码提交。

## 本次实际验收（2026-09-11）

- 从空指纹库启动新服务，没有目标 PID 过滤。服务启动后正常重开隔离 OpenCode，自动发现原始目标 PID 83659。
- 资产阶段保存了模型/网关资料和各资产组的范围结论；后续 Hook 阶段由真实 Goose 生成 2 个文件。56 次工具调用后产出候选，通用执行器自动安装，没有人工写入或修改 Hook 内容。
- 原始调查：`artifacts/autonomous-service/pid_83659_1789108360895/`。资产调查：同目录树下 `pid_83659_1789108108940/`。
- 方案明确需要下次启动加载。第一次重开 PID 87758 自动复用、重新绑定，取得 5 条有效事件与一次 read 前后配对。
- 第二次重开 PID 88774 再次自动复用；没有任何该实例的 Hook 调查，只有独立的资产调查。已校验工具实际返回 demo.txt 的内容，且 2 个安装文件字节仍与 Goose 候选完全一致。
- 服务重启后仍恢复 PID 88774 的资产和真实活动，旧实例保持历史记录，未触发重复调查。浏览器实际显示“已加载 · 工具事件已接通”“read · 1 次前后配对”，当前无执行中的调查。
- 当前实例读到 5 条有效事件；日志中另外 5 条属于上一个实例，按实例绑定被排除，未冒充新实例活动。
- 最终资产范围：模型/网关已采集；MCP 仍有待确认项；Skill 和规则在检查范围内为空。并非全部资产齐全。
- Codex、ZCode 也经过自动调查并产生候选方案，但未在其现用目录安装，不能算第二种 Agent 的完整挂接验收。

验收摘要：`artifacts/autonomous-demo/first-verification.json`、`reuse-verification.json`、`restored-verification.json`。最后一份同时核对服务恢复、文件实际内容、安装字节和无新增 Hook 调查。

验证：305 项全量回归通过；之后的阶段/生命周期专项回归通过，历史渲染修复的 9 项专项回归通过。全量检查中发现的两条过期测试（进程替身缺少 environ；旧界面文案断言）已同步修正。原始事件和真实模型验证独立于这些离线测试。
