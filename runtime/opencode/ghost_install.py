# -*- coding: utf-8 -*-
"""ASG OpenCode 观测插件：通用安装计划 / dry-run / 隔离安装 / 卸载回滚。

只针对独立测试工作区目录（--workspace，默认 artifacts/stage1/opencode-observe，
该目录在 .gitignore 内）：不触碰用户全局配置、现有项目，不打开 UI，不重启/终止
现有 Agent。安装时把 nonce 写入插件同目录 .asg-observe/nonce；事件写入同目录
events.jsonl（权限 0600）；两者不依赖注入环境变量，用户打开工作区即可工作。

安全约束（评审要求）：
- ROOT 通过查找含 runtime/opencode/asg-observe.js 的仓库根来定位，不限 cwd。
- name 只能是纯文件名（拒绝绝对路径/../ 穿越）；workspace 必须已存在且为目录。
- 拒绝 symlink 目标；插件文件用 O_CREAT|O_EXCL 原子独占创建（已存在即拒绝覆盖）。
- 持久 manifest 记录 name/sha256/nonce/workspace/installed_at；卸载必须校验
  manifest+hash，文件被修改后拒绝删除（保留用户文件负例）。
"""
import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
from pathlib import Path
from datetime import datetime, timezone

PLUGIN_NAME = "asg-observe.js"
STATEDIR_NAME = ".asg-observe"


def repo_root():
    """任意 cwd 下定位仓库根：向上找含 runtime/opencode/asg-observe.js 的目录。"""
    here = Path(__file__).resolve()
    for ancestor in [here] + list(here.parents):
        if (ancestor / "runtime" / "opencode" / PLUGIN_NAME).exists():
            return ancestor
    raise SystemExit("cannot locate repo root (runtime/opencode/%s not found)" % PLUGIN_NAME)


def plugin_source() -> Path:
    return repo_root() / "runtime" / "opencode" / PLUGIN_NAME


def log(msg):
    print(msg, file=sys.stderr)


def sanitize_name(name: str) -> str:
    """只允许纯文件名：拒绝绝对路径、目录分隔与 .. 穿越。"""
    base = os.path.basename(name)
    if not name or name != base:
        raise ValueError("plugin name must be a bare filename")
    if base in ("", ".", "..") or "/" in base or chr(92) in base:
        raise ValueError("plugin name must be a bare filename")
    return base


def check_symlink_free(p: Path, label: str):
    try:
        st = p.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise ValueError("%s must not be a symlink: %s" % (label, p))


def ensure_workspace(ws: Path) -> Path:
    ws = ws.resolve()
    if not ws.exists():
        raise ValueError("workspace missing: %s" % ws)
    if not ws.is_dir():
        raise ValueError("workspace is not a directory: %s" % ws)
    return ws


def plugin_dir(ws: Path) -> Path:
    return ws / ".opencode" / "plugins"


def state_dir(ws: Path) -> Path:
    return ws / ".opencode" / "plugins" / STATEDIR_NAME


def manifest_path(ws: Path) -> Path:
    return state_dir(ws) / "manifest.json"


def plan(ws: Path, name: str):
    return [
        "1. workspace=%s (isolated; not user project)" % ws.resolve(),
        "2. engine plugin glob {plugin,plugins}/*.{ts,js} scans cwd=config dir",
        "3. create workspace/.opencode/plugins/%s (bare filename only)" % name,
        "4. write .asg-observe/nonce + events.jsonl (0600) beside plugin",
        "5. write/refresh manifest.json (name, sha256, nonce, workspace, installed_at)",
        "6. user opens workspace in Desktop UI (cannot be automated)",
        "7. engine loads plugin -> hook.loaded event; receiver validates PID+create_time+nonce",
    ]


