# ASG 可提供信息与 API 示例

版本：2026-09-14。用途：与 AI Trust 对齐信息清单和接口，不实现新接口。

本文依据当前 dashboard、Hook 数据、调查活动、指纹和控制状态的代码及实际响应编写。第 2 节是**已经存在的读取接口**；第 3 节是**建议的统一交换格式，尚未实现**。示例值均为虚构，不是某个真实 Agent 的验收记录。

## 1. 当前可以提供的信息

“可提供”表示系统已有相应数据来源，不代表每个目标均已采集。字段缺失必须保留状态、原因和范围，不能补成成功或空清单。

| 信息域 | 可提供内容 | 当前来源与限制 |
| --- | --- | --- |
| 扫描状态 | 最近扫描时间、周期、次数、是否扫描中、自动调查开关、活跃调查、指纹数量 | `/api/state` 根字段；当前周期由运行设置覆盖 |
| 进程与身份 | PID、创建时间、实例 ID、关联进程 PID、显示名称、入口、工作目录、发现分数、理由、发现证据、身份来源 | `agents[]` 和 `adapter`；分数是发现/身份线索，不是风险评分；没有统一完整主机资产档案 |
| 角色分类 | Agent、候选、模型网关、工具服务、宿主及其他角色，判断依据 | `adapter.agent_classification`、`investigated_identity`；分类可能仍待确认 |
| 模型与网关 | 提供方、模型名、base URL、路由、配置来源、声明能力/限制、观测状态 | `assets.model_routing`、findings 的 `model_gateway`；配置存在不等于实际调用 |
| 配置 | 已解析配置及来源、有效范围 | `assets.parsed_config`；可能未调查，不能承诺提供全部配置文件 |
| MCP 与工具 | 名称、说明、插件归属/版本、传输方式、命令、参数、路径、工作目录、URL、启用/声明/运行状态、工具清单 | `assets.registered_tools_and_mcp`、findings 的 `mcp`；只返回实际查得字段；未执行 tools/list 时工具清单未知 |
| Skills | 名称、说明、SKILL.md/目录路径、插件/包归属、作用范围、来源与已知加载情况 | `assets.skills`、findings 的 `skills`；发现文件不能证明已加载或执行 |
| 规则与上下文 | 规则文件路径、已读取内容/摘要、作用范围、来源；已查得的记忆上下文 | `assets.system_prompt_rules`、`memory_context`；不保证完整系统提示词、记忆或注入顺序 |
| 网络 | 监听地址/端口、已观察连接、远端端点、调查时间和来源 | `assets.network_surface`；瞬时连接不等于完整网络历史，不承诺抓到所有 TLS 请求正文 |
| 执行活动 | 工具名、参数/结果、子进程及执行事件、最近事件时间、调用配对 | `assets.child_executions`、observation、Hook records；仅限已捕获范围 |
| 调查过程 | 状态、阶段、起止时间、退出码、结束原因、部分发现、未知项、可续查状态、队列信息 | `adapter.investigation`、`partial_findings`、pipeline；工具活动接口提供有界摘要 |
| 调查依据 | evidence_refs、来源、scope、uncertainty、open_questions、可读 summary/facts、发现历史 | findings；原始证据文件在本地，尚无通用证据下载 API；活动接口不输出模型思考或原始配置 |
| 指纹与候选配方 | 指纹 ID、特征、修订、首次/最近发现、命中次数、来源、Hook 文件计划、兼容约束及验证标记 | `/api/fingerprints`；含候选和未验证配方，不能按名称直接判兼容或声称安装 |
| 安装与接入 | 安装计划、授权状态、修改/备份/回滚信息（已有时）、重启要求、安装结果、绑定、验证检查点 | `adapter.onboarding`、`pipeline`、`hook_state`、`sink_state`；当前信任准入未单独完整表达 |
| 原始输入输出 | 用户消息、模型请求/响应、流式片段、最终回答、工具前后数据、会话生命周期及错误 | `/api/hook-data` 的 records/payload；不同 Hook 名称和字段不同，正文可能嵌套/序列化，存在空缺、截断和脱敏 |
| 采集覆盖与健康 | 事件种类及数量、缺失种类、过滤/解析失败、源文件范围、绑定数、网络覆盖、IO 验收缺口 | Hook coverage 与 observation_instances；读取窗口完整不等于端到端 IO 完整 |
| 控制及验收 | 当前策略、待确认请求、决策事件、执行回执、按实例记录的验证 | `/api/hook-control/status`，仅本机；不等于 AI Trust 已对接，不等于全部 Agent 支持阻断 |
| Goose 模型设置 | provider、base_url、模型名、有无密钥、联通测试状态及返回验证 | `/api/model-settings`；不提供密钥值，与目标 Agent 自身模型资产是两类数据 |

