# 交付项 7「安装、启动、检查、升级、卸载」——草履虫级实现手册（2026-09-21）

先说结论：这一项**已实现并有自动化验收**（17/17 通过，绑定包摘要
5362cd0a8ca0f4c18b259d41baccb173be33b609c4a7a6519b430d9ee18bc3c4）。
本手册回答的是"这一句话到底怎么变成代码、每一步照做能不能复现、怎么算好"。
实现文件：release/asgctl.py（全部 7 个子命令）、release/build_release.py（打包）、
release/service_main.py（服务监督器）、e2e/verify_terminal_lifecycle.py（自动验收）。
行号以提交 df561a9 为准；手册里每个函数名都可以用
grep -n 'def 函数名' release/asgctl.py 直接定位。

读法：每个入口按固定套路写——入口命令、执行顺序（每步失败返回什么）、
失败处理、手动验证命令、通过门槛。任何人照对应小节可以重新实现一遍。

---

## 0. 五个名词，先懂再看

| 名词 | 是什么 | 具体位置 |
|---|---|---|
| 交付包 | 解压即用的目录：程序 + 自带 Python 运行时 + 清单 release.json | 现在这份：/tmp/asg-rel-A5-XhXf/asg-terminal |
| home | 安装后的家目录，程序副本、配置、数据、日志全在这 | macOS 默认 ~/Library/Application Support/ASG；验收测试用 --home 指到临时目录 |
| current | home 里的软链接，指向 releases/ 下正在运行的版本；切换它等于切换版本 | home/current |
| 心跳文件 | 服务每 5 秒写一次的"我确实在干活"证据；全系统健康判断只认它，**不认服务管理器的 running** | home/state/operations/heartbeat.json |
| 收据 | 往 Agent 里写 Hook 时留下的记录：写了哪些文件、每个文件写完后的 sha256。卸载只按收据回滚，没收据的东西一律不动 | 每个安装实例状态目录里的 package-receipt.json |

统一返回码（所有子命令一致；验收脚本只读 rc 和 --json，不解析中文）：

| rc | 含义 | 谁会返回 |
|---|---|---|
| 0 | 成功 | 全部 |
| 1 | 执行失败，或升级失败已回滚 | install / start / stop / upgrade |
| 2 | 包或配置类错误：缺文件、摘要不符、平台不符、该用 upgrade 却跑了 install | install / upgrade |
| 3 | 服务注册着但不健康 | status / doctor |
| 4 | 未安装 | status |
| 5 | 服务正常但 SOC 鉴权被拒 | install |

---

## 1. 文件契约（先把"有哪些文件、每个文件的规矩"钉死）

### 1.1 交付包结构（build_release.py 产出，目录名可换、职责不可少）

    asg-terminal/
    |-- asgctl          三行 sh 包装：用包内 python 跑 release/asgctl.py，不依赖用户环境
    |-- release.json    清单：版本/git提交/平台/架构/config版本/db版本/每个文件sha256
    |-- app/            ASG 程序（asg_os_sensor.py, service_main.py, runtime/, integrations/, bin/goose）
    |-- python/         自带 Python 运行时和全部依赖（用户不需要装任何东西）
    |-- release/        asgctl.py 命令实现本体
    |-- services/       com.asg.terminal.plist.template 和 asg-terminal.service.template
    +-- README.md

规格每条要求与代码对应：

| 规格要求 | 实现在哪 | 状态 |
|---|---|---|
| asgctl 可直接执行 | 包根 asgctl 是 sh 包装，用包内解释器，不依赖用户 PATH | 通过 |
| release.json 六要素 | verify_package（asgctl.py:70）逐字段检查，缺哪个字段就报哪个字段名 | 通过 |
| 装前校验全部摘要 | 同上：文件缺失报"文件缺失或形态改变: 文件名"，不符报"文件摘要不一致: 文件名" | 通过 |
| 平台不符要停下 | verify_package 比对 platform.system()/machine() 与清单，不符 rc2 并写明包平台与本机平台 | 通过 |
| 依赖随包、免手动装包 | build_release.py 内嵌便携运行时+psutil 等；e2e 第 2 步删除解压目录后服务仍健康，证明运行闭包完整 | 通过 |
| 下载失败不许说成功 | 依赖固定在包内，安装期零下载；自检失败一律 rc1、不注册服务 | 通过 |

