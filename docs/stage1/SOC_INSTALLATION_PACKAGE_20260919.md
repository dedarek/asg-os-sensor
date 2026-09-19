# SOC Hook 安装包交付与验收（2026-09-19）

## 当前结论

尚未完成所有类型原 SOC 一键部署和自动挂接。已完成通用文件计划安装包、SOC 入库/原下载接口以及 OpenCode 新实例加载回调验收。新包需要终端已有 ASG 运行时；不是原 SOC 四种 SDK 安装器的无条件替代品。

## 已实现

- 归档包含安装、预检、卸载、事务备份与回滚代码，提供 install.sh 和 install/install.sh 入口。
- 安装检查平台、架构、目标可执行文件及应用 bundle 哈希；错误构建拒绝写入。
- 重复安装幂等；文件被用户修改时拒绝覆盖及破坏性回滚。
- --verify-pid 校验执行文件、工作区、已安装文件哈希和本次安装之后的真实 hook.loaded 事件，生成实例绑定回执。
- 采集器在主动采集时上报完整安装包；未声明完整文件集的旧配方仍作为配方包保留。
- 网关拒绝路径越界、链接、重复文件、过大解包内容、缺失入口、配方不一致的安装包。
- 安装包存入原 artifact_packages / MinIO，使用原 SOC 制品下载接口，下载内容逐字节相等。
- 完整文件集作为部署配方单独保存，不覆写现有运行时 Hook 配方。

## 实测

| 项目 | 结果 |
|---|---|
| OpenCode 包上传、存储、下载 | 通过；learned-install-0390aea69902fca0f067 |
| OpenCode 安装、重复安装、卸载 | 通过 |
| SOC 下载的 OpenCode 包，新工作区加载 | 通过；PID 81385；create_time 1789759882.481516；1 条本实例 hook.loaded |
| ZCode 包上传、存储、下载 | 通过；learned-install-41ee7c1be70844368f7c |
| ZCode 干净工作区安装、重复安装 | 通过；补齐注册配置和启动器 |
| ZCode 官方信任 CLI | 隔离 HOME 内 grant 后返回 trusted_persistent |
| ZCode 自动加载 | 未通过：真实 headless 会话返回 workspace_hooks_feature_disabled，没有 Hook 回调 |
| ZCode 模型调用 | 失败：本地模型 Responses 流出现 text part 解析错误；不能替代加载验收 |
| Python 回归 | 12 项通过 |
| Go controller 回归（含包边界） | 通过 |
| Vue 类型检查 | 通过 |

## 使用安装包

先从制品页面下载 learned-install-*，解压，在运行 Agent 的终端执行：

```sh
bash install.sh --target /absolute/workspace --exe /absolute/agent-executable --asg-root /absolute/asg-installation
```

预检追加 `--dry-run`。目标重新加载之后追加 `--verify-pid ACTUAL_PID` 验证真实加载回调。卸载使用相同路径参数运行 `uninstall.sh`。

安装器不改变目标原生信任开关。ZCode 需要通过支持工作区 Hook 的桌面入口验收；headless CLI 的禁用功能没有绕过。

## 未完成且不得混淆

- 原 SOC bootstrap 注册后传入的 backend-url / token 等参数尚未完整转换成 ASG 终端配置，因此原 SOC 一键注册部署不是本轮通过项。
- 新包仍依赖现有 ASG 终端运行时，部分历史脚本有本机解释器路径；异机部署尚未验收。
- ZCode 桌面新会话及其他所有类型尚未逐一完成回调验收。
- 本轮 hook.loaded 只证明加载，不代表完整输入输出、工具控制、重启及断网复用全部通过。
- learned-* 禁止自动提升为当前生产安装包，保留发布门禁。

证据目录：artifacts/acceptance/soc-installation-packages/。OpenCode 回执位于 opencode-test.json；存储下载校验及包文件保存在同目录。
