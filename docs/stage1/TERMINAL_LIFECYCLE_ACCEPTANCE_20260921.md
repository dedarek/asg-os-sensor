# 终端生命周期验收报告（交付项 7 / G14）2026-09-21

结论：**通过**。打包 → 安装 → 心跳健康 → 上报 → 断网补报 → 启停 → 凭据修复 → 负向安装 → doctor 诊断 → 升级保留配置 → 升级故障自动回滚 → 卸载保留数据，15/15 步全部通过，失败 0。

2026-09-21 按细化规格两轮加强验收并复跑通过（最新报告 asg-lifecycle-acceptance-qvq3zic7，17/17）。第一轮：启停从 5 轮加到 **10 轮且 stop 后 30 秒不被服务管理器拉起**；doctor 故障注入从 2 类加到规格要求的 **4 类**（错误凭据、真实网络不可达（TEST-NET 地址）、缺失程序文件、工作循环停滞——用 SIGSTOP 冻结引擎进程验证进程存活但循环停滞时 doctor 与 status 都报异常、恢复后转 OK），并增加 refused 端口不冒充凭据问题的反例；报告每一步新增**预期结果**字段。期间发现并修正一个测试口径错误：本机 refused 端口是服务故障不是网络故障，网络故障必须用不可路由地址验证。

第二轮补上规格第 8 节的卸载范围验收（此前只有默认卸载）：验收脚本先重新安装同一发布包（同时证明卸载真的清掉了程序），再用发布包自带的 learned_install 种入两项 Hook 安装记录——一项原样、一项安装后被用户修改。断言：doctor 的 Hook 安装检查点名且只点名被修改那项的文件漂移；uninstall --remove-hooks 只回滚未修改那项的 ASG 写入文件，被用户修改的文件保留原文并计入冲突；工作区内与 ASG 无关的用户文件、以及用户自定义配置键，在两轮卸载后原样保留；第二次卸载仍报 not_installed。

绑定制品：发布包 release.json sha256
5362cd0a8ca0f4c18b259d41baccb173be33b609c4a7a6519b430d9ee18bc3c4
（构建自提交 67a9954 加大未提交工作区，见下方边界说明）。
验收报告：/var/folders/xf/_m1f6xjn7cd55zzpvqp3r3f80000gn/T/asg-lifecycle-acceptance-d8oep4st/report.json
SOC 网关：http://127.0.0.1:8095（本机 SOC POC 栈）。

## 1 交付内容

1. release/asgctl.py — 终端唯一入口，7 个子命令：
   - install：12 步固定顺序（校验包摘要 → 复制运行闭包到用户目录 → 写配置 → 保存凭据 → 安装系统服务 → 等待健康）。重复执行幂等；已安装时探测 SOC，错误凭据返回 rc5 并支持用新凭据修复。
   - start / stop / status：经系统服务管理，status 读取心跳文件判断健康。
   - doctor：逐项自检（包完整性、身份文件、配置、网络可达、SOC 鉴权、服务状态、队列积压），网络和鉴权分开报告，支持 --json 单行输出。
   - upgrade：暂存目录写完并校验后一次 rename 切换；新版本先起、健康才替换；失败自动回滚旧版本并恢复旧队列。升级继承旧配置，不会重新打开用户关掉的开关。
   - uninstall：删除服务与运行闭包，保留用户配置、状态库、日志和已写入的 Hook 文件。
2. release/build_release.py — 可复现打包：内嵌便携 Python 运行时与全部依赖，校验清单摘要，支持 --version / --out / --sabotage（故障注入测试用）。
3. release/service_main.py — 服务监督器：拉起发现/调查引擎与上报端两个子进程，每 5 秒写 heartbeat.json（子进程存活、扫描轮次、调查状态、SOC 通道三态、队列深度），供 status / doctor / 升级判活共用。
4. integrations/soc_inventory/endpoint.py — 新增 loop-beat / bridge-beat 心跳与 soc-health.json 三态（ok / rejected / error）持久标记，只由清点和注册路径写入，命令桥接不会覆盖。
5. e2e/verify_terminal_lifecycle.py — 本报告所依据的 15 步验收脚本，隔离 --home 与独立服务标签，可随时重放。