### 1.2 home 布局与每个目录的规矩

    home/
    |-- releases/<版本>/            程序副本；升级=新增目录，绝不原地覆盖运行中版本
    |-- current -> releases/<版本>   原子切换点（临时软链 + os.replace）
    |-- config/terminal.json        soc_url/port/asg_version/service_label/scan_interval... (0600)
    |-- config/endpoint.json        上报端配置 backend_url/state_dir/agents/interval... (0600)
    |-- config/credentials.json     凭据裸文件（O_CREAT 0600 写入）
    |-- state/operations/heartbeat.json      健康唯一依据
    |-- state/endpoint/outbox.sqlite         待发送队列：断网入队、恢复补报、升级不动
    |-- state/installation.json     安装记录：版本/目录/服务名/config摘要(哈希)/时间，无凭据正文
    |-- state/backups/pre-upgrade-<时间戳>/   升级前配置+数据库备份，失败回滚从这里恢复
    +-- logs/service.log            launchd/systemd 重定向目标；监督器 8MB 轮转
    home 之外: ~/Library/Application Support/asg-hooks    Hook 运行目录，卸载扫描器永远碰不到

四条"规矩"的代码保证：

1. releases 不原地覆盖 —— copy_release（asgctl.py:109）先复制到
   releases/版本.staging-随机后缀，逐文件重算 sha256 全对后一次 os.replace 改名转正。
   中途任何异常只会留下 staging 垃圾目录，"半新半旧版本"在结构上不可能出现。
2. config 升级不覆盖 —— write_configs（asgctl.py:341）先读旧 terminal.json，
   旧 dict 打底、只覆盖 asgctl 托管的键；用户自己加的键原样保留。
   endpoint.json 只刷新连接字段，agents/discovery/soc_installation/upload_skill_content
   一律沿用旧值——用户安装时关掉的开关，升级绝不会重新打开。
3. state 永不清空 —— 升级只换 current 软链；数据库用 sqlite 在线备份到 state/backups，
   备份失败时 current 还没动过，直接 rc1 退出。
4. Hook 独立性 —— hook_run_dir（asgctl.py:330）固定放在 home 之外，
   代码注释写明：卸载扫描器永远删不到 Agent Hook 依赖的文件。

### 1.3 heartbeat.json（健康的全部输入，抄一份带注释）

    {
      "t": 1789.0,                  // 写入时刻；年龄>25秒=心跳停
      "pid": 12345,                 // 监督器进程号，doctor 拿它和服务管理器 pid 互核
      "service_version": "0.9.1",   // 运行版本，和期望不符=跑错版本
      "children": { "engine": {"alive": true}, "endpoint": {"alive": true} },
      "engine":   { "reachable": true, "scan_stalled": false,
                    "scan_count": 41, "scan_interval": 60 },
      "endpoint": { "loop_age_s": 12, "bridge_age_s": 30,
                    "soc": {"state": "ok|rejected|error|unknown", "checked_at": 1789.0},
                    "queue": {"pending_requests": 0} }
    }

evaluate_health（asgctl.py:241）：判"健康"当且仅当以下全部成立——
心跳年龄<=25s；engine 与 endpoint 两个子进程都 alive；engine HTTP 可达；
发现循环未停滞（超过 3 个扫描周期没有新扫描=停滞）；上报循环 loop_age<=120s；
桥接循环 bridge_age<=180s；service_version 等于期望版本。
不满足就逐条列原因。status、doctor、升级健康检查、start 等待，四处共用这一个函数，
全系统"健康"只有一个口径。

service_main.py 监督器要点：纯标准库（跑在包内解释器上）；子进程环境先剥掉全部
ASG_ 前缀变量再按需注入（凭据不下传）；发现 app/bin/sabotage.json 标记文件就
拒绝启动、退出 86——这是故意做"坏版本"用的验收钩子，不是彩蛋。

---

## 2. install：12 步固定顺序

入口命令：

    ./asgctl install --soc-url http://SOC地址 --credential-file 凭据文件 \
        [--home 目录] [--json] [--package 包目录] [--port 8081]
        [--scan-interval 60] [--collection-mode manual|auto] [--service-label 名]
        [--skip-soc-installation] [--skip-skill-upload] [--no-discovery]

实现 cmd_install（asgctl.py:429），顺序与每步失败行为：

