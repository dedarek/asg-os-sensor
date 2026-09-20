# 交付项 7「安装、启动、检查、升级、卸载」——草履虫级实现手册 v2（2026-09-21）

结论先行：本项已实现并通过自动验收 17/17，绑定交付包摘要
317732f5cd8045175e14157135ba809960f8e366474d8a7d0061b76a8e259cc0
（版本 0.9.2，构建自提交 6274392，报告
/var/folders/xf/_m1f6xjn7cd55zzpvqp3r3f80000gn/T/asg-lifecycle-acceptance-8v1ix06j/report.json）。
本手册的目标是"不看代码也能照着重新实现一遍"：每个支撑函数给出输入、输出、
判定规则和固定错误文案；每个命令给出步骤顺序、每步失败返回什么、怎么人工验证、
怎么算通过；验收脚本 17 步逐步骤写明做法和门槛。
v1 只写到函数名和行号，v2 把每个函数内部的判定逻辑全部写明。

实现文件与行数基准（提交 9eeca82）：
- release/asgctl.py 1174 行 —— 7 个子命令与全部支撑函数
- release/service_main.py 223 行 —— 服务监督器（心跳的生产者）
- release/build_release.py 244 行 —— 交付包打包器
- e2e/verify_terminal_lifecycle.py 728 行 —— 17 步自动验收

定位任意函数：grep -n 'def 函数名' release/asgctl.py

---

## 0. 五个名词 + 统一返回码 + 三条铁律

| 名词 | 是什么 | 位置 |
|---|---|---|
| 交付包 | 解压即用的目录：程序 + 自带 Python 运行时 + 清单 release.json | 当前这份 /tmp/asg-rel-20260921/asg-terminal |
| home | 安装后的家目录：程序副本、配置、数据、日志全在这 | macOS 默认 ~/Library/Application Support/ASG；验收用 --home 指临时目录 |
| current | home 里的软链接，指向 releases/ 下正在运行的版本；切软链=切版本 | home/current |
| 心跳文件 | 监督器每 5 秒写一次的"我确实在干活"证据。全系统健康判断只认它，**不认服务管理器的 running** | home/state/operations/heartbeat.json |
| 收据 | 往 Agent 写 Hook 时的记录：写了哪些文件、每个文件写完后的 sha256。卸载只按收据回滚，没收据的一律不动 | 每个安装实例状态目录里的 package-receipt.json |

统一返回码（所有子命令一致；验收脚本只读 rc 和 --json 字段，不解析中文）：

| rc | 含义 | 谁返回 |
|---|---|---|
| 0 | 成功 | 全部 |
| 1 | 执行失败，或升级失败已回滚 | install / start / stop / upgrade；uninstall 仅在进程没退出时 |
| 2 | 包或配置类错误：缺文件、摘要不符、平台不符、该用 upgrade 却跑了 install、凭据不可用 | install / upgrade |
| 3 | 服务注册着但不健康 | status / doctor |
| 4 | 未安装 | status |
| 5 | 服务正常但 SOC 鉴权被拒 | install |

三条铁律（重写时必须保留）：
1. asgctl.py 只用 Python 标准库——它要在"还没装任何东西"的机器上先跑起来。
2. 一切机器可读判定来自结构化文件（release.json / installation.json /
   heartbeat.json / 收据 JSON），永远不解析人读中文。
3. --json 输出恰好一行；字段名英文且稳定，供脚本断言。

---

## 1. 文件契约

### 1.1 交付包结构（build_release.py 产出；目录名可换，职责不可少）

    asg-terminal/
    |-- asgctl          三行 sh 包装（§9 第 6 步），用包内 python 跑 release/asgctl.py
    |-- release.json    清单（字段规格见 1.2）
    |-- app/            ASG 程序：monitor_dashboard.py, asg_os_sensor.py,
    |                   service_main.py, runtime/, integrations/, web/, recipes/,
    |                   policies.yaml, identities.yaml, llm.yaml,
    |                   bin/goose(可选), bin/sabotage.json(仅坏版本)
    |-- python/         自带 Python 3.11 独立运行时 + 全部第三方依赖（用户零安装）
    |-- release/        asgctl.py 命令实现本体
    |-- services/       com.asg.terminal.plist.template + asg-terminal.service.template
    +-- README.md       装/查/升/卸四段人读说明 + 数据目录 + 凭据警告

### 1.2 release.json 字段规格（verify_package 的输入契约）

七个必填键，缺任何一个报 "release.json 缺少字段: 键名"（rc2）：

    {
      "asg_version": "0.9.2",        // 字符串；install/upgrade 用它命名 releases/ 子目录
      "git_commit": "6274392b...",   // 构建时 git rev-parse HEAD
      "platform": "Darwin",          // 与 platform.system() 全等比对，不等 rc2
      "architecture": "arm64",       // 与 platform.machine() 全等比对
      "config_version": 1,           // 大于 asgctl 常量 CONFIG_VERSION=1 则 rc2 拒绝
      "db_schema_version": 1,        // 大于 DB_SCHEMA_VERSION=1 则 rc2 拒绝
      "files": {                     // 除 release.json 外每个文件/软链一条
        "app/service_main.py": {"sha256": "..."},
        "asgctl": {"sha256": "...", "executable": true},
        "python/bin/python3": {"sha256": "", "link": "../Resources/bin/python3"}
      },
      "built_at": "2026-09-21T04:02:53+0800"   // 仅记录，不校验
    }

files 条目规则：普通文件必须含 sha256（构建时逐文件计算）；有可执行位的加
executable: true（复制时 chmod 或 0o755 还原）；符号链接用 link 字段记录
readlink 目标、sha256 留空。release.json 自己不列进 files（否则自我指涉算不出
摘要），由 copy_release 显式补拷，保证 current/release.json 永远存在——doctor、
status、监督器版本探测全靠它。

### 1.3 home 布局与每个目录的规矩

    home/
    |-- releases/<版本>/            程序副本；升级=新增目录，绝不原地覆盖运行中版本
    |-- current -> releases/<版本>   原子切换点（临时软链 + os.replace）
    |-- config/terminal.json        引擎侧配置（0600），键见 §3 第 8 步
    |-- config/endpoint.json        上报端配置（0600），键见 §3 第 8 步
    |-- config/credentials.json     SOC Bearer 凭据裸文本（O_CREAT 0600）
    |-- state/operations/heartbeat.json      健康唯一依据（监督器每 5 秒重写）
    |-- state/endpoint/outbox.sqlite         待发送队列：断网入队、恢复补报、升级不动
    |-- state/installation.json     安装记录（0644）：版本/目录/服务标签/清单摘要/
    |                               config 摘要/soc_url/port/home/时间；不含凭据正文
    |-- state/backups/pre-upgrade-<时间戳>/   升级前 3 配置 + sqlite 在线备份
    +-- logs/  service.log / supervisor.log / engine.log / endpoint.log（8MB 轮转）
    home 之外: ~/Library/Application Support/asg-hooks —— Hook 运行目录，
              与 home 平级，卸载扫描器永远碰不到