## 2 验收执行与结果

命令：python3 e2e/verify_terminal_lifecycle.py（隔离 home、独立端口与 com.asg.terminal.e2e-* 服务标签，不触碰开发实例）。

首次运行暴露 4 个缺陷并全部修复：已安装路径不探测 SOC；doctor --json 多行；卸载后 installation.json 残留导致二次运行状态错误；命令桥接探测覆盖 SOC ok 标记。第二次运行 15/15 通过：

| 步骤 | 结果 | 关键数字 |
|---|---|---|
| install 后 60 秒内健康 | 通过 | 10.7 秒 |
| 安装后删除或改名包目录服务仍存活 | 通过 | 运行闭包在用户目录 |
| 重复 install 幂等（×3） | 通过 | rc0，无重复服务 |
| 注册隔离测试 Agent | 通过 | asg-5bef6df98421cc12271d8ae17beb8265 |
| 首轮清点到达 SOC | 通过 | 5.2 秒，1 条快照 |
| 断网入队 → 恢复补报 | 通过 | 队列增长后清空，soc=ok |
| SOC 快照无重复修订 | 通过 | 2 条快照无重复 revision |
| 10 轮 start / stop 单进程；stop 后 30 秒不自动拉起 | 通过 | 无孤儿进程；65.2 秒 |
| 错误凭据 rc5 加重装修复 | 通过 | 修复后恢复 ok |
| 负向安装：缺文件 rc2 / 篡改摘要 rc2 / SOC 离线仍装成 | 通过 | 均无残留服务 |
| doctor 四类故障注入 | 通过 | 网络与鉴权分离；缺 identities.yaml 点名文件；冻结引擎循环被 doctor 与 status 双双识别并恢复；恢复后 OK |
| 用户配置键跨升级保留 | 通过 | 自定义键存在 |
| 升级 A→B 保留配置与流水线 | 通过 | 10.3 秒完成 |
| 升级故障 90 秒内回滚 | 通过 | 回滚 71.1 秒；旧版本健康；注入队列行保留（总 145.6 秒含二次重试） |
| uninstall 保留配置 / 状态 / 日志 / Hook；二次运行 not_installed | 通过 | rc0 |
| 重装同包后种入两项学习 Hook；doctor 点名被修改项漂移 | 通过 | 未修改项不误报；11.2 秒 |
| uninstall --remove-hooks 精确范围 | 通过 | 未修改项回滚、用户修改保留并报冲突、无关文件与用户配置键不动 |

回归：python3 -m unittest 显式 13 个模块（integrations.soc_inventory.test_channel_and_budget 至 .test_g07_investigation_lifecycle）共 58 个测试全部通过。验收后已确认无残留 e2e 服务。

## 3 已知边界（如实）

- Windows 不在本轮范围（用户明确暂不计入）。
- Linux systemd 单元路径已实现但未实机验证。
- 复制阶段中途断电未做杀进程注入；暂存目录加单次 rename 使半新半旧版本在结构上不可出现。
- doctor 的已学习 Hook 分支只在 8081 开发主机覆盖，e2e 隔离 home 未重复覆盖。
- 本报告在临时目录，重放 e2e 脚本即可再生成；制品内容可由包摘要 5362cd0a 追溯。
- 验收包构建自 67a9954 加当时未提交的工作区改动；本提交合入后用同一提交重新打包应产生同一运行闭包（便携运行时缓存摘要固定）。

## 4 对照 35 点计划

- 交付项 7（终端生命周期：安装、启动、检查、升级、卸载入口）：**通过**，绑定包摘要 5362cd0a。
- 交付项 1–6：状态不变，逐项见 GAP_AUDIT_20260920.md 的 G01–G13、G15、G16，本报告不改变任何未验收项的结论。