稳定的跨重启 Agent ID、主机/租户归属、可靠子 Agent 关系、统一五阶段映射、统一分页增量上报，都还需要协议与实现补齐。当前 `instance_id=pid:create_time` 只用于本机运行实例隔离；跨主机交换需再加部署身份。

## 2. 已存在的读取 API

本机地址 `http://127.0.0.1:8081`；局域网使用运行该服务的机器 IP。

| 方法与路径 | 请求参数 | 主要响应 |
| --- | --- | --- |
| GET `/api/state` | 无 | 扫描、全部候选/Agent/非 Agent、资产、调查、安装、观测实例 |
| GET `/api/fingerprints` | 无 | `fingerprints[]`，包含特征、修订和候选配方 |
| GET `/api/onboarding?pid=12345` | 当前扫描中的 PID | `status,target,onboarding,source` |
| GET `/api/investigation/activity?pid=12345&limit=40` | PID；可选 run_id、limit（1–100） | target、runs、run_id、status、events、truncated、limitations |
| GET `/api/hook-data?instance_id=12345%3A1789364589.29371&limit=100` | instance_id，或 pid/create_time；可选 since、until、limit、max_bytes | schema_version、status、generated_at、filter、bindings、records、events、coverage |
| GET `/api/hook-data/download?pid=12345&create_time=1789364589.29371` | 同上 | 同样的 JSON，以附件下载；仍受读取窗口限制 |
| GET `/api/model-settings` | 无 | config、test（不含密钥原文） |
| GET `/api/hook-control/status` | 无，仅本机回环访问 | policy、pending、events、verifications、enforcement_verified |
| GET `/api/native-trust` | 无 | 目标原生 Hook 定义的信任状态（`trusted`／`untrusted`）与重复注册 |
| POST `/api/native-trust/refresh` | 无 | 触发一次后台只读探测，返回 `started` 与当前缓存报告 |
| GET `/api/capability?pid=12345` | 当前扫描中的 PID | 该实例的十级能力阶梯、原生信任快照、独立运行服务状态 |
| GET `/api/recipe-bundle/export?fingerprint_id=<id>` | 指纹 ID，可选 note | 可移植配方包 JSON（含配方、构建约束、验证状态与完整性摘要） |
| POST `/api/recipe-bundle/import` | `{bundle, observed_build}` 或 `{bundle, exe, argv, cwd}` | 校验与兼容性结论，以及仍需授权、安装与独立验收的计划 |

`/api/hook-data` 的响应还包含 `conversation`（归一化后的用户／助手正文）与 `vocabulary`（已翻译与未翻译的原生事件名）。未翻译的名字不会被计入输入输出验收。

`/api/hook-events` 及其 `/download` 是 Hook 数据接口的别名。历史 Hook 数据建议使用完整实例 ID，避免只用 PID。`records` 携带来源行号、实例和时间，`events` 是对应 payload 的便捷投影，不应重复统计。

```bash
# 当前全部资产/状态
curl 'http://127.0.0.1:8081/api/state'
# 替换为 /api/state 中真实的 instance_id
curl --get 'http://127.0.0.1:8081/api/hook-data' \
  --data-urlencode 'instance_id=12345:1789364589.29371' \
  --data-urlencode 'limit=100'
```

Hook 数据响应形状（示例）：