四条规矩与代码保证（重写时逐条对应）：
1. "半新半旧版本"结构上不可能 —— copy_release 先写 releases/版本.staging-随机，
   逐文件重算 sha256 全对后一次 os.replace 改名转正；中途失败只留 staging 垃圾。
2. 配置升级不覆盖用户键 —— write_configs 旧 dict 打底、只覆盖 asgctl 托管键；
   endpoint.json 只刷新连接字段，agents/discovery/soc_installation/
   upload_skill_content 一律沿用旧值（安装时关掉的开关，升级绝不重开）。
3. state 永不清空 —— 升级只换软链；数据库 sqlite 在线备份到 state/backups，
   备份失败时 current 还没动过，直接 rc1 退出。
4. Hook 独立性 —— hook_run_dir 固定放 home 之外（mac 见上；linux 是
   XDG_DATA_HOME 或 ~/.local/share 下 asg-hooks）。

### 1.4 heartbeat.json（健康判断的全部输入）

    {
      "t": 1789.0,                  // 写入时刻；年龄>25 秒 = 心跳停
      "pid": 12345,                 // 监督器 pid；doctor 与服务管理器 pid 互核
      "service_version": "0.9.2",   // 从 current/release.json 现读；与期望不符=跑错版本
      "children": { "engine":   {"pid":1,"alive":true,"exit_code":null},
                    "endpoint": {"pid":2,"alive":true,"exit_code":null} },
      "engine":   { "reachable": true, "scan_stalled": false, "scan_count": 41,
                    "scan_interval": 60, "last_scan_time": ...,
                    "active_investigations": 0
                  },                  // 不可达时另有 "error": "engine /api/state unreachable"
      "endpoint": { "loop_age_s": 12, "bridge_age_s": 30,
                    "soc": {"state": "ok|rejected|error|unknown", "checked_at": 1789.0},
                    "queue": {"pending_requests": 0, "drafts": 0} },
      "soc":    // 与 endpoint.soc 相同（冗余一份方便人读）
    }

生产者只有 service_main.py（§8）；消费者只有 evaluate_health 一个函数——
status、doctor、升级健康检查、start 等待四处共用。全系统"健康"只有一个口径。


---

## 2. 支撑函数完整规格（asgctl.py，逐个照抄即可复现）

### 2.1 default_home()
darwin 返回 ~/Library/Application Support/ASG；linux 返回 XDG_DATA_HOME
（未设置则 ~/.local/share）下的 asg；其他平台直接退出 "unsupported platform: 平台名"。

### 2.2 default_label()
darwin 返回 "com.asg.terminal"，其他返回 "asg-terminal"。launchd 域恒为 gui/<uid>。

### 2.3 load_json(path) / write_json(path, value, mode)
load_json：文件缺失或 JSON 损坏一律返回 None，绝不抛异常——调用方都以
"None 当未配置"处理。write_json：先写同目录临时名 name.tmp-随机6位，
chmod 指定权限，再 os.replace 原子替换。所有状态写入必须走它，保证任何时刻
读到的都是完整旧文件或完整新文件，不存在半截 JSON。

### 2.4 sha256_file(path)
1MB 分块读取，返回十六进制摘要。用途：包校验、复制后校验、doctor 完整性、
收据漂移检测。

### 2.5 emit(args, payload, human, error)
args.json 为真只打印 payload 的一行 JSON；否则打印 human（error=True 走 stderr）。
JSON 模式绝不掺人读文本。

### 2.6 verify_package(pkg) → manifest 或 raise ValueError(首个具体原因)
判定顺序（每步失败文案固定）：
1) release.json 读不出 dict → "安装包缺少 release.json"
2) 七个必填键缺哪个报哪个 → "release.json 缺少字段: 键名"
3) platform/architecture 与本机不全等 → "安装包平台不匹配: 包为 X/Y，本机为 A/B"
4) config_version 或 db_schema_version 超出常量 → 对应"超出本 asgctl 支持"
5) **按文件名排序**逐文件（排序保证报错确定性）：
   link 条目：必须 is_symlink 且 readlink 与记录相等，否则"符号链接缺失或被修改: 名"
   普通条目：先查形态（是软链或不是文件 → "文件缺失或形态改变: 名"），
   再算摘要（不符 → "文件摘要不一致: 名"）
返回 manifest 供调用方取 asg_version。

### 2.7 copy_release(pkg, destination) → manifest
1) purge_staging：删 destination 父目录下遗留的 *.staging-* 垃圾；
2) 目标目录已存在 → 先 os.replace 挪到 .displaced-随机（rename 不能撞非空目录）；
3) 建 staging，按 manifest 逐条目复制：link 条目做 os.symlink；普通条目 copy2，
   清单标了 executable 的 chmod 或 0o755；release.json 显式补拷；
4) 复制后逐文件重算 sha256，任何不符 raise "复制后摘要校验失败: 名"；
5) 成功 → os.replace(staging, destination) 一步转正；
6) 捕获 BaseException（含 Ctrl-C）：删 staging；若 displaced 存在且目标没生成，
   把 displaced 挪回原位（旧版本目录原地复活），再重抛；
7) 成功且 displaced 存在 → 删掉旧目录。

### 2.8 服务注册三件套
register_service(home, label)：程序参数固定为
[home/current/python/bin/python3, home/current/app/service_main.py, --home, home]。
- mac：plistlib 写 ~/Library/LaunchAgents/label.plist，键固定 Label、
  ProgramArguments、WorkingDirectory(=home/current/app)、RunAtLoad、KeepAlive、
  ThrottleInterval=10、StandardOutPath 与 StandardErrorPath(=home/logs/service.log)；
  先 launchctl bootout 同名（幂等清残留），再 launchctl bootstrap；
  bootstrap 非 0 → raise "launchctl bootstrap 失败: stderr"。
- linux：写 ~/.config/systemd/user/label.service（Type=simple、Restart=always、
  RestartSec=10、WantedBy=default.target，ExecStart 逐项双引号转义），
  systemctl --user daemon-reload 后 enable --now；任一步非 0 → run_check 统一
  raise "命令失败 全串: stderr 前300字"。

unregister_service(label)：mac bootout + 删 plist；linux disable --now + 删 unit +
daemon-reload。**先删注册再删文件**，保证不存在被服务管理器自动复活的路径。

service_loaded(label) → (loaded, pid 或 None)：
- mac：launchctl print gui/<uid>/label；rc==0 才算 loaded，pid 从输出
  "pid =" 行解析；
- linux：systemctl --user show -p ActiveState -p MainPID；
  ActiveState==active 才算 loaded，pid 取 MainPID。

run_check(command)：非 0 → raise "命令失败 全串: stderr 前300字"；成功返回结果对象。