| 步 | 做什么 | 失败时 |
|---|---|---|
| 1 | 认包：目录里必须有 release.json | rc2 "目录 X 不是交付包（缺少 release.json）"，不注册任何服务 |
| 2 | 验包：verify_package 查平台、六要素、全部文件 sha256 | rc2 点名第一处问题 |
| 3 | 先存凭据再短路：save_credential 放在幂等检查**之前**——改好凭据重跑 install 必须能修复连接（endpoint 每次请求都重读该文件，连重启都不需要） | rc2 "凭据不可用: ..." |
| 4 | 查已有安装：同版本+同配置+健康+服务在 → 探一次 SOC 后回 already_installed（鉴权被拒 rc5）；同版本有残留 → 继续往下修复；不同版本 → rc2 提示改用 upgrade，**绝不直接覆盖** | — |
| 5 | 建目录：releases/config/state/logs/hook 运行目录（顺带验证可写） | rc1 |
| 6 | 复制程序：copy_release 的 staging+校验+一次改名 | rc1 "复制程序文件失败（未注册任何服务）" |
| 7 | 本地自检 self_check（asgctl.py:403）：用包内 python import 全部必需模块 + SQLite 建表试写 | rc1 "本地自检失败（未注册任何服务）" |
| 8 | 写配置：write_configs，合并保留用户键 | — |
| 9 | 切 current：临时软链 + os.replace 原子换 | — |
| 10 | 注册服务 register_service（asgctl.py:172）：mac 写 LaunchAgent plist（RunAtLoad、KeepAlive、ThrottleInterval=10、日志重定向），先 bootout 同名再 bootstrap；Linux 写 user unit 后 systemctl --user enable --now。注册失败 → 把 current 软链恢复旧目标再退出 | rc1 "服务注册失败，未启动任何服务" |
| 11 | 等健康：wait_healthy 轮询心跳最多 60 秒；先写 installation.json 再报错（失败也要有记录） | rc1 "服务已注册但 60 秒内未通过健康检查: 原因（诊断: home/logs）" |
| 12 | 探 SOC 三分支（见下） | — |

soc_probe（asgctl.py:285）是**两次独立请求、两个独立结论**：
GET /api/health 不带凭据，只证明网络通；GET /api/asg/self 带 Bearer，才证明鉴权。
401/403 记 rejected，其余错误记 error。"网关 200 就宣布鉴权正常"这类误判在代码路径上不存在。
第 12 步三分支：鉴权 ok → "安装成功，已连接 SOC" rc0；
网络不可达 → "安装成功；SOC 当前不可达，发现与清点照常，数据暂存本机，恢复后自动补报" rc0；
凭据被拒 → "服务已安装且运行正常；SOC 鉴权失败" rc5。

失败清理硬规则：第 10 步之前失败 → 无已注册服务、current 未切换；
注册后启动失败 → 保留日志、报 installed_unhealthy、rc1，结构上不返回成功；
SOC 离线绝不删本地缓存。

install 怎么算好（每条一个可复制的验证，期望值写死）：

| 测试 | 命令 | 通过 = | 已验证于 |
|---|---|---|---|
| 干净安装 60s 健康 | ./asgctl install --soc-url ... --credential-file ... --home /tmp/h --json | rc0 且 JSON status=installed、healthy=true | e2e 步骤1，实测 10.7s |
| 删解压目录仍正常 | 装完 mv 包目录 包目录.off，再 status --json | rc0 且心跳年龄持续小于 25 | e2e 步骤2 |
| 连装 3 次 | 同参数 install 跑 3 遍 | 后两次 rc0 already_installed；launchctl print 只有一个 pid | e2e 步骤3 |
| 缺文件的包 | 手删 app/ 里任一文件再 install | rc2 且消息点名该文件；launchctl print 查无此服务 | e2e 步骤10 |
| 被篡改的包 | 改任一文件 1 字节再 install | rc2 "文件摘要不一致: 文件名" | e2e 步骤10 |
| SOC 断网 | --soc-url http://192.0.2.1:1（TEST-NET 不可路由） | rc0 且消息含"暂存本机" | e2e 步骤10 |
| 凭据错误 | 给一个错误 key 文件 | rc5，消息含"鉴权失败"，不出现"已连接"字样 | e2e 步骤9 |
| 恢复免重装 | 换正确凭据再跑 install | rc0 且 soc.auth=ok | e2e 步骤9 |

（一个真实踩过的坑：验收里"网络故障"必须用不可路由地址。用本机 closed 端口测出来的
是"连接被拒"，doctor 会正确归为服务故障而不是网络故障——口径混淆会造成假阴。）

---

## 3. start / stop / status