```json
{
  "schema_version": "示例：以接口实际值为准",
  "status": "ok",
  "generated_at": "2026-09-14T10:00:00Z",
  "filter": {"instance_id": "12345:1789364589.29371", "limit": 100},
  "bindings": [],
  "records": [{
    "line": 4,
    "timestamp_iso": "2026-09-14T09:59:00+00:00",
    "event_type": "user.input",
    "instance_id": "12345:1789364589.29371",
    "target": {"pid": 12345, "create_time": 1789364589.29371},
    "payload": {"event": "user.input", "detail": {"session_id": "session-example", "parts": {"present": true, "truncated": false, "value": "[{\"type\":\"text\",\"text\":\"请总结这个项目\"}]"}}}
  }],
  "events": [],
  "coverage": {
    "complete": false,
    "observed_event_types": ["user.input"],
    "missing_event_types": ["model.response"],
    "accepted_records": 1,
    "filtered_records": 0,
    "malformed_records": 0,
    "network_capture_complete": false,
    "limitations": ["示例仅展示部分字段；bindings/events 在实际响应中含对应绑定和 payload"],
    "io_acceptance": {"end_to_end_verified": false}
  }
}
```

## 3. 建议给平台的统一 API 示例（未实现）

建议通过一个批量上报接口表达现有信息，各信息域可拆批发送。以下路径、鉴权、回执和字段名均为拟议协议，并不是可以现在调用的服务。

```http
POST /api/v1/asg/reports HTTP/1.1
Authorization: Bearer <platform-issued-token>
Content-Type: application/json
```

```json
{
  "schema_version": "asg-report.example.v1",
  "report_id": "report-example-001",
  "generated_at": "2026-09-14T10:00:00Z",
  "collector": {
    "deployment_id": "deployment-example",
    "host_id": null,
    "scan": {"interval_seconds": 3001, "last_scan_at": "2026-09-14T09:58:00Z", "running": false}
  },
  "subjects": [{
    "subject_ref": "deployment-example/12345:1789364589.29371",
    "platform_agent_id": null,
    "instance_id": "12345:1789364589.29371",
    "identity": {
      "name": "Example Agent",
      "version": "1.0",
      "roles": ["agent"],
      "classification": "confirmed_agent",
      "evidence_refs": ["ev-example-1"]
    },
    "process": {
      "pid": 12345,
      "create_time": 1789364589.29371,
      "related_pids": [12345, 12346],
      "entry": "/opt/example/bin/agent",
      "cwd": "/workspace/project"
    },
    "discovery": {"score": 70, "reasons": ["入口与运行证据"], "evidence": {}},
    "relationships": {"parent_subject_ref": null, "status": "unknown"},
    "assets": [
      {"kind": "model_gateway", "status": "collected", "items": [{"name": "provider-example", "model": "model-example", "base_url": "https://gateway.example/v1", "activation": "configured"}], "evidence_refs": ["ev-example-2"]},
      {"kind": "mcp", "status": "collected", "items": [{"name": "filesystem", "description": "读取工作目录文件", "transport": "stdio", "command": "node", "args": ["/opt/example/mcp/server.js"], "cwd": "/workspace/project", "source_path": "/workspace/project/mcp.json", "plugin": "example-plugin", "plugin_version": "1.0", "activation": "configured", "tools": null}], "uncertainty": ["未获取 tools/list，实际工具清单未知"]},
      {"kind": "skills", "status": "collected", "items": [{"name": "review", "description": "项目审查说明", "path": "/workspace/project/skills/review/SKILL.md", "scope": "project", "activation": "unknown"}]},
      {"kind": "rules", "status": "collected", "items": [{"path": "/workspace/project/AGENTS.md", "summary": "项目协作规则", "scope": "project", "activation": "unknown"}]},
      {"kind": "network", "status": "collected", "items": [{"address": "127.0.0.1", "port": 9000, "state": "LISTEN"}]},
      {"kind": "parsed_config", "status": "not_collected", "items": null},
      {"kind": "memory_context", "status": "unknown", "items": null},
      {"kind": "executions", "status": "not_collected", "items": null}
    ],
    "investigation": {
      "status": "completed",
      "phase": "assets",
      "started_at": "2026-09-14T09:50:00Z",
      "ended_at": "2026-09-14T09:58:00Z",
      "summary": "已保存部分资产，运行情况尚待验证",
      "open_questions": ["MCP 是否激活"],
      "activity": [{"type": "tool_completed", "tool": "read_config", "status": "succeeded", "evidence_id": "ev-example-2"}]
    },
    "fingerprint": {"id": "fingerprint-example", "revision": 1, "match_status": "exact", "recipe_status": "structure_validated_hook_unverified"},
    "onboarding": {
      "status": "installed",
      "restart_required": true,
      "native_trust": {"status": "unknown", "reason": "当前数据尚无统一信任准入字段"},
      "hook_state": "waiting_for_events",
      "control_verified": false
    },
    "observation": {"accepted_records": 0, "last_event_at": null, "io_complete": false, "limitations": ["当前实例尚无事件"]}
  }],
  "runtime_records": [],
  "fingerprints": [],
  "control": {"policy": null, "pending": [], "events": [], "verifications": []},
  "evidence": [{"id": "ev-example-2", "source_type": "config", "source_path": "/workspace/project/mcp.json", "scope": "项目配置", "summary": "发现一个 MCP 声明", "raw_download_url": null}],
  "service": {
    "analyst_model": {"provider": "openai-compatible", "model": "model-example", "base_url": "https://gateway.example/v1", "has_key": true},
    "model_test": {"status": "unknown"}
  },
  "limitations": ["所有值均为虚构格式示例", "稳定身份和统一字段转换尚未实现"]
}
```

