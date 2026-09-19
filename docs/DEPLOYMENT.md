# 部署、协议接入与验证

本版本提供 macOS、Linux 和 Windows 的部署入口。脚本安装运行依赖、准备独立数据目录、启动服务并检查 HTTP；不需要复制开发者的 `.env` 或指纹库。Windows 的实际运行结果以 Windows Runner 的报告为准，不能用 macOS 测试代替。

## 1. 下载与一键部署

安装 Git，克隆仓库并进入目录：

```text
git clone https://gitlab.xpaas.lenovo.com/aitrust/incubator/asg-os-sensor.git
cd asg-os-sensor
```

macOS / Linux：

```bash
bash setup.sh
```

Windows：双击 `setup.bat`，或在 PowerShell 执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

安装入口要求 Python 3.10+；缺失时，Windows 尝试 winget，macOS 尝试现有 Homebrew，Debian/Ubuntu 尝试 apt。系统包管理器不可用或没有安装权限会明确报错，不会伪装部署成功。Linux 需 Python venv 组件。其他发行版先安装 Python 与 venv/pip。

脚本优先复用 `ASG_GOOSE_BIN` / PATH 上已有 Goose，否则从 block/goose 官方固定版本 v1.50.1 下载对应 CLI、校验官方 SHA256 后安装到 `.tools`，并执行版本检查。下载需访问 GitHub；Python 包需访问包镜像。现有 Goose 版本由使用者管理。仅部署协议通道可传 `--skip-goose`，Windows 使用 `-SkipGoose`；这种模式没有模型调查回退。

成功时显示 `Ready: http://127.0.0.1:8081`。页面里填写 Goose 的模型服务地址、模型名和密钥，执行联通验证，实际返回消息后再使用模型调查。模型服务与账号凭据不能由安装脚本替用户生成。

## 2. 自动安装的范围

默认发现与调查启用，自动修改目标配置关闭。部署时指定允许安装 Hook 的目录即可开启：

```bash
bash setup.sh --port 8081 --allow-root /absolute/path/to/agent/workspace
```

```powershell
.\setup.ps1 -Port 8081 -AllowRoot 'C:\Agent Workspaces'
```

路径可以有空格，可指定多个根目录。目录必须已经存在。发现范围与允许修改范围不同；自动安装只能写授权目录。全局 Agent 配置在另一个目录时，需要把其配置目录也明确加入。

首次设置保存到 `deployment.local.json`，重跑 setup 保留已有配置，不会用参数覆盖它。修改部署端口、安装范围或局域网设置时，编辑此文件，再 stop/start。不要将这个含本机路径的文件提交。

局域网部署：首次加 `--lan` / `-Lan`，监听 `0.0.0.0`。看板用于可信局域网，没有完整的多用户访问控制；控制与 OTLP 通道仍校验独立凭据。系统防火墙需允许选定端口入站，脚本不自动修改防火墙。

## 3. 启停和诊断

macOS/Linux 使用 `.venv/bin/python`；Windows 使用 `.venv\Scripts\python.exe`：

```text
<venv-python> deploy.py status
<venv-python> deploy.py doctor
<venv-python> deploy.py stop
<venv-python> deploy.py start
```

旧 `start_dashboard.sh` / `start_dashboard.bat` 也调用 `deploy.py start`，要求先完成 setup。它们不再使用另一套依赖安装或模型探测流程。

服务后台运行，日志 `data/dashboard.log`，持久状态 `data/`。重复启动不会创建第二个进程；端口被其他服务占用会失败。停止操作只处理当前部署记录中 PID、启动时间和入口匹配的服务。当前脚本不注册开机服务，操作系统重启后执行 start。

升级前备份 `data/`、`deployment.local.json`、`.env` 和目标安装事务目录。stop → 更新代码 → setup → doctor。已安装 Hook 仍需按当前文件版本重新验证，不能继承旧版本验收。

离线部署：在同操作系统、架构、Python 版本的联网机器执行 `python -m pip download -r requirements.txt -d vendor/wheels`，复制源码及 wheels；设置本机 Goose 路径，运行 setup 的 `--offline` / `-Offline`。协议专用环境同时加 skip-goose。

## 4. 协议优先接入的具体行为

