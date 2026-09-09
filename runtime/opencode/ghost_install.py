# -*- coding: utf-8 -*-
"""ASG OpenCode 观测插件：通用安装计划 / dry-run / 隔离安装 / 卸载回滚。

所有操作只针对独立测试工作区目录（--workspace），不触碰用户全局配置、现有项目，
不打开 UI、不重启/终止现有 Agent 实例。dry-run 仅打印将要执行的步骤与文件清单。

用法：
  python3 ghost_install.py --plan           列出步骤（不落盘）
  python3 ghost_install.py --preflight --workspace DIR  校验前置条件并打印隔离局限
  python3 ghost_install.py --install --workspace DIR --nonce NONCE       --name asg-observe.mjs
  python3 ghost_install.py --uninstall --workspace DIR --name asg-observe.mjs
退出码：0 成功，1 失败；dry-run 输出 JSON 供审查。
"""
import argparse
import base64
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
PLUGIN_SRC = ROOT / "runtime" / "opencode" / "asg-observe.mjs"
PATTERN = "{plugin,plugins}/*.{ts,js}"


def log(msg):
    print(msg, file=sys.stderr)


def emit_plan(steps, dry_run=False):
    if dry_run:
        print(json.dumps({"plugin": str(PLUGIN_SRC), "pattern": PATTERN, "steps": steps}, ensure_ascii=False, indent=2))
    else:
        for s in steps:
            log(s)


def plan(workspace: Path, name: str):
    ws = workspace.resolve()
    steps = [
        "5. copy plugin to workspace plugin search dir",
        "6. set event file & nonce env (ASG_OBSERVE_EVENTS/ASG_OBSERVE_NONCE)",
        "7. user opens workspace in Desktop (UI; cannot be automated)",
        "8. engine loads plugin -> hook.loaded event; receiver validates PID+create_time+nonce",
    ]
    # 1-4 是它依赖的：workspace 内 .opencode/plugins 需位于插件搜索路径。
    s2 = [
        "1. workspace=%s (isolated temp; not user project)" % ws,
        "2. search pattern " + PATTERN + " relative to workspace config dir",
        "3. ensure workspace/.opencode/plugins exists (plugin dir)",
        "4. engine installs @opencode-ai/plugin dep into that dir on load",
    ] + steps
    return s2


def preflight(workspace: Path):
    ws = workspace.resolve()
    issues = []
    if not ws.exists() or not ws.is_dir():
        issues.append("workspace missing: %s" % ws)
    plug = ws / ".opencode" / "plugins"
    if plug.exists() and any(plug.iterdir()):
        issues.append("plugin dir not empty (existing plugin will conflict): %s" % plug)
    # 隔离局限：引擎会向全局配置目录搜索 opencode.json/opencode.jsonc/config.json，
    # 并 install @opencode-ai/plugin 到工作区目录 —— 全局行为未被隔离。
    limits = [
        "global config (opencode.json/opencode.jsonc/config.json) still searched by engine",
        "engine installs @opencode-ai/plugin into workspace dir on load (network fetch)",
        "plugin loads only when workspace is opened in Desktop UI",
    ]
    return {"workspace": str(ws), "plugin_src": str(PLUGIN_SRC), "clean": len(issues) == 0, "issues": issues, "limits": limits}


def install(workspace: Path, name: str, nonce: str):
    ws = workspace.resolve()
    plug = ws / ".opencode" / "plugins"
    plug.mkdir(parents=True, exist_ok=True)
    dest = plug / name
    if dest.exists():
        log("refusing to overwrite existing plugin: %s" % dest)
        return 1
    data = PLUGIN_SRC.read_bytes()
    dest.write_bytes(data)
    print(json.dumps({"installed": str(dest), "sha256": hashlib.sha256(data).hexdigest(),
                      "nonce": nonce, "events_env": "ASG_OBSERVE_EVENTS",
                      "next": "open workspace in Desktop UI to load plugin"}, ensure_ascii=False))
    return 0


def uninstall(workspace: Path, name: str):
    ws = workspace.resolve()
    dest = ws / ".opencode" / "plugins" / name
    if not dest.exists():
        log("not installed: %s" % dest)
        return 1
    dest.unlink()
    print(json.dumps({"uninstalled": str(dest), "next": "restart workspace engine to confirm no events"},
                     ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--name", default="asg-observe.mjs")
    ap.add_argument("--nonce", default=None)
    a = ap.parse_args()
    ws = Path(a.workspace) if a.workspace else (ROOT / "artifacts" / "stage1" / "opencode-observe")
    if a.plan:
        emit_plan(plan(ws, a.name), dry_run=True)
        return 0
    if a.preflight:
        print(json.dumps(preflight(ws), ensure_ascii=False, indent=2))
        return 0
    if a.install:
        return install(ws, a.name, a.nonce or secrets.token_hex(8))
    if a.uninstall:
        return uninstall(ws, a.name)
    print("no action; use --plan/--preflight/--install/--uninstall", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