### 2.9 evaluate_health(home, expect_version, max_age=25) → {healthy, reasons, heartbeat}
按序收集**全部**不满足项（不短路，理由一次列全）：
1) 无心跳文件 → 直接 {healthy:false, reasons:["没有心跳文件（服务从未启动）"]}；
2) now 减 t 大于 max_age → "心跳已停止推进 N 秒"；
3) children.engine.alive / children.endpoint.alive 任一非真 → "子进程 X 未运行"；
4) engine.reachable 非真 → "发现/调查引擎无响应: error"；
   reachable 但 scan_stalled → "发现循环停滞（超过 3 个扫描周期没有新的扫描）"；
5) endpoint.loop_age_s 为 None → "上报服务尚未写入循环心跳"；大于 120 → "上报服务循环 N 秒未推进"；
6) bridge_age_s 存在且大于 180 → "桥接循环 N 秒未推进"；
7) 给了 expect_version 且 service_version 不符 → "运行版本 X 与预期 Y 不一致"。
healthy = reasons 为空。**阈值就是规格**：25 秒（5 秒心跳 × 容忍丢 5 拍）、
上报循环 120 秒、桥接循环 180 秒。重写必须一致，否则验收会抖。

wait_healthy(home, expect_version, timeout=60)：每 2 秒轮询，健康立即返回，
超时返回最后一次结果——调用方负责把 reasons 报给用户。

### 2.10 soc_probe(home) → {network, auth, detail}
**两次独立请求、两个独立结论**，"网关 200 就宣布鉴权正常"这类误判在代码路径上不存在：
1) GET soc_url + /api/health，**不带凭据**，5 秒超时，显式空代理（不走系统代理）。
   status 小于 500 → network=ok；连上了但 HTTP 错误 → reachable_error 带 "HTTP 码"；
   DNS/拒绝/超时 → unreachable 带异常类名，**直接返回**（auth 保持 unknown）。
2) 读 config/credentials.json，缺失 → detail="凭据文件缺失" 返回；
3) GET soc_url + /api/asg/self 带 Authorization: Bearer 内容。小于 400 → auth=ok；
   401/403 → rejected（这是"凭据被拒"的唯一判据）；其余 HTTP → error；异常 → error。
用法约定：network 证明"路通"，auth 才证明"钥匙对"。doctor 第 6/7 项、
install 第 12 步、status 的 SOC 行全用这同一个函数。

### 2.11 require_installed(home)
config/terminal.json 不是文件 或 current 不是软链 → 退出
"未找到已安装的 ASG（路径）。请先执行 asgctl install。"；否则返回 terminal dict。
start/stop/upgrade 的第一道门（doctor 不用它，未安装时 doctor 逐项如实报）。

### 2.12 hook_run_dir(home)
mac 固定 ~/Library/Application Support/asg-hooks；linux 固定 XDG 目录下 asg-hooks。
代码注释写明设计意图：卸载扫描器永远删不到 Agent Hook 依赖的文件。

### 2.13 write_configs(home, args, version, label)
写两个文件（均 0600、走 write_json 原子写）：
- terminal.json 托管键：soc_url（去尾斜杠）、port、asg_version、service_label、
  config_version、db_schema_version、scan_interval、collection_mode、collect_interval；
  engine_env 单独合并（--engine-env KEY=VALUE 可重复，无等号 → raise
  "--engine-env 需要 KEY=VALUE 形式: 原串"；同名键覆盖旧值，旧的其他键保留）。
  若已有旧 terminal.json：旧 dict 打底，托管键覆盖，**用户自加的未知键原样保留**。
- endpoint.json：连接托管键 backend_url、state_dir、interval_seconds、
  asg_url(=http://127.0.0.1:port)、runtime_bridge=true、collection_mode；
  旧文件存在时只刷这些，agents/discovery/soc_installation/upload_skill_content
  照抄旧值；旧文件不存在（首装）才用命令行开关初始化：
  upload_skill_content = 非 --skip-skill-upload；
  soc_installation = 非 --skip-soc-installation（验收实例用开关避免往真实 Agent 写 Hook）；
  discovery = 除非 --no-discovery，写入 application_key_file 指向凭据文件
  （生命周期发现拿它向 SOC 注册真实本机 Agent）。

### 2.14 save_credential(home, credential_file)
读文件去空白，空 → raise "凭据文件为空: 路径"。用 os.open 带
O_WRONLY|O_CREAT|O_TRUNC 和 0o600 写 config/credentials.json（权限在建文件时
就定死，不存在先 0644 再改的空窗）。

### 2.15 self_check(release_dir) → None 或错误串
1) release_dir/python/bin/python3 不是文件 → "捆绑解释器缺失: 路径"；
2) 用它跑一条 60 秒超时的 import 探针：sys.path 插入 release_dir/app 后
   import integrations.soc_inventory.endpoint、integrations.soc_inventory.discovery、
   runtime.matcher、runtime.hook_data、runtime.learned_install 和 psutil，
   打印 self-check ok。非 0 → "依赖或模块加载失败: stderr 尾400字"；
3) sqlite3 真建一个探针库、建表、插一行、提交、删文件；异常 →
   "本地 SQLite 不可用: 异常"。返回 None 才算过。
注意：探针用的是**包内解释器**，证明的是"这份包在自己身上自洽"，
与开发机环境完全无关。

### 2.16 hook_install_records(home) → 记录列表
只信 ASG 自己的账本，绝不整盘扫描：
1) 只读模式打开 state/endpoint/outbox.sqlite；读 enrolled 表 configuration 列
   （每行一个 Agent JSON）；有 soc_onboarding 表就读 instance→result 映射；
2) 每个 enrolled Agent：onboarding 记录 status 不在 (installed, already_installed)
   → 跳过；transport 不是 soc-direct-v1 → 记 kind=native（原生 Hook，不核对文件）；
3) learned 记录：workspace 缺失跳过；状态目录 = workspace 父目录下
   .asg-install- 前缀 + workspace 绝对路径 sha256 前 16 位十六进制；
   返回 {instance, kind:learned, workspace, state_dir, status}。
数据库缺失/打不开 → 空列表（doctor 相应报"本机没有 ASG 登记的 Hook 安装记录"）。


---

## 3. install：12 步固定顺序（cmd_install）

入口命令：

    ./asgctl install --soc-url http://SOC地址 --credential-file 凭据文件 \
        [--home 目录] [--json] [--package 包目录] [--port 8081]
        [--scan-interval 60] [--collect-interval 600]
        [--collection-mode manual|auto] [--service-label 名]
        [--engine-env KEY=VALUE]... [--skip-soc-installation]
        [--skip-skill-upload] [--no-discovery]

包目录默认 = asgctl.py 所在目录的上一级（即交付包根），--package 可显式指定。

顺序与每步失败行为（失败时"已造成什么状态"是验收重点）：

