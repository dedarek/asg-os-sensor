# ASG · Agent 发现、接入与运行治理

ASG 从本机进程发现候选 Agent，识别协议或兼容配方，调查缺失信息，安装 Hook，并展示当前实例的资产、真实输入输出与控制结果。当前是 PoC/MVP；安装、加载、正文采集和实际阻断分别验收。

## 启动和使用

首次部署：

- macOS / Linux：`bash setup.sh`
- Windows：双击 `setup.bat`，或执行 `powershell -ExecutionPolicy Bypass -File .\setup.ps1`

打开 http://127.0.0.1:8081/，配置模型并验证返回消息，然后扫描。自动安装 Hook 需要指定允许修改的目录，详见 [部署说明](docs/DEPLOYMENT.md)。

后续统一使用 `deploy.py` 管理服务，macOS/Linux 的解释器为 `.venv/bin/python`，Windows 为 `.venv\Scripts\python.exe`：

```text
<venv-python> deploy.py start
<venv-python> deploy.py status
<venv-python> deploy.py doctor
<venv-python> deploy.py stop
```

旧 `start_dashboard.*` 仅转发到上述 start，不再另装依赖或启动另一套配置。开发时需要前台调试，可直接运行 `python monitor_dashboard.py`；这条入口使用环境配置，不读取部署配置文件。

## 只需先看这三份资料

| 目的 | 入口 |
|---|---|
| 安装、配置、启停、升级 | [部署说明](docs/DEPLOYMENT.md) |
| 架构、页面、API、状态语义、排障 | [项目指南](docs/PROJECT_GUIDE.md) |
| 协议优先、模型补充、配方复用 | [接入设计与验证记录](docs/THREE_LAYER_ONBOARDING.md) |

[发布记录](RELEASE.md)描述版本范围；`docs/stage1/` 和 [历史资料](docs/archive/) 保留阶段证据，不作为当前部署指令。旧批次通过不能替代当前实例验收。

## 代码怎么找

| 位置 | 职责 |
|---|---|
| `deploy.py`、`setup.*` | 统一部署与服务管理 |
| `monitor_dashboard.py` | 扫描、调查调度、页面 HTTP 和 API |
| `web/dashboard.html` | 页面、样式与前端交互；无前端构建依赖 |
| `asg_os_sensor.py`、`runtime/identity.py`、`runtime/analyzer.py` | 进程采集、候选发现、归属与结构证据 |
| `runtime/protocol_*.py`、`runtime/integration_protocol.py` | 已知协议发现、直接接入和格式归一化 |
| `runtime/analyst_*.py`、`recipes/` | Goose 工具与调查指令 |
| `runtime/matcher.py`、`runtime/learned_*.py`、`runtime/onboarding.py` | 指纹、配方、安装及复用 |
| `runtime/hook_*.py`、`runtime/observation_*.py`、`hook_runtime.py` | 实例事件、控制和独立 Hook 服务 |
| `test_*.py` | 单元与本地集成回归，保留原测试入口 |
| `e2e/` | 验收程序；真实目标脚本需按场景单独运行 |
| `tools/diagnostics/`、`e2e/legacy/` | 实验盘点与早期模拟演示，不参与正常启动 |
| `data/`、`artifacts/`、`e2e/artifacts/` | 本地运行数据与证据，不作为新增提交内容 |

## 验证

本地代码回归：`python -B -m unittest discover -p 'test_*.py'`。

干净目录部署检查：`python e2e/verify_deployment.py`。它会创建临时部署、安装 Python 依赖、启停隔离服务，不安装 Goose。

真实 Agent 验收清单：`python e2e/run_all_acceptance.py --list`。先查看目标和条件；单元测试、模拟器和真实实例验收分别记录。

## 当前边界

协议和兼容配方优先，缺口交给模型调查；Goose 生成候选不等于接入成功。控制只有在目标执行前等待决定且拒绝后没有副作用时才算通过。Windows 脚本存在不等于所有 Windows Agent 已通过验收。

运行日志可能含会话正文和凭据。保留本地证据，发布前检查提交内容；不将私有运行目录、`.env` 和部署配置上传。
