# 交付说明（2026-09-15）

验收批次：ASG-BATCH-20260914-180121；机器：01a329ad376e9a7f。

## 一、必须保留的常驻组件（逐项）

1. `hook_runtime.py` — 独立运行服务的启停脚本（start/stop/status），含进程身份校验
2. `runtime/hook_service.py` — 独立运行服务本体：事件、对话、控制决策；不扫描、不调查、不学配方
3. `runtime/hook_control.py` — 同步决策引擎（放行/拒绝/人工确认、fail-closed、幂等）
4. `runtime/hook_control_client.py` — 目标侧决策客户端（Hook 通过它同步获取决策）
5. `runtime/hook_data.py` — 有界日志读取与归一化（含 conversation 与词表覆盖）
6. `runtime/observation_registry.py` — 观测绑定登记（实例级历史绑定）
7. `runtime/observation_source.py` — 观测源校验与快照
8. `runtime/event_vocabulary.py` — 原生事件名到统一词表的翻译
9. `runtime/learned_install.py` — 文件计划安装与回滚（备份/原子写）
10. `runtime/file_lock.py` — 跨进程单写者文件锁（独立于匹配器，供安装与指纹库共用）
11. `runtime/analyst_evidence.py` — 载荷脱敏
12. `runtime/hook_acceptance.py / runtime/io_acceptance.py` — 输入输出验收契约（计数/配对/检查点）

运行需要的文件与配置：

- settings：`/Users/mac/个人项目/asg-os-sensor-advisor-20260911/artifacts/autonomous-service/hook-runtime.json`
- runtime_state：`/Users/mac/个人项目/asg-os-sensor-advisor-20260911/artifacts/autonomous-service/hook-runtime-state.json`
- control_client_config：`/Users/mac/个人项目/asg-os-sensor-advisor-20260911/artifacts/autonomous-service/hook-control-client.json`
- control_token：`/Users/mac/个人项目/asg-os-sensor-advisor-20260911/artifacts/autonomous-service/hook-control.token`
- bindings：`/Users/mac/个人项目/asg-os-sensor-advisor-20260911/artifacts/autonomous-service/observations.json`
- bound_log_example：`artifacts/autonomous-demo/workspace/.opencode/asg-logs/decision-hook-events.jsonl`

运行依赖（闭包检查）：hook_runtime.py、runtime/hook_control_client.py、runtime/hook_service.py、runtime/observation_registry.py、runtime/learned_install.py、runtime/file_lock.py、runtime/observation_source.py、runtime/hook_data.py、runtime/event_vocabulary.py、runtime/io_acceptance.py、runtime/hook_control.py、runtime/hook_acceptance.py、runtime/analyst_evidence.py

解释器：/Library/Developer/CommandLineTools/usr/bin/python3

对发现模块的运行时依赖：无

## 二、可以删除的发现部分

- monitor_dashboard.py 的进程扫描与展示部分 —— 仅在发现/展示阶段需要，可退出
- Goose 调查与配方学习（recipes/、analyst_*） —— 学习阶段需要，运行阶段不需要
- find_agents*.py、matcher.py（指纹匹配与扫描入口） —— 发现/匹配入口；运行闭包已不再依赖它（锁已独立到 file_lock.py）
- runtime/llm_proxy.py 的捕获配置 —— 仅在显式测试网关下需要

## 三、启停与状态

```bash
# start
python3 hook_runtime.py start --config artifacts/autonomous-service/hook-runtime.json
# status
python3 hook_runtime.py status --config artifacts/autonomous-service/hook-runtime.json
# stop
python3 hook_runtime.py stop --config artifacts/autonomous-service/hook-runtime.json
```

服务端点：/health、/api/instances、/api/hook-data、/api/conversation、/api/hook-control/status、/api/hook-control/decision

## 四、最近独立运行批次记录（以验收总表判定为准）

- 独立运行 0 分钟，采样 0 次（0 次表示本轮未执行耐久测试）
- 期间完成 30 轮对话、70 次工具调用
- 停服期间产生 728 条事件，恢复后 9.171534061431885 秒补齐
- 闭包式安装（仅复制运行依赖到新目录）：启动正常并服务 33 个已绑定实例；安装目录中无发现模块文件（[]）

## 五、任务签收状态

- 任务 1：通过
- 任务 2：未执行
- 任务 3：通过
- 任务 4：通过
- 任务 5：未执行
- 任务 6：未执行
- 任务 7：待外部条件
- 任务 8：待外部条件

## 六、本机仍未签收

- 当前正常桌面 Codex 再完成 9 个完整回合，使独立逐字对账达到 10 回合（任务 2）
- 独立 Hook 服务连续运行并采样满 60 分钟（任务 5）
- WorkBuddy 与一个无原生 Hook 目标完成各自采集、控制和重启固定批次（任务 6）
- 在浏览器内测量事件产生到页面可见的实际延迟（统一延迟指标）

## 七、用户已明确本轮不做的外部验收

- AI Trust 测试环境 + 接入协议 + 测试凭证（任务 8 平台侧）
- 第二台机器（任务 7 跨机器验收）
- 专用测试机器（任务 5 机器重启后 60 秒恢复）
