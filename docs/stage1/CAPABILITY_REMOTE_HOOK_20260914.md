# 能力阶梯、输入输出归一化与独立运行服务（2026-09-14）

这份文档记录本轮针对会议讨论中“说得比实现更满”的几条所做的实现。每一条都只声明到当前证据支持的程度。

## 一、解决了什么

| 会上说满的说法 | 本轮实现 |
| --- | --- |
| 挂上 Hook 就能拿到工作流、输入输出并控制 | 增加十级能力阶梯，逐级显示“已验证／待生效／被阻断／不支持／无证据／未到达”，把“装上了”和“看得到输入输出、能控制”分开 |
| 装完以后发现程序可以删掉 | 新增独立运行服务，不扫描、不调查、不学配方，只读取已绑定实例的事件并回答控制决策 |
| 填一个平台地址就能拿信息流、随时阻断 | 控制接口支持带令牌的远程访问。我们自己的地址可填，AI Trust 未接通 |
| 重启后 Hook 还在就等于被接管 | 只读探测目标原生 `hooks/list`，把 `trusted`／`untrusted` 作为独立一级；重启不再被当作修复 |
| 一个用户学出指纹，其他人复用 | 新增可移植配方包：导出配方、构建约束与验证状态；导入时校验完整性与构建一致性，并明确仍未安装 |
| “接进去”到底指什么 | 接入阶梯给出一致定义：发现→调查→方案→安装→原生准入→加载→事件观测→输入输出→执行控制→独立运行 |

“没有原生 Hook 也能造”不属于本轮范围，它仍是研究方向，本文件不作任何能力声明。

## 二、代码位置

新增：

- `runtime/capability.py`：能力阶梯，纯投影，无 I/O。
- `runtime/event_vocabulary.py`：把生产者原生事件名翻译成统一词表；无法翻译的名字原样上报，不猜、不丢。
- `runtime/native_trust.py`：只读探测目标原生 Hook 信任状态，带缓存与后台刷新。
- `runtime/hook_service.py`：独立运行服务（事件 + 控制 + 对话 + 实例清单）。
- `runtime/recipe_bundle.py`：配方包的导出、校验、兼容性比对与导入计划，含 CLI。
- `hook_runtime.py`：独立服务的启停脚本（与 `service.py` 分离）。

修改：

- `runtime/hook_control.py`：控制接口从“只允许本机”改为“本机始终允许；远程需要 `ASG_HOOK_REMOTE=1` 且提供同一 Bearer 令牌”。
- `runtime/hook_data.py`：在 Hook 数据响应中增加 `conversation`（归一化对话）与 `vocabulary`（翻译覆盖）。
- `runtime/io_acceptance.py`：验收按归一化事件名计数，避免把 `user.prompt.submitted` 判成“缺少用户输入”。
- `monitor_dashboard.py`：新增 `/api/native-trust`、`/api/capability`、`/api/recipe-bundle/export`、`/api/native-trust/refresh`、`/api/recipe-bundle/import`；卡片与抽屉展示能力阶梯。

测试：`test_capability.py`、`test_event_vocabulary.py`、`test_recipe_bundle.py`。

## 三、怎么运行独立服务

```bash
python3 hook_runtime.py start --config artifacts/autonomous-service/hook-runtime.json
python3 hook_runtime.py status --config artifacts/autonomous-service/hook-runtime.json
python3 hook_runtime.py stop --config artifacts/autonomous-service/hook-runtime.json
```

设置文件字段：`ASG_RUN_DIR`、`ASG_HOOK_HOST`（默认 `0.0.0.0`）、`ASG_HOOK_PORT`（默认 `8099`）、`ASG_HOOK_REMOTE`。

服务写自己的状态到 `<ASG_RUN_DIR>/hook-runtime-state.json`，与设置文件分离，运行中的服务不会覆盖启动它的配置。

它复用 dashboard 的读取器与控制引擎，不存在第二份数据契约。停止主服务（扫描与 Goose）后，它仍然服务已经绑定的实例。

## 四、怎么验证

```bash
# 1) 独立服务在运行，且不依赖扫描
curl -s http://127.0.0.1:8099/health
curl -s http://127.0.0.1:8099/api/instances

# 2) 远程访问需要令牌
TOKEN=$(cat <ASG_RUN_DIR>/hook-control.token)
curl -s -o /dev/null -w '%{http_code}\n' http://<本机IP>:8099/api/instances          # 期望 403
curl -s -H "Authorization: Bearer $TOKEN" http://<本机IP>:8099/api/instances       # 期望 200

# 3) 输入输出归一化
curl -s 'http://127.0.0.1:8081/api/hook-data?pid=<PID>&limit=300'   # 看 conversation / vocabulary

# 4) 能力阶梯
curl -s 'http://127.0.0.1:8081/api/capability?pid=<PID>'

# 5) 原生信任
curl -s http://127.0.0.1:8081/api/native-trust

# 6) 配方包
export ASG_RUN_DIR=<运行目录> ASG_FINGERPRINT_DB=<运行目录>/fingerprints.json
python3 -m runtime.recipe_bundle list
python3 -m runtime.recipe_bundle export --fingerprint-id <ID> --out /tmp/bundle.json
python3 -m runtime.recipe_bundle inspect --file /tmp/bundle.json
```

单元测试：

```bash
python3 -m unittest test_capability test_event_vocabulary test_recipe_bundle
```

## 五、本轮验证到的真实结果

在编写本文件时的本机环境：

- 独立服务在 `0.0.0.0:8099` 运行，无扫描即列出 19 个已绑定实例。
- 远程无令牌返回 403，带令牌返回 200。
- 原生信任探测（约 0.2 秒）得到 10 个匹配 Hook 全部 `untrusted`，并识别出 `preToolUse`／`postToolUse`／`sessionStart` 在 `config.toml` 与 `hooks.json` 中重复注册。
- 某实例在归一化后可以取出用户原文；未翻译的原生事件（如 `assistant.stream.delta`、`model.request.messages`）被列为未翻译，而不是计入通过。
- 该实例的执行控制显示为“不支持”，符合其挂接位置的能力，没有被写成已验证。

## 六、仍然不宣称

- 不宣称任何实例的输入输出“完整无遗漏”：端到端完整性仍需独立入口对账。
- 不宣称已经接通 AI Trust：只提供了带凭证的远程入口。
- 不宣称跨机器复用已验证：只提供了配方包与兼容性判断，接收方仍需授权、安装与独立验收。
- 不宣称自动化批准原生信任：信任审阅必须由目标自身的用户完成。
- 不宣称对所有 Agent 通用：以上均在具体实例上计算，结论不继承。