| 步 | 做什么 | 失败时 | 失败时机器状态 |
|---|---|---|---|
| 1 | 认包：pkg/release.json 必须是文件 | rc2 "目录 X 不是交付包（缺少 release.json）" | 什么都没发生 |
| 2 | 验包：verify_package 全量 | rc2 "安装包校验失败: 原因（未注册任何服务）" | 什么都没发生 |
| 3 | 先存凭据（save_credential）——**放在幂等检查之前**：改好凭据重跑 install 必须能修复连接（endpoint 每次请求重读该文件，连重启都不需要） | rc2 "凭据不可用: 原因（未注册任何服务）" | 只写了凭据文件 |
| 4 | 幂等短路：installation.json 存在且 current 是软链时——同版本且 soc_url/port 相同且服务 loaded 且 evaluate_health 通过 → 再跑一次 soc_probe：auth=rejected 则 rc5，否则 rc0 already_installed。同版本但有残留（不健康/配置不同/服务没了）→ 不短路，继续往下**修复**（重复制、重注册）。不同版本 → rc2 "已安装版本 X；安装新版本请使用 asgctl upgrade --package" | — | 未改动 |
| 5 | 建目录：releases、config、state/operations、logs、hook_run_dir（顺带证明可写） | rc1 | 只有空目录 |
| 6 | 复制程序：copy_release 到 releases/版本 | rc1 "复制程序文件失败: 原因（未注册任何服务）" | 无服务、current 未切 |
| 7 | 本地自检 self_check | rc1 "本地自检失败: 原因（未注册任何服务）" | 同上 |
| 8 | 写配置 write_configs | — | — |
| 9 | 切 current：软链到 releases/版本，经 .current-link-随机 临时名 + os.replace 原子换 | — | — |
| 10 | 注册服务 register_service；失败且之前有旧 current → 软链恢复旧目标再退出 | rc1 "服务注册失败，未启动任何服务: 原因" | 无进程 |
| 11 | 等健康 wait_healthy 最多 60 秒；**先写 installation.json 再报错**（失败也要有记录） | rc1 "服务已注册但 60 秒内未通过健康检查: 原因（诊断: home/logs）"，status=installed_unhealthy | 服务注册着但不健康，日志保留 |
| 12 | 探 SOC 三分支（下表） | — | — |

installation.json 内容（0644）：version、release_dir、service_label、installed_at、
updated_at、release_manifest_sha256（包清单的 sha256，报告绑包靠它）、
config_digest（写完后 terminal.json 的 sha256）、soc_url、port、home。

第 12 步三分支（soc_probe 结果驱动，消息口径固定）：

| soc 结果 | rc | 人读消息口径 |
|---|---|---|
| auth=ok | 0 | "安装成功，已连接 SOC。版本 X。" |
| network 不可达 | 0 | "安装成功；SOC 当前不可达（原因），发现与清点照常运行，数据暂存本机，恢复联网后自动补报。" |
| auth=rejected | 5 | "服务已安装且运行正常；SOC 鉴权失败（凭据被拒绝）。请更正凭据文件后重启服务即可，无需重装。" |

失败清理硬规则（重写必须保持）：
- 第 10 步之前失败 → 无已注册服务、current 未切换；
- 注册后启动失败 → 保留日志、报 installed_unhealthy、rc1，结构上不返回成功；
- SOC 离线绝不删本地缓存。

install 怎么算好（每条一个可复制验证，期望值写死）：

| 测试 | 命令要点 | 通过 = | 已验证于 |
|---|---|---|---|
| 干净安装 60s 健康 | 干净 --home + 真网关 + 真凭据 | rc0 且 JSON status=installed、healthy=true | e2e 步骤1，实测 10.7s |
| 删解压目录仍正常 | 装完把包目录改名，再 status --json | rc0 且心跳年龄持续小于 25 | e2e 步骤2，12.0s |
| 连装 3 次 | 同参数跑 3 遍 | 后两次 rc0 already_installed；服务管理器只有一个 pid | e2e 步骤3，1.2s |
| 缺文件的包 | 手删 app/ 里任一文件再 install | rc2 且消息点名该文件；服务查无 | e2e 步骤10，13.3s |
| 被篡改的包 | 改任一文件 1 字节再 install | rc2 "文件摘要不一致: 文件名" | e2e 步骤10 |
| SOC 断网 | --soc-url 指 192.0.2.1（TEST-NET 不可路由） | rc0 且消息含"暂存本机" | e2e 步骤10 |
| 凭据错误 | 给错误 key 文件 | rc5，含"鉴权失败"，**不出现"已连接"字样** | e2e 步骤9，12.3s |
| 恢复免重装 | 换正确凭据再跑 install | rc0 且 JSON soc.auth=ok | e2e 步骤9 |

真实踩坑记录：测"网络故障"必须用不可路由地址。用本机 closed 端口测出来的是
"连接被拒"，会被正确归类为服务故障而不是网络故障——口径混淆造成假阴。

---

## 4. start / stop / status

### start（cmd_start）
前置 require_installed。顺序：service_loaded 且 evaluate_health 已健康 →
直接转 report_status（note="正在运行"，幂等）；否则 register_service 拉起 →
wait_healthy 60 秒 → 不健康 rc1 "60 秒内未恢复健康: 原因"，健康转 status 输出。
"清理过期 PID 文件"为什么不用写：健康只认心跳里的活 pid 和服务管理器现查 pid，
死 pid 记录天然无效，不存在人肉清 PID 文件的路径。

### stop（cmd_stop）
前置 require_installed。loaded 为假 → rc0 "服务本就未运行"。
unregister_service（mac bootout + 删 plist；先删注册，KeepAlive 随之移除，
结构上不存在被自动拉起的路径）→ 每秒轮询 service_loaded 最多 30 秒 →
仍 loaded → rc1 "30 秒后服务进程仍未退出（pid=N）"，绝不假装停了。
成功消息："服务已停止，不会被自动拉起；待发送记录保留在本机。"
边界：stop 不碰 Agent、不碰 Hook、不碰 SOC 策略。队列在 SQLite、进度在
state 文件里，进程死了状态就在，不需要优雅停机魔法。

### status（cmd_status → report_status）
未安装（无 terminal.json 或无 current 软链）→ JSON 模式打印
{"command":"status","installed":false}、人读"未安装。"、rc4。
已安装输出（人读逐行）：服务运行与否 / 版本 / 健康（异常带全部原因）/
SOC 连接（正常|鉴权失败|请求失败|尚未上报 + detail）/ 最近上报检查（N 秒前）/
待发送条数 / 发现循环（正常与否 + scan_count + interval）/ 上报循环心跳年龄 /
深度清点模式 / 数据目录。--json 输出同结构机器字段。
返回码：健康 0；注册着但不健康 3（服务管理器显示 running 但心跳停了**必须**是 3）。

怎么算好：连发 10 次 start 进程数仍为 1；stop 后 30 秒不被拉起；
start/stop/start 连续 5 轮成功；SIGSTOP 冻结 engine 子进程（进程活着、循环死了）
时 status 必须 rc3。对应 e2e 步骤 8（59.0s）与 11，全部通过。

---

## 5. doctor：十项检查（cmd_doctor），每项一句可行动结论