拟议响应：

```json
{"report_id":"report-example-001","status":"accepted","subject_mappings":[{"subject_ref":"deployment-example/12345:1789364589.29371","platform_agent_id":"platform-agent-example"}],"errors":[]}
```

上述 `runtime_records` 承载第 2 节的原始 `records`，附上部署身份；`fingerprints` 承载指纹/配方；`control` 承载现有控制状态。平台可以分别消费，不要求将所有原始文件或配方脚本放入每个资产上报。

### 字段转换和缺失约定

- `model_gateway ← adapter.assets.model_routing`；`mcp ← registered_tools_and_mcp`；`rules ← system_prompt_rules`；`network ← network_surface`；`executions ← child_executions`。Skills 和其他字段以当前资产/调查 findings 为源。
- 所有资产支持 `status、items/value、source、evidence_refs、scope、uncertainty、open_questions、display`。示例只展示部分公共字段；非统一原值可保留在 `raw_value`，避免转换丢失信息。
- `collected` 表示查得信息；`empty` 仅表示检查范围内为空；`unknown/not_collected` 使用 null，不能替换为“没有”。同一资产另用 activation 区分声明、加载、实际运行和未知。
- 非 Agent 沿用 subjects 的身份、进程、资产和调查结构，roles 写对应类别；无需附 Agent 的 onboarding/control 能力。
- 用户原文、组合后的模型请求、模型响应、最终回答必须分别表达。当前 `user.prompt.submitted` 等原生别名尚需统一，不能把所有 prompt 当用户原文。
- 会话、请求、工具调用和父子关系只使用来源提供或可靠关联的信息；拿不到填 null。不能补造 event_id、序号或完整性计数来通过验收。
- 原始凭据不进入交换格式；不返回 API Key、Bearer Token、认证环境变量值。文本数据按当前脱敏策略输出，不承诺包含未脱敏的所有原文。
- 时间、分页截断、过滤原因和覆盖缺口必须随数据传递。当前接口是快照/有界读取，没有已实现的持久游标或消息可靠投递承诺。

## 4. 当前访问说明

页面服务已配置为 IPv4 全接口监听 `0.0.0.0:8081`。同局域网使用机器 IP，`127.0.0.1` 仅指访问者自己。

Hook 控制接口仍按现有代码限制本机回环访问；局域网可看页面、资产和 Hook 数据，但控制策略/确认区域会收到 403。页面其他管理接口尚无统一登录鉴权，本次仅开启局域网展示服务，没有实现对外平台接口或公网发布。