### start（cmd_start:557）
前置：已安装（terminal.json + current 软链都在，否则直接提示先 install）。
顺序：查健康，已健康直接回"正在运行"（幂等）；register_service 拉起；
wait_healthy 最多 60 秒；输出状态。失败 rc1 带逐条原因。
"过期 PID 文件清理"为什么不用单独写：健康只认心跳里的活 pid + 服务管理器现查的 pid，
死 pid 记录天然无效，不存在需要人肉清 PID 文件的路径。

### stop（cmd_stop:579）
unregister_service（mac 是 launchctl bootout + 删 plist；Linux disable --now）→
轮询最多 30 秒确认退出 → 超时 rc1 报具体 pid，绝不假装停了。
"保存待发送队列/清点进度"的实现方式：队列在 SQLite、进度在 state 文件里，
进程死了状态就在，不需要优雅停机魔法。
边界：stop 不碰 Agent、不碰 Hook、不碰 SOC 策略。bootout 移除了 KeepAlive 注册，
所以 stop 之后不存在被服务管理器自动复活的路径——这是"stop 后 30 秒不被拉起"的结构保证。

### status（cmd_status:643 → report_status:604）
人读版逐行输出：服务/版本/健康(异常时带原因)/SOC 连接(带明细)/最近上报检查/
待发送条数/发现循环(扫描计数与间隔)/上报循环心跳/深度清点模式/数据目录。
--json 输出同结构字段供脚本断言。返回码：未安装 4；健康 0；注册着但不健康 3
（服务管理器显示 running 但心跳停了必须是 3，这是刻意的）。

怎么算好：连发 10 次 start 进程数仍为 1；stop 后等 30 秒不被拉起；
start/stop/start 连续 5 轮成功；SIGSTOP 冻结 engine 子进程（进程活着、循环死了）时
status 必须显示异常 rc3。对应 e2e 步骤 8 与 11，全部通过。

---

## 4. doctor：十项检查，每项一句可行动的结论

入口：asgctl doctor [--json]（单行 JSON {command,checks[],healthy}），rc = 全 OK ? 0 : 3。
实现 cmd_doctor（asgctl.py:708），逐项：

| # | 检查项 | 怎么查（代码事实） | 失败时告诉用户什么 |
|---|---|---|---|
| 1 | 程序完整性 | current/release.json 清单 vs 现盘逐文件 sha256 | 点名缺失/漂移文件 |
| 2 | 配置 | terminal.json 必填字段 + 三个配置文件存在性 | 哪个字段/哪个文件 |
| 3 | 本地数据库 | state 下真开事务建表、插入、回滚 | 不可写或损坏 + 异常原文 |
| 4 | 服务身份 | 服务管理器现查 pid 与心跳 pid 互核 | 不一致=跑错版本；没有=未注册 |
| 5 | 工作循环 | 心跳年龄 / engine.reachable / scan_stalled / loop_age | 哪个循环停止推进 |
| 6 | SOC 网络 | /api/health 不带凭据 | DNS/连接/超时分类明细 |
| 7 | SOC 鉴权 | /api/asg/self 带 Bearer，401/403=rejected | 凭据失效或权限不足 |
| 8 | 上报队列 | 心跳里的 pending_requests | 积压条数（断连增长是正常，不判故障） |
| 9 | Hook 安装 | 逐个收据：事务记录 status=installed 且每个写入文件 sha256 与收据 after_sha256 一致 | 点名漂移的文件 |
| 10 | Hook 活动 | 有 activation-receipt.json 才算见过真实回调 | "N/M 项已见真实回调；其余等待目标新活动（无事件不代表 Hook 损坏）" |

规格四条"不能做的事"逐条有代码保证：
无事件不等于 Hook 坏（第 10 项措辞写死）；loaded 不等于输入输出阻断全通（9/10 分开）；
健康接口 200 不等于鉴权正常（6/7 是两次请求）；doctor 全程只读，无任何写配置路径。

怎么算好：主动制造四类故障，各自给出**各自的**原因，不许全糊成"服务异常"。
e2e 步骤 11 实测：换坏凭据 → 只有第 7 项 FAIL；soc-url 指 TEST-NET → 第 6 项 FAIL
且第 7 项注明"网络不可达无法验证"；删 app/identities.yaml → 第 1 项点名该文件；
SIGSTOP 冻结 engine → 第 5 项 FAIL 且 status 同时 rc3；每类恢复后 doctor 转全 OK。

---

## 5. upgrade：新版本先起、健康才转正；失败走显式回滚分支