入口：asgctl doctor [--json]。--json 单行 {command, checks:[{name,ok,detail}], healthy}；
人读每项一行 "名字 OK/FAIL 细节"。rc = 全 OK ? 0 : 3。
构造器 check(name, ok, detail) 逐项追加，**全程只读，无任何写配置路径**
（数据库探针 rollback 且不提交）。

| # | 检查项 | 怎么查（代码事实） | FAIL 时告诉用户什么 |
|---|---|---|---|
| 1 | 程序完整性 | 读 current/release.json，按清单逐文件：link 条目比 readlink；普通文件缺失或摘要漂移点名 | "全部 N 个文件与清单一致" 或 前6个问题文件名（缺失/摘要不一致/链接） |
| 2 | 配置 | terminal.json 存在且 soc_url 非空且 port 是整数；credentials.json、endpoint.json 是文件 | 哪个字段/哪个文件缺失 |
| 3 | 本地数据库 | state/operations 下真开 sqlite 连接、建表、INSERT、**rollback**、删探针文件 | "state 目录 SQLite 可开事务读写" 或 异常原文 |
| 4 | 服务身份 | service_loaded 的 pid 与心跳 pid **互核**：无 loaded 或无 pid → 未注册；两 pid 都有但不等 → 跑错版本 | "pid=N 与心跳进程一致" / "心跳进程 X 与服务管理器 pid Y 不一致" |
| 5 | 工作循环 | 顺序判：心跳年龄>25s → 心跳停；engine 不可达；scan_stalled；loop_age_s 为 None 或>120 | 精确到哪个循环停止推进 |
| 6 | SOC 网络 | soc_probe 的 network（不带凭据那次请求） | "网关可达" 或 "网络失败（DNS/连接/超时）: detail" |
| 7 | SOC 鉴权 | soc_probe 的 auth（带 Bearer 那次请求）；网络不通时**不猜**，直接 FAIL | 网络不通 → "网络不可达，无法验证鉴权"；rejected → "凭据失效或权限不足: detail"；其余 → "未能验证: detail" |
| 8 | 上报队列 | 心跳 endpoint.queue.pending_requests 是整数就 OK | 断连积压是**正常**，不判故障；非整数（队列读不了）才 FAIL |
| 9 | Hook 安装 | hook_install_records 逐 learned 项：收据存在 → 事务 JSON（plan_digest 命名）status=installed → 逐 change 现算 sha256 比 after_sha256 | 点名"实例：收据缺失 / 安装事务记录异常 / 文件与收据不一致 路径" |
| 10 | Hook 活动 | 有 activation-receipt.json 才算见过真实回调；**永远 OK**，只报统计 | "N/M 项已见真实回调；其余等待目标新活动（无事件不代表 Hook 损坏）" |

四条"不能做的事"逐条有代码保证：
无事件不等于 Hook 坏（第 10 项措辞写死且永不 FAIL）；installed 不等于输入输出
阻断全通（第 9/10 项分开）；健康接口 200 不等于鉴权正常（第 6/7 是两次请求）；
doctor 全程只读。

怎么算好：主动制造四类故障，各自给出**各自的**原因，不许全糊成"服务异常"。
e2e 步骤 11（23.5s）实测：换坏凭据 → 只有第 7 项 FAIL；soc_url 指 TEST-NET →
第 6 项 FAIL 且第 7 项注明"网络不可达无法验证"；删 app/identities.yaml →
第 1 项点名该文件；SIGSTOP 冻结 engine → 第 5 项 FAIL 且 status 同时 rc3；
每类恢复后 doctor 转全 OK。


---

## 6. upgrade：新版本先起、健康才转正；失败走显式回滚分支

入口：asgctl upgrade --package 新包 [--force] [--soc-url ...] [--port ...]
（未显式给的连接参数从旧 terminal.json 继承，见第 7 步）。实现 cmd_upgrade。

顺序十步，每步标注"失败时 current 动没动"（这是本命令的全部难点）：

| 步 | 做什么 | current | 失败返回 |
|---|---|---|---|
| 1 | require_installed；verify_package 新包 | 未动 | rc2 "新包校验失败: 原因（当前安装保持不变）" |
| 2 | 版本相同且无 --force → rc0 status=same_version "当前已是版本 X；如需重装同一版本请加 --force" | 未动 | — |
| 3 | copy_release 到 releases/新版本 | 未动 | rc1 "复制新版本失败（当前安装保持不变）" |
| 4 | 新版本离线自检 self_check；失败**删掉新版本目录** | 未动 | rc1 "新版本离线自检失败（当前安装保持不变）" |
| 5 | 备份：terminal/endpoint/credentials 三配置 copy2 + outbox.sqlite 用 sqlite 在线 backup API 拷到 state/backups/pre-upgrade-时间戳/。备份失败 → 直接退出 | **未动** | rc1 "数据库备份失败（当前安装保持不变）" |
| 6 | unregister_service 旧服务 | 未动 | — |
| 7 | 连接参数继承（scan_interval/collection_mode/collect_interval/soc_url/port 从旧配置补齐）→ write_configs 新版本号（用户键保留、关掉的开关不被重开） | 未动 | — |
| 8 | 临时软链 + os.replace 切 current 到新版本 | **已动** | — |
| 9 | register_service；失败 → 软链切回旧目标、重新注册旧服务 | 已复原 | rc1 failed_rolled_back "升级失败，已恢复旧版本 X 并重启服务" |
| 10 | wait_healthy(新版本, 60 秒) | 已动 | 见下方回滚分支 |

**回滚是代码里的显式分支，不是口头承诺**（第 10 步不健康时依次执行）：
1. unregister_service；
2. 从备份恢复三个配置文件（恢复后重设 0600）；
3. 备份库 sqlite backup **反向**写回 outbox.sqlite（失败尽力，不阻断回滚）；
4. 软链切回旧目标；
5. register_service 旧版；
6. wait_healthy(旧版, **90 秒**——给旧版更宽的恢复预算)；
7. installation.json 记 upgraded_failed_at 时间戳；
8. 返回 status=failed_rolled_back、rc1，消息
   "升级失败，已恢复旧版本 X（备份: 路径）"，JSON 带 reasons/new_version/restored/
   backup/old_healthy。**返回值里没有任何字段可能被读成"升级成功"。**

成功路径收尾：installation.json 更新 version/release_dir/updated_at/upgraded_from/
backup/release_manifest_sha256；releases/ 清理只保留 {旧,新} 两个目录；
成功消息明说"Agent Hook 不会被自动重写"——cmd_upgrade 全流程零调用 Agent
配置写入，这是规格"升级 ASG 不自动重写 Agent Hook"的实现。

upgrade 怎么算好（e2e 步骤 12-14 实测值）：
- 正常升级（A→B）后：用户自加的自定义配置键仍在、清点流水线配置保留，12.4s；
- sabotage 坏版本（起不来）升级：失败后旧版本 **71.1 秒**恢复健康（门槛 90 秒）、
  升级前特意注入的队列行一条不少、随后可直接 upgrade 到好版本成功——整步 143.9s；