def preflight(ws: Path, name: str):
    ws = ws.resolve()
    issues = []
    if not ws.exists():
        issues.append("workspace missing: %s" % ws)
    elif not ws.is_dir():
        issues.append("workspace not a dir: %s" % ws)
    pd = plugin_dir(ws)
    try:
        check_symlink_free(pd, "plugin dir")
    except ValueError as e:
        issues.append(str(e))
    if pd.exists():
        for item in pd.iterdir():
            if item.name not in (STATEDIR_NAME, name):
                issues.append("unexpected file preserved: %s" % item.name)
    limits = [
        "global config (opencode.json/opencode.jsonc/config.json) still searched by engine",
        "engine installs @opencode-ai/plugin dep into workspace dir on load (network fetch)",
        "plugin loads only when workspace is opened in Desktop UI",
    ]
    return {"workspace": str(ws), "plugin": str(plugin_source()), "name": name,
            "clean": len(issues) == 0, "issues": issues, "limits": limits}


def install(ws: Path, name: str, nonce: str):
    ws = ensure_workspace(ws)
    name = sanitize_name(name)
    src = plugin_source()
    pd = plugin_dir(ws)
    check_symlink_free(pd, "plugin dir")
    pd.mkdir(parents=True, exist_ok=True)
    sd = state_dir(ws)
    sd.mkdir(parents=True, exist_ok=True)
    dest = pd / name
    if dest.exists():
        log("refusing to overwrite existing plugin: %s" % dest)
        return 1
    data = src.read_bytes()
    fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    sha = hashlib.sha256(data).hexdigest()
    for fname, content in (("nonce", nonce + chr(10)), ("events.jsonl", "")):
        fpath = sd / fname
        fd2 = os.open(str(fpath), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd2, content.encode())
        finally:
            os.close(fd2)
    manifest = {"name": name, "sha256": sha, "nonce": nonce,
                "workspace": str(ws), "installed_at": datetime.now(timezone.utc).isoformat()}
    mp = manifest_path(ws)
    tmp = mp.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    os.replace(tmp, mp)
    print(json.dumps({"installed": str(dest), "sha256": sha, "nonce": nonce,
                      "events_file": str(sd / "events.jsonl"),
                      "next": "open workspace in Desktop UI to load plugin"}, ensure_ascii=False))
    return 0


def uninstall(ws: Path, name: str):
    ws = ws.resolve()
    name = sanitize_name(name)
    dest = ws / ".opencode" / "plugins" / name
    sd = state_dir(ws)
    mp = manifest_path(ws)
    if not dest.exists():
        log("not installed: %s" % dest)
        return 1
    if not mp.exists():
        log("refusing uninstall without manifest: %s" % mp)
        return 1
    man = json.loads(mp.read_text())
    if man.get("name") != name or man.get("workspace") != str(ws):
        log("manifest does not match install (name/workspace)")
        return 1
    if hashlib.sha256(dest.read_bytes()).hexdigest() != man.get("sha256"):
        log("plugin file was modified since install; refusing delete (preserve user files)")
        return 1
    dest.unlink()
    for fname in ("nonce", "manifest.json"):
        p = sd / fname
        if p.exists():
            p.unlink()
    print(json.dumps({"uninstalled": str(dest),
                      "next": "restart workspace engine to confirm no events"}, ensure_ascii=False))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--preflight", action="store_true")
    ap.add_argument("--install", action="store_true")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--workspace", default=None)
    ap.add_argument("--name", default=PLUGIN_NAME)
    ap.add_argument("--nonce", default=None)
    a = ap.parse_args()
    ws = Path(a.workspace) if a.workspace else (repo_root() / "artifacts" / "stage1" / "opencode-observe")
    try:
        if a.plan:
            print(json.dumps({"plugin": str(plugin_source()),
                              "pattern": "{plugin,plugins}/*.{ts,js}",
                              "steps": plan(ws, a.name)}, ensure_ascii=False, indent=2))
            return 0
        if a.preflight:
            print(json.dumps(preflight(ws, a.name), ensure_ascii=False, indent=2))
            return 0
        pf = preflight(ws, a.name)
        if not pf["clean"] and not a.uninstall:
            print(json.dumps({"error": "preflight failed", "issues": pf["issues"]}, ensure_ascii=False))
            return 1
        if a.install:
            return install(ws, a.name, a.nonce or secrets.token_hex(8))
        if a.uninstall:
            return uninstall(ws, a.name)
        print("no action; use --plan/--preflight/--install/--uninstall", file=sys.stderr)
        return 1
    except (ValueError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