入口：asgctl upgrade --package 新包 [--force]。实现 cmd_upgrade（asgctl.py:860）。

顺序（每步标注失败时 current 动没动）：
1. 验新包 verify_package（未动）rc2；
2. 同版本拒绝、提示 --force（未动）；
3. copy_release 到 releases/新版本（未动）rc1；
4. 新版本离线自检，失败删新版本目录（未动）rc1；
5. 备份三个配置 + sqlite 在线备份到 state/backups/pre-upgrade-时间戳，
   备份失败（未动）rc1——**先备份再动 current 是硬顺序**；
6. unregister 旧服务；
7. write_configs 继承旧配置（用户键保留、关掉的开关不被重开）；
8. 临时软链 + os.replace 切 current；
9. register 新服务，失败 → 软链切回旧目标、重新注册旧版；
10. wait_healthy 60 秒。

健康失败的回滚是代码里的显式分支（不是口头承诺）：
恢复三个配置（重设 0600）→ 恢复数据库（sqlite 反向 backup）→ 软链切回旧目标 →
重新注册 → wait_healthy(旧版, 90 秒) → installation.json 记 upgraded_failed_at →
返回 status=failed_rolled_back、rc1，消息"升级失败，已恢复旧版本 X（备份: 路径）"。
该路径的返回值里没有任何字段可能被读成"升级成功"。

额外规格"升级 ASG 不自动重写 Agent Hook"：cmd_upgrade 全流程零调用 Agent 配置写入；
成功消息明说"Agent Hook 不会被自动重写"。旧版本目录至少保留一个
（keep 集合 = 旧+新，其余 releases 才清理）。

怎么算好（e2e 步骤 12-14 实测值）：
正常升级后用户自加的自定义配置键仍在、清点流水线保留（10.3 秒）；
故意起不来的 sabotage 版本升级失败后，旧版本 71.1 秒恢复健康（门槛 90 秒）、
升级前注入的队列行一条不少、随后可直接重试成功；复制期间中断只留 staging 垃圾
（结构保证，无需测试兜底）。

---

## 6. uninstall：删哪些、留哪些，白纸黑字

入口：asgctl uninstall 或 asgctl uninstall --remove-hooks。
实现 cmd_uninstall（asgctl.py:1037）+ remove_hooks（asgctl.py:995）。

默认卸载**删**：系统服务注册、releases 程序目录（仅符合 is_release_layout 判定的
目录——用户自己放进 releases 的东西不动）、current 软链、installation.json
（替换为 uninstalled.json，让第二次运行能如实报 not_installed）。
默认卸载**留**：config、state（数据库/队列全在）、logs、home 之外整个 Hook 运行目录、
Agent 里已装的 Hook 文件。输出 JSON 里 kept 字段明说每个保留位置。
收尾验证：再查一次 service_loaded，进程还在就 rc1。

--remove-hooks 的精确范围（"卸载不破坏用户修改"的核心）：
1. 逐个读 Hook 安装记录；只处理 ASG 有收据的 learned 安装；
2. 原生 Hook（由目标自身信任机制管理的，如 Codex）不动文件，只提示在 Agent 内确认卸载；
3. 收据缺失 → 保留现状、报冲突；
4. 回滚走收据里的 plan_digest 调 learned_install.rollback——
   该函数对"安装后被用户改过"的文件是**拒绝覆盖、报冲突、保留原文**，
   绝不整份还原成旧备份（这条语义是 learned_install 的既有契约，卸载只是复用）；
5. 全部处理完才执行默认卸载。

怎么算好（e2e 步骤 15-17 实测）：
默认卸载后 launchctl print 查无服务、程序目录消失，config/state/logs/hook 四处保留；
种两项学习 Hook（一项原样、一项装后故意被"用户"修改）再 --remove-hooks：
原样项回滚成功、修改项保留并计入 hook_conflicts、doctor 只点名修改项漂移不误报原样项、
工作区无关文件与用户配置键两轮卸载后原样、第二次卸载 rc0 not_installed。

---

## 7. 还差的最后一条：真实 Agent 在"ASG 全停"下的独立 Hook 验收

规格第十条打勾清单里唯一没对当前发布包重跑的一条：
停掉 ASG 终端服务后，真实 Agent 的 Hook 仍能直报 SOC 并落实拒绝。
如实状态：既往正向证据（DSH 聊天/模型/工具阻断、WorkBuddy 拒绝生效、OpenCode 直连决策）
都产自 8081 开发环境上下文，不是这份生命周期验收要求的"交付包上下文"，
不重跑就一直记欠账。