- 复制期间中断（Ctrl-C/断电）：只留 staging 垃圾目录（§2.7 第 6 步的结构保证）。

---

## 7. uninstall：删哪些、留哪些，白纸黑字

入口：asgctl uninstall [--remove-hooks]。实现 cmd_uninstall + remove_hooks。

未安装判定：无 installation.json 且无 current 软链且 releases 下没有任何
app/service_main.py → rc0 status=not_installed "已卸载（没有安装记录）"
（幂等，第二次卸载必须走到这条）。

默认卸载**删**（按序）：
1. unregister_service + 30 秒内轮询确认进程消失；
2. --remove-hooks 时先做 Hook 回滚（见下）；
3. installation.json 里记录的 release_dir 整目录；
4. releases/ 下其余**符合 is_release_layout 判定**的目录——判定标准=目录内同时有
   app/service_main.py 和 release.json；用户自己塞进 releases 的东西不动；
   再清 staging 垃圾；
5. current 软链 + 遗留 .current-link-* 临时软链；
6. installation.json 删除，替换为 state/uninstalled.json（uninstalled_at + 版本），
   让第二次运行如实报 not_installed；
7. 收尾再查 service_loaded，进程还在 → rc1（唯一 rc1 路径）。

默认卸载**留**：config、state（数据库/队列全在）、logs、home 之外整个 Hook 运行
目录、Agent 里已装的 Hook 文件。输出 JSON 的 kept 字段明说每个保留位置。

--remove-hooks 的精确范围（"卸载不破坏用户修改"的核心）：
逐条遍历 hook_install_records：
1. kind=native（Codex 这类由目标自身信任机制管理的原生 Hook）→ **不动文件**，
   notes 记 "原生 Hook 由目标自身的信任机制管理，请在 Agent 内确认卸载"；
2. learned 项收据缺失 → conflicts 记 "收据缺失，保留现状"；
3. learned 项有收据 → 用包内解释器调 runtime.learned_install.rollback，
   传 workspace、state_dir、approved_workspace、approved_digest=收据 plan_digest。
   该函数既有契约：对"安装后被用户改过"的文件**拒绝覆盖、报冲突、保留原文**，
   绝不整份还原旧备份——卸载只是复用这条安全语义，不自己发明；
4. 返回 rolled_back/already_rolled_back → removed；否则 conflicts 记 stderr 尾行；
5. 全部处理完才继续默认卸载；冲突只报告，不阻断卸载。

uninstall 怎么算好（e2e 步骤 15-17 实测）：
- 默认卸载：服务查无、程序目录消失，config/state/logs/hook 四处保留（1.2s）；
- 种两项 learned Hook（一项原样、一项装后故意被"用户"改过）再 --remove-hooks：
  原样项 removed、修改项进 hook_conflicts 且原文保留、doctor 只点名修改项漂移
  不误报原样项、工作区无关文件与用户配置键两轮卸载后原样、
  第二次卸载 rc0 not_installed（16+17 共 12.9s）。


---

## 8. 心跳的生产者：service_main.py 监督器（规格）

launchd/systemd 只证明"这个进程注册着"；监督器负责证明"活真的在干"。
纯标准库（跑在包内解释器上）。启动顺序与规则：

1. 读 home/config/terminal.json；解析 current 软链得到 app 目录；
2. 发现 app/bin/sabotage.json 标记文件 → supervisor.log 记一行
   "refusing to start: release marked fail_start"，退出码 86。这是**故意**
   做"起不来的坏版本"用的验收钩子（配合 build_release --sabotage），不是彩蛋；
3. spawn 两个子进程，命令固定：
   - engine:  包内python -u -B app/monitor_dashboard.py
   - endpoint: 包内python -B -m integrations.soc_inventory.endpoint
               --config home/config/endpoint.json
   都 start_new_session（独立进程组）、stdout/stderr 追加进 logs/名字.log；
4. 子进程环境：先剥掉全部 ASG_ 前缀变量（**凭据不下传**），再注入
   PYTHONUTF8、PATH 前置包内 python bin、PYTHONPATH 前置 app；engine 额外注入
   ASG_HOST=127.0.0.1、ASG_PORT、ASG_RUN_DIR、ASG_FINGERPRINT_DB、
   ASG_SCAN_INTERVAL（配置为整数才注入）、ASG_GOOSE_BIN（app/bin/goose 存在才注入）；
   最后叠加 terminal.json 的 engine_env；
5. 每 5 秒（BEAT_INTERVAL_S）主循环：poll 子进程，死了记日志重新 spawn
   （下一拍重试即退避）；组装 §1.4 心跳写 state/operations/heartbeat.json；
6. engine_probe：GET http://127.0.0.1:port/api/state（4 秒超时、空代理）。
   取 scan_count/last_scan_time/scan_interval/active_investigations；
   scan_count 变化就重置计时基线；停滞判定 = 距上次变化超过 3×scan_interval+120 秒
   才算 stalled（**慢但在推进=健康**）；scan_count 拿不到（非 int）= stalled；
7. endpoint_probe：读 state/endpoint 下 loop-beat.json / bridge-beat.json 算年龄、
   soc-health.json 透传、只读打开 outbox.sqlite 数 queue 和 drafts 两张表；
   库不可读 → queue={error:"outbox unreadable"}（doctor 第 8 项据此 FAIL）；
8. 收到 SIGTERM/SIGINT：对每个子进程组 killpg 发 TERM，30 秒宽限后 kill；
   日志 supervisor stopped 后退出；
9. 日志轮转：任何日志超过 8MB 改名为 .1 重新开（保一份历史，无压缩）。

手工观察：安装后 watch -n5 直接看 heartbeat.json 的 t 每 5 秒前进、
children 两个 alive=true——这就是"服务活着"的唯一证据链起点。

---

## 9. 交付包怎么造：build_release.py（规格）

入口：python3 release/build_release.py --version 0.9.3 --out 干净目录
     [--wheels-dir 缓存] [--sabotage]（坏版本，仅回滚验收用）

产出步骤：
1. 输出目录已存在 → 直接失败（验收要求每次构建用干净目录）；
2. git rev-parse HEAD 记进清单；
3. copy_app：按固定清单复制 monitor_dashboard.py、asg_os_sensor.py、policies.yaml、
   identities.yaml、llm.yaml、web/、recipes/、runtime/、integrations/ 进 app/，
   忽略 __pycache__、*.pyc、test_*、*.bak-*、*.lock；仓库缺任何一项 → 构建失败；
   goose 可选：ASG_GOOSE_BIN 或 /opt/homebrew/bin/goose 存在才解引用复制、0755；
4. bundle_python：从 python-build-standalone 下载固定版本 cpython-3.11.16
   install_only（URL 和 sha256 写死在源码里），下载到缓存前先比对既有缓存摘要，
   下载后再验摘要，不符 → "捆绑解释器下载摘要不一致，拒绝使用"；
   用包内 pip --target 装固定依赖清单：psutil、pyyaml、tomlkit、json5、
   python-dotenv、protobuf、opentelemetry-proto；
