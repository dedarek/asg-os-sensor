# Stage1 REVIEW（唯一最新入口: docs/stage1/）

根目录 PLAN.md / WORKLOG.md / REVIEW.md 只保留历史轮次，最新进度与证据一律以
docs/stage1/ 下文件为准。

## 本轮改动摘要（独立提交）
- 中断检查点 + 502 未提交批次整体收口（含此前 8 改 5 增），详见 WORKLOG。

## 中途 review 修复（顾问复核后）
1) recipe_validation.validate：证据必须绑定冻结实例 (target.pid+create_time)、
   真实非占位成功结果、观测工具白名单；hook_evidence_supported 恒 False
   （proposed/unverified）——结构校验通过 ≠ Hook 建议有证据支持，任意外部
   method（含虚构）不构成 supported。
2) 生命周期 instance_id = pid:create_time 贯穿 running/retry/result/API；
   _record_investigation_result 结束时不再重读 psutil，PID 复用不串 result。
3) _family_identity_matches：身份 vs 构建兼容分离；entry='native' 常量不作身份；
   同壳不同 app 不合并；原生升级（包名同）允许演进；exact 仍由 compatibility
   digest 整体判定，升级后 digest 变则不做 exact。
4) collection 保留成功项 + 局部失败；文案区分本机读取与脱敏外发。
5) 呈现层保守化：succeeded 消息的旧"接入点建议有证据支持"改写为
   proposed/unverified，重启不恢复虚假结论。

## 真实验证结果（隔离目标 authorized-goose）
- 真实 Goose 对 OpenCode 45780 完成调查：3 条实例绑定证据 → 校验通过落库
  revision 2（revision 1 保留）→ 同实例再扫描 exact 复用，无新增 Goose 调用；
  hook 未安装、model_routing 未采集、network 已采集。
- 证据归档 artifacts/stage1/authorized-goose/pid_45780_1788962638/（gitignore）。
- 跨实例复用（测试级）：合成目标（随机命名脚本）实例 A 落库后同入口新实例 B
  classify=exact，调查函数 0 次新增调用。

## 复现命令
- 全量: python3 -B -m unittest test_discovery test_matcher_stage1 \
  test_status_stage1 test_goose_stage1 → 47 tests OK。
- 演示服务 8081: artifacts/stage1/authorized-goose/start.py（隔离库,
  ASG_PORT=8081, ASG_ALLOW_INSECURE_ANALYST=1 用户已授权）。停止: kill <service.json pid>。

## 已知限制与未完成项（诚实清单）
- 真实 Goose 仅对 OpenCode 45780 跑过一次；其余候选（ZCode/codex/bun）在
  ASG_ANALYST_TARGET_PID 单目标限制下为 not_scheduled/miss，资产未采集。
- 资产采集范围有限：仅进程打开的受限 JSON 配置 + CWD 预设文件名；MCP/Skill/
  规则未采集，网络已采集，model_routing 未采集。
- Hook 恒未安装/未验证；接入点建议一律 proposed/unverified。
- Windows msvcrt 锁分支未实测（macOS）。
- 真实跨版本目标复用尚未复跑（测试覆盖脚本/原生升级同族演进）。

## 进入下一阶段（Hook 安装与生效验证）的条件
- 条件未齐：接入点证据映射（验证 hook.method 存在）、Hook 安装器、生效验证与
  回滚、真实跨版本复用复跑。当前完成 发现→调查→落库→复用。


## 架构边界：通用核心 vs OpenCode 特定（泛化性约束）
- 通用（无产品名特判）：发现评分（asg_os_sensor.Sensor）、实例归属（identity.
  ownership, instance_id=pid:create_time）、证据采集接口（collection.collect 契约）、
  调查调度（monitor_dashboard 生命周期）、指纹与版本（matcher：classify exact/
  similar/miss + revisions + 演进门禁）、配方校验（recipe_validation：绑定实例+
  真实成功结果+hook proposed/unverified）、事件契约（runtime/hook_node/preload.js
  与 e2e/hook/sitecustomize.py：只抓公共面、脱敏、fail-open）。
- OpenCode 特定（仅作为第一份真实验收样本，不进入核心）：桌面包 bundle 元数据、
  本地监听端口 52714 的候选观察点。其配置路径（~/.config/opencode/opencode.jsonc、
  Application Support/ai.opencode.desktop、/Users/mac/CLAUDE.md 存在但进程未加载）
  均只作为证据字段记录，不作为核心逻辑输入。
- 第二种适配（合成/mock）：test_adapter_stage1.py 以 Python sitecustomize 机制走
  同一 core 契约，证明换配方不改核心；unittest 通过，明确标注不冒充第二个真实
  Agent 已验收。
- 能力边界：发现/识别身份/资产可见性/可观测/可阻断为独立能力，不承诺所有 Agent
  可 Hook；无法接入时明确 unsupported/limited，不伪成功。