还没跑的原因（不是忘了）：本机 com.asg.soc-collector 与 8081 面板是用户明确要求
不打扰的运行中服务，该验收必须把它们停 10-15 分钟；需要一个空闲窗口。

草履虫步骤（预计 15 分钟，用 OpenCode 演示实例 pid 57879）：

1. 打开该实例 state 目录里 package-receipt.json 给出的 Hook 入口文件，
   确认它写死的上报地址是网关 http://127.0.0.1:8095，且全文没有 8081、
   没有任何 ASG home 路径引用。**有引用 = 架构直接判失败，先修这里。**
2. 记录基线：该 Agent 在 SOC 当前事件条数。
3. 停终端服务：launchctl bootout gui/501/com.asg.soc-collector；
   pgrep -fl asg 存档输出确认无 ASG 进程（这份快照是本验收的核心证据）。
4. 经 OpenCode API（POST /session，再 POST /session/ID/message）发一轮
   "创建指定文件名"的 canary；再发一轮应命中拒绝策略的 canary，
   确认目标文件**没有**被创建。
5. 查 SOC：该实例 user.input / assistant.output / tool 事件条数比基线多，
   拒绝轮有决策记录且文件确未生成。
6. launchctl bootstrap 恢复服务，asgctl status 回健康，报告 rc。

通过 = 1-5 全部成立且第 4 步发生在"无 ASG 进程"时刻；任一不成立 → 如实记失败原因。
跑完后把结果追加到 TERMINAL_LIFECYCLE_ACCEPTANCE_20260921.md 并更新下表。

---

## 8. 自动验收脚本的写法要求（规格第九条的落地，防止"测试只测退出码"）

入口：python3 e2e/verify_terminal_lifecycle.py --package 交付包
      [--gateway http://127.0.0.1:8095] [--enrollment-key 应用key] [--keep]

写法铁律（都在现有代码里）：
唯一临时 home（mkdtemp）+ 独立服务标签 com.asg.terminal.e2e-*，绝不碰开发实例；
每个步骤记录 {step, expected(中文预期), package_sha256, started_at, elapsed_s,
pass, detail/error}；断言一律读 --json 字段和 rc，禁止解析中文提示判成败；
失败保留临时目录和日志并打印 report.json 路径，成功才自动卸载清场（--keep 强制保留）。

17 步与规格第十二步清单的映射：规格 2/3/4 → 步骤 1-3；规格 5 → 步骤 8（10 轮启停）；
规格 6/8 → 步骤 6-7（断网入队、恢复清空、无重复修订）；规格 7 → 步骤 13；
规格 9 → 步骤 14（sabotage 回滚）；规格 10 → 步骤 12/16/17（用户键与修改过的 Hook）；
规格 11 → 步骤 15；规格 12/13 → Runner 报告逻辑。
最新报告目录：/var/folders/xf/_m1f6xjn7cd55zzpvqp3r3f80000gn/T/asg-lifecycle-acceptance-qvq3zic7/report.json（17/17）。
回归测试单独跑：python3 -m unittest 显式列 13 个 integrations.soc_inventory.test_* 模块，
共 58 个测试；用 discover 会因相对导入报错，别用。

---

## 9. 打勾清单（本项对外口径，截至 df561a9）

| 规格第十条条目 | 状态 | 证据 |
|---|---|---|
| 五类入口都能执行 | 通过 | e2e 17/17 |
| 干净目录安装成功、不依赖开发源码 | 通过 | 步骤 1-2，包内自带运行时 |
| 失败场景给准确原因、不误报成功 | 通过 | 步骤 9-11（rc2/rc5/doctor 四类故障） |
| 升级失败恢复旧版本、配置缓存不丢 | 通过 | 步骤 13-14（71.1s 回滚、队列行保留） |
| 卸载不破坏用户后来修改的配置 | 通过 | 步骤 15-17 |
| 停 ASG 后真实 Hook 仍直报+拒绝（绑当前包） | **待跑**（需停本机服务约 15 分钟的空闲窗口） | 步骤见第 7 节 |
| 报告绑定最终交付包摘要 | 通过 | report.json 每步含 package_sha256 |

按规格"缺任意一条只能写部分完成"：本项对外写
"自动化 17/17 通过，绑定包 5362cd0a；真实 Agent 全停验收待跑"，不写"全部完成"。