5. --sabotage 时写 app/bin/sabotage.json；
6. write_launcher：三行 sh 包装（#!/bin/sh; DIR=脚本所在目录;
   exec 包内python 包内release/asgctl.py 透传参数）——**不依赖用户 PATH 上任何东西**；
7. write_services：两份 plist/systemd 模板（占位 __HOME__，供文档人读与手工排障；
   运行时实际由 asgctl register_service 生成同构内容）；
8. manifest_files：先删全部 __pycache__（字节码是运行时产物，绝不能进清单，
   否则首次解释器运行就"摘要漂移"）；rglob 每个文件/软链按 §1.2 规则记条目；
9. 写 release.json（七必填 + built_at）；
10. 确定性自验：用**包内解释器**跑包内 asgctl install --help，非 0 →
    "交付包内 asgctl 无法运行"；成功打印 {package, version, git_commit, files,
    manifest_sha256, sabotage}——manifest_sha256 就是验收报告绑定的包摘要。

当前交付包事实：0.9.2、2332 个文件、manifest sha256 前缀 317732f5、
构建自 6274392、位置 /tmp/asg-rel-20260921/asg-terminal。

---

## 10. 自动验收：verify_terminal_lifecycle.py 的 17 步逐步骤说明

入口：python3 e2e/verify_terminal_lifecycle.py --package 交付包
      [--gateway http://127.0.0.1:8095] [--enrollment-key 应用key] [--keep]

写法铁律（都在现有代码里，重写必须保留）：
- 唯一临时 home（mkdtemp）+ 独立服务标签 com.asg.terminal.e2e-随机，
  **绝不碰开发实例**（8081 面板、com.asg.soc-collector 都不受影响）；
- 启动前先 pgrep 清理上次失败运行遗留的验收引擎进程
  （模式 asg-lifecycle-acceptance-.*/(monitor_dashboard|service_main|endpoint)），
  这是修过的真实坑：孤儿引擎占端口导致 doctor 期间引擎崩溃循环；
- 每步记录 {step, expected(中文预期), package_sha256, started_at, elapsed_s,
  pass, detail/error}；断言只读 --json 字段和 rc，**禁止解析中文提示判成败**；
- SIGSTOP/SIGCONT 注入要容忍监督器在两次读之间重启子进程（重读 pid 再冻，
  ProcessLookupError 在同预算内重试）；
- 失败保留临时目录和日志并打印 report.json 路径；全部成功才自动卸载清场
  （--keep 强制保留）。脚本退出码 = 失败步数。

17 步（名称 | 怎么做 | 通过门槛 | 本轮实测）：

| # | step | 预期（脚本原文） | 门槛与实测 |
|---|---|---|---|
| 1 | install_healthy_within_60s | 干净目录安装：60 秒内心跳健康，SOC 鉴权成功，rc0 | rc0、status=installed、healthy=true；10.7s |
| 2 | service_survives_package_removal | 删除/改名解压目录后服务心跳与子进程仍正常 | 改名包目录后 status rc0、心跳年龄<25；12.0s |
| 3 | repeat_install_idempotent | 重复安装 3 次：只有一个服务、一个运行实例，rc0 already_installed | 后两次 already_installed；pgrep 服务 pid 数=1；1.2s |
| 4 | register_test_agent | 隔离测试 Agent 经网关注册成功并写入采集配置 | 注册接口 rc 成功、endpoint.json agents 写入；0.2s |
| 5 | first_collection_reaches_soc | 首轮清点快照到达 SOC，队列清空 | SOC 可查到该 agent 快照、queue=0；5.3s |
| 6 | disconnect_queues_reconnect_drains | SOC 不可达时队列增长；恢复后自动补报清空且无需重装 | 断开期 pending>0，恢复后回 0 且 SOC 快照更新；9.5s |
| 7 | soc_snapshots_no_duplicates | SOC 快照数等于去重后的修订数（无逻辑重复） | 条数==distinct 修订数；0.0s |
| 8 | start_stop_cycles | 10 轮 start 只有一个进程；stop 后 30 秒不被服务管理器拉起 | 10 轮后 pid 数=1；stop 后 30s loaded=false；59.0s |
| 9 | bad_credential_rc5_and_repair | 错误凭据：install rc5 报鉴权失败；更正凭据重装修复，不出现已连接误报 | 错 key rc5 且 JSON soc.auth=rejected；换对 key rc0 auth=ok；12.3s |
| 10 | negative_installs | 缺文件 rc2 点名缺失；篡改摘要 rc2 拒绝；SOC 离线仍可装成并明示离线 | 三个负例包各自 rc2/点名/rc0+离线口径；13.3s |
| 11 | doctor_fault_injection | 四类故障各自给出正确原因：坏凭据/网络断/缺文件/工作循环停滞；恢复后 OK | 每类故障只有对应项 FAIL；恢复全 OK；23.5s |
| 12 | seed_user_config_key | 写入用户自定义配置键，供升级后核验保留 | terminal.json 加 acceptance_user_key；0.0s |
| 13 | upgrade_a_to_b | 升级后版本变新，用户配置键与流水线状态保留 | B 包健康、用户键在；12.4s |
| 14 | upgrade_failure_rolls_back_within_90s | 坏版本升级失败自动回滚：旧版本健康、队列记录不丢、可重复重试 | sabotage 失败后旧版 ≤90s 健康、预注入队列行在、再升好版成功；143.9s |
| 15 | uninstall_keeps_data_and_hooks | 默认卸载移除服务与程序但保留配置/状态/Hook 运行目录；二次运行 not_installed | 四目录存在、服务查无、二次 rc0 not_installed；1.2s |
| 16 | reinstall_seed_hooks | 重装同一发布包成功；种入两项学习 Hook 安装后，doctor 点名被用户修改的那项漂移、未修改项不误报 | doctor FAIL 细节只含被改项文件名；12.3s |
| 17 | uninstall_remove_hooks_scoped | --remove-hooks 只回滚未修改的 ASG 写入文件；被用户修改的保留并报冲突；无关文件与用户配置键不动；再次卸载 not_installed | removed 含原样项、conflicts 含修改项、用户键与 user-notes.md 原样；0.6s |

报告 report.json 顶层含 package、package_manifest_sha256、gateway、steps、
failed、home——每步都带 package_sha256，"报告绑定最终交付包摘要"由此满足。

回归测试单独跑（与验收互补）：python3 -m unittest 后**显式列** 13 个
integrations.soc_inventory.test_* 模块（共 58 个测试）。用 discover 会因相对
导入报错，别用。

最新报告：/var/folders/xf/_m1f6xjn7cd55zzpvqp3r3f80000gn/T/
asg-lifecycle-acceptance-8v1ix06j/report.json（17/17，全部步骤绑定 317732f5）。


---

## 11. 命令行入口规格（build_parser + main）

- 全局 --home 与 --json 用 argparse.SUPPRESS 默认值，**放子命令前后都行**
  （asgctl --json status 和 asgctl status --json 等价）——否则子解析器的默认值
  会覆盖主解析器已解析的值，这是真实踩过的坑。
- install 参数：--soc-url 必填、--credential-file 必填、--package（默认=asgctl
  所在包根）、--port 默认 8081、--scan-interval 默认 60、--collect-interval 默认
  600、--collection-mode manual/auto 默认 manual、--service-label、
  --engine-env KEY=VALUE 可重复、--skip-soc-installation、--skip-skill-upload、
  --no-discovery。后三个开关是给验收/隔离场景的，生产默认全不跳。
- upgrade 参数：--package 必填、--force，其余连接参数可选（不给就继承旧配置）。
- uninstall：--remove-hooks。
- main 兜底：任何未捕获异常 → 统一 emit 一行 {command,status:failed,error:
  异常类名: 消息} + 人读 "命令 X 失败: 消息"、rc1。**任何路径都不向用户甩
  Python traceback。**

## 12. 人工冒烟手册（15 分钟，照做即验证五个入口）

前提：macOS；有一份交付包（§9 现造或直接用 /tmp/asg-rel-20260921/asg-terminal）；
一个 SOC 网关地址和一份有效凭据文件（内容=一行 key）。

1. 打包（可选）：python3 release/build_release.py --version 0.9.3
   --out /tmp/asg-smoke  → 记下输出的 manifest_sha256；
2. 装：cd 包目录 && ./asgctl install --soc-url http://127.0.0.1:8095
   --credential-file ~/asg.key → 期望 60 秒内打印"安装成功"；
3. 看：./asgctl status → 健康：正常、SOC 连接：正常；
   再看 ~/Library/Application Support/ASG/state/operations/heartbeat.json
   的 t 字段每 5 秒前进（watch -n5 cat 即可）；
4. 断网口径：sudo 不必——直接把 terminal.json 的 soc_url 想改也改不了（doctor
   只读），正确做法是临时把路由器/网关停掉，30 秒内 status 的 SOC 行变
   请求失败/未上报、待发送条数开始增长，然后 status 仍 rc0 或 rc3 视心跳而定
   （心跳照常=健康，SOC 断连是"降级"不是"故障"）；恢复后条数自动回 0；
5. 停启：./asgctl stop → launchctl print gui/501/com.asg.terminal 报找不到；
   ./asgctl start → 60 秒内恢复健康；
6. 体检：./asgctl doctor → 十行 OK、rc0；故意 cat /dev/null > 一个 app 文件再
   doctor → 第 1 项点名该文件；还原后全 OK；
7. 升级：造 B 包（改 version 重新 build）→ ./asgctl upgrade --package B →
   版本行变新、用户键保留；
8. 卸载：./asgctl uninstall → 服务查无、config/state/logs 还在；再跑一次 →
   "已卸载（没有安装记录）" rc0。
全部符合预期 = 五入口人工口径通过（自动化口径以 §10 报告为准）。

## 13. 打勾清单（本项对外口径，截至 9eeca82 / 包 317732f5）

| 规格条目 | 状态 | 证据 |
|---|---|---|
| 五类入口都能执行 | 通过 | e2e 17/17，绑定包 317732f5 |
| 干净目录安装成功、不依赖开发源码 | 通过 | 步骤 1-2（包内自带运行时） |
| 失败场景给准确原因、不误报成功 | 通过 | 步骤 9-11（rc2/rc5/四类故障各自定位） |
| 升级失败恢复旧版本、配置缓存不丢 | 通过 | 步骤 13-14（71.1s 回滚、队列行保留） |
| 卸载不破坏用户后来修改的配置 | 通过 | 步骤 15-17（修改项冲突保留、用户键原样） |
| 报告绑定最终交付包摘要 | 通过 | report.json 每步 package_sha256=317732f5 |
| 停 ASG 后真实 Hook 仍直报+拒绝（绑当前包） | **待跑**（需停本机服务约 15 分钟的空闲窗口） | 步骤见 §13.1 |

按规格"缺任意一条只能写部分完成"，本项对外口径是：
"自动化 17/17 通过，绑定包 317732f5；真实 Agent 全停验收待跑"。不写"全部完成"。

### 13.1 还差的最后一条：真实 Agent 在"ASG 全停"下的独立 Hook 验收

规格打勾清单里唯一没对当前发布包重跑的一条：停掉 ASG 终端服务后，真实 Agent
的 Hook 仍能直报 SOC 并落实拒绝。如实状态：既往正向证据（DSH 聊天/模型/工具
阻断、WorkBuddy 拒绝生效、OpenCode 直连决策）产自 8081 开发环境上下文，不是
本验收要求的"交付包上下文"，不重跑就一直记欠账。

还没跑的原因（不是忘了）：需要停本机 com.asg.soc-collector 与 8081 面板约
10-15 分钟——这两个是用户明确要求不打扰的运行中服务，要等空闲窗口。

草履虫步骤（预计 15 分钟，用 OpenCode 演示实例，当前 pid 见 target.json）：

1. 打开该实例 state 目录 package-receipt.json 指向的 Hook 入口文件，确认它写死
   的上报地址是网关 http://127.0.0.1:8095，且全文没有 8081、没有任何 ASG home
   路径引用。**有引用 = 架构直接判失败，先修这里。**
2. 记录基线：该 Agent 在 SOC 当前事件条数
   （psql -h 127.0.0.1 -p 55432 -U mac -d asg_soc_poc，表 asg_runtime_events，
   按 instance_id 计数）。
3. 停终端服务：launchctl bootout gui/501/com.asg.soc-collector；
   pgrep -fl asg 存档输出确认无 ASG 进程（这份快照是本验收的核心证据）。
4. 经 OpenCode API（POST /session，再 POST /session/ID/message）发一轮
   "创建指定文件名"的 canary；再发一轮应命中拒绝策略的 canary，
   确认目标文件**没有**被创建。
5. 查 SOC：该实例 user.input / assistant.output / tool 事件条数比基线多，
   拒绝轮有决策记录且文件确未生成。
6. launchctl bootstrap 恢复服务，asgctl status 回健康，把结果追加到
   TERMINAL_LIFECYCLE_ACCEPTANCE_20260921.md 并更新上表。

通过 = 1-5 全部成立且第 4 步发生在"无 ASG 进程"时刻；任一不成立 → 如实记失败原因。

## 14. v1 → v2 变更说明

v1（绑定 df561a9）：命令级步骤表 + 函数名行号索引。
v2（本文件，绑定 9eeca82）：新增 §2 全部支撑函数的判定级规格（含固定错误文案、
阈值常量、平台差异）、§8 监督器规格、§9 打包器规格、§10 验收 17 步逐步骤
做法与门槛、§12 人工冒烟手册；§13.1 欠账条目与 v1 一致（仍未跑，如实保留）。
行号基准整体从 df561a9 迁移到 9eeca82（asgctl.py 本体两提交间零改动，
仅验收脚本有 36+/11- 的健壮性修复，故 §2-§7 规格对两提交同时成立）。