| 通道 | 自动发现/安装 | 收到什么 | 控制边界 |
|---|---|---|---|
| 命令式 Hooks | 从目标打开文件、入口、项目目录、配置环境路径读取 JSON/JSONC/TOML；跟随 hooks.files/config_files；唯一兼容配置且授权后事务安装 | 当前配置已声明事件中的用户输入、助手输出、工具前后和加载事件 | PreToolUse 同步等待；拒绝返回 deny/exit 2，目标是否遵守独立验收 |
| ACP v1 | 发现 agent_servers 的 command+args，唯一配置且授权后包装启动命令；保留 env 等其他字段 | 新子进程的 initialize 握手、prompt、流式消息、工具更新、权限请求；结束后拼接助手文本 | 仅 request_permission；ASG 拒绝直接回拒绝选项，无选项时取消。ASG 放行仍保留原客户端权限处理 |
| OpenTelemetry | 识别目标配置中的 otel/opentelemetry；提供 OTLP/HTTP JSON、protobuf logs/traces 接收端 | 原始遥测、trace/span 标识；存在 GenAI input/output messages 时提取模型内容 | 只观测，不具备阻断能力 |

可独立运行只读协议发现，不调用 Goose、不执行扫描到的命令：

```text
<venv-python> -m runtime.protocol_discovery --pid <目标PID>
```

输出候选家族、来源路径、事件名、检查范围、错误与截断状态，不输出配置里的密钥或命令正文。

不根据产品名称安装。没有现成 Hook 条目、配置多义、未知 ACP 声明格式、未加载或自检缺口，会交给 Goose 调查。JSONC 安装会保留配置语义但重写为格式化 JSON，注释不保留；安装事务保留原文，可回滚。TOML 使用 tomlkit 保留注释。

ACP 需要拥有实际传输，不能从另一个已运行的桌面聊天会话旁路接管。自动包装后，下次由客户端启动的新子 Agent 独立登记 PID 与启动时间；父客户端不继承其验证。也可手动把 ACP 客户端原启动命令替换为：

```text
<venv-python> <repo>/runtime/acp_bridge.py --run-dir <repo>/data --control-config <repo>/data/hook-control-client.json -- <original-acp-executable> <original-arguments>
```

控制配置由运行中的 ASG 控制契约接口生成；没有控制配置时省略该参数，桥接只观测。不要把桥接输出重定向进日志：stdout 是 ACP JSON-RPC 协议通道，日志由桥接自行保存。

OTLP 导出配置：指向看板或独立 Hook 服务的 `/v1/traces` 和 `/v1/logs`，协议 `http/protobuf` 或 `http/json`，HTTP 头 `Authorization: Bearer <data/hook-control.token 内容>`。先通过现有实例绑定登记目标；资源字段包含 `process.pid`，或完整 `asg.instance.id=PID:create_time`。没有精确活动实例或时间不符的记录计入 partialSuccess 拒收。支持的日志与 trace 原样保留，GenAI 内容可缺失，不据此宣称完整对话已采集。当前不提供 gRPC、压缩载荷和 metrics 接收，也不擅自覆盖目标 OTel 导出目的地。

## 5. 可复现验收

```text
<venv-python> -m unittest test_protocol_transports test_protocol_fastpath test_acceptance_integrity
<venv-python> -m unittest discover -p "test_*.py"
```

前一组验证：匿名配置结构识别、JSONC/TOML 引用、边界过滤、安装保留及回滚、真实 ACP 子进程往返、流式拼接、取消不算完整、权限拒绝、OTLP JSON/protobuf、旧 PID 拒收、多通道不覆盖、对账缺失计为失败。它们是协议工程测试，不冒充真实产品聊天与阻断验收。

部署验收要求：全新路径 setup 成功；doctor 的依赖项全部 true、http_ready=true；连续两次 start 的 PID 相同；stop 后该 PID 退出；再次 start 返回健康页面。测试目录包含空格与中文。

GitLab 提供 Linux 和可选 Windows 测试作业。Windows Runner 必须是 PowerShell shell executor、安装 Python 3.10+，标签与 `ASG_WINDOWS_RUNNER_TAG` 匹配。设置 CI 变量 `ASG_RUN_WINDOWS_TESTS=1` 才启用；没有 Runner 时应记为未验收，不能写 Windows 实测通过。

真实接入仍按实例在「Hook 实时数据」查看八项自检。没有完整模型内容、操作前等待或独立阻断效果时，只报告实际已采集能力。
