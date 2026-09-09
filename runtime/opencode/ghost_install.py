# -*- coding: utf-8 -*-
"""ASG OpenCode 观测插件：事务化隔离安装 / dry-run / 卸载回滚。

只针对独立测试工作区目录：不触碰用户全局配置、现有项目，不打开 UI，不重启/终止
现有 Agent。安装采用独立 run 目录 + manifest 原子发布：
  .asg-observe/manifest.json   <- 当前 active run（原子替换发布）
  .asg-observe/runs/<runid>/   <- 每轮安装的 nonce(0600) 与 events.jsonl(0600)
支持 install -> uninstall -> reinstall；任一步失败回滚本 run（无残留有效插件）。

安全约束（评审要求）：
- ROOT 通过查找含 runtime/opencode/asg-observe.js 的仓库根定位，不限 cwd。
- 仅接受授权隔离根（系统临时目录或仓库 artifacts/stage1/**）；workspace 及其全部
  路径组件均拒绝 symlink；ws 不 resolve 隐藏到隔离区外。
- name 纯文件名（拒绝绝对路径/../）；插件文件 O_CREAT|O_EXCL 原子独占创建。
- state 目录 0700、manifest 0600；拒绝已有无归属 state；卸载同样验证路径。
"""
import argparse
import hashlib
import json
import os
import secrets
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PLUGIN_NAME = "asg-observe.js"
STATEDIR_NAME = ".asg-observe"


def repo_root() -> Path:
    here = Path(__file__).resolve()
    for anc in [here] + list(here.parents):
        if (anc / "runtime" / "opencode" / PLUGIN_NAME).exists():
            return anc
    raise SystemExit("cannot locate repo root (runtime/opencode/%s not found)" % PLUGIN_NAME)


def plugin_source() -> Path:
    return repo_root() / "runtime" / "opencode" / PLUGIN_NAME


def log(msg):
    print(msg, file=sys.stderr)


def sanitize_name(name: str) -> str:
    base = os.path.basename(name)
    if not name or name != base:
        raise ValueError("plugin name must be a bare filename")
    if base in ("", ".", "..") or "/" in base or chr(92) in base:
        raise ValueError("plugin name must be a bare filename")
    return base


def authorized_roots() -> list:
    roots = [Path(tempfile.gettempdir()).resolve()]
    roots.append((repo_root() / "artifacts" / "stage1").resolve())
    return roots


def check_no_symlink(p: Path, label: str):
    try:
        st = p.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(st.st_mode):
        raise ValueError("%s must not be a symlink: %s" % (label, p))


def check_path_components(p: Path, label: str):
    """校验路径全部组件非 symlink（防中间目录链向隔离区外）。"""
    cur = Path(p.anchor) if p.anchor else Path("/")
    for part in p.parts[1:]:
        cur = cur / part
        check_no_symlink(cur, label)
    check_no_symlink(p, label)


def ensure_workspace(ws: Path) -> Path:
    ws = ws.resolve()
    ok = any(str(ws) == str(r) or str(ws).startswith(str(r) + os.sep) for r in authorized_roots())
    if not ok:
        raise ValueError("workspace outside authorized isolation roots: %s" % ws)
    if not ws.exists() or not ws.is_dir():
        raise ValueError("workspace missing/not a dir: %s" % ws)
    check_path_components(ws, "workspace")
    return ws


def check_state_dir(ws: Path) -> Path:
    sd = ws / ".opencode" / "plugins" / STATEDIR_NAME
    if sd.exists():
        mp = sd / "manifest.json"
        inactive = sd / "manifest.json.inactive"
        # 本工具卸载时保留 inactive manifest 作为历史；允许重装
        if not mp.exists() and not inactive.exists():
            raise ValueError("existing unowned state dir without manifest: %s" % sd)
        if not mp.exists() and inactive.exists():
            # 旧 inactive 应用于旧 run；重装将发布新 manifest
            pass
    return sd


def manifest_path(sd: Path) -> Path:
    return sd / "manifest.json"


def read_manifest(sd: Path):
    mp = manifest_path(sd)
    if not mp.exists():
        return None
    m = json.loads(mp.read_text())
    return m if m.get("active") else None


def plan(ws: Path, name: str):
    return [
        "1. workspace=%s (isolated; authorized root check)" % ws.resolve(),
        "2. engine plugin glob {plugin,plugins}/*.{ts,js} scans cwd=config dir",
        "3. create .asg-observe/runs/<runid>/{nonce,events.jsonl} (0600, dir 0700)",
        "4. copy plugin into workspace/.opencode/plugins/%s (O_EXCL)" % name,
        "5. atomically publish .asg-observe/manifest.json (active run)",
        "6. user opens workspace in Desktop UI (cannot be automated)",
        "7. engine loads plugin -> hook.loaded; receiver validates nonce+PID+create_time",
    ]


def preflight(ws: Path, name: str):
    issues = []
    try:
        ensure_workspace(ws)
        check_state_dir(ws)
    except ValueError as e:
        issues.append(str(e))
    return {"workspace": str(ws.resolve()), "plugin": str(plugin_source()), "name": name,
            "clean": len(issues) == 0, "issues": issues,
            "limits": ["global config still searched by engine",
                        "engine installs @opencode-ai/plugin dep into workspace dir (network)",
                        "plugin loads only when workspace opened in Desktop UI"]}


def install(ws: Path, name: str, nonce: str):
    ws = ensure_workspace(ws)
    name = sanitize_name(name)
    src = plugin_source()
    pd = ws / ".opencode" / "plugins"
    check_path_components(pd, "plugins dir")
    pd.mkdir(parents=True, exist_ok=True)
    sd = check_state_dir(ws)
    sd.mkdir(parents=True, exist_ok=True)
    os.chmod(sd, 0o700)
    runid = secrets.token_hex(8)
    run_dir = sd / "runs" / runid
    created = []
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(run_dir, 0o700)
        created.append(run_dir)
        nf = run_dir / "nonce"
        fd = os.open(str(nf), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, (nonce + chr(10)).encode()); os.close(fd)
        created.append(nf)
        ef = run_dir / "events.jsonl"
        fd = os.open(str(ef), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, b""); os.close(fd)
        created.append(ef)
        data = src.read_bytes()
        dest = pd / name
        if dest.exists():
            raise ValueError("plugin already exists (uninstall first): %s" % dest)
        fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, data); os.close(fd)
        created.append(dest)
        sha = hashlib.sha256(data).hexdigest()
        man = {"manifest_version": 1, "name": name, "runid": runid, "active": True,
                "sha256": sha, "nonce": nonce, "workspace": str(ws),
                "installed_at": datetime.now(timezone.utc).isoformat()}
        mp = manifest_path(sd)
        tmp = mp.with_suffix(".tmp")
        tmp.write_text(json.dumps(man, indent=2, ensure_ascii=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, mp)
        created.append(mp)
    except (ValueError, OSError) as e:
        for item in reversed(created):
            try:
                if item.is_dir() and not item.is_symlink():
                    item.rmdir()
                else:
                    item.unlink()
            except OSError:
                pass
        raise
    print(json.dumps({"installed": str(dest), "runid": runid, "sha256": sha,
                      "nonce": nonce, "events_file": str(ef),
                      "next": "open workspace in Desktop UI to load plugin"}, ensure_ascii=False))
    return 0


def uninstall(ws: Path, name: str):
    ws = ensure_workspace(ws)
    name = sanitize_name(name)
    pd = ws / ".opencode" / "plugins"
    check_path_components(pd, "plugins dir")
    sd = check_state_dir(ws)
    mp = manifest_path(sd)
    if not mp.exists():
        raise ValueError("refusing uninstall without manifest")
    man = json.loads(mp.read_text())
    if man.get("name") != name or man.get("workspace") != str(ws):
        raise ValueError("manifest does not match install (name/workspace)")
    dest = pd / name
    if not dest.exists():
        raise ValueError("plugin file missing: %s" % dest)
    if hashlib.sha256(dest.read_bytes()).hexdigest() != man.get("sha256"):
        raise ValueError("plugin file modified since install; refusing delete (preserve user files)")
    runid = man.get("runid")
    os.replace(mp, mp.with_name("manifest.json.inactive"))
    nf = sd / "runs" / runid / "nonce"
    if nf.exists():
        nf.unlink()
    dest.unlink()
    print(json.dumps({"uninstalled": str(dest), "runid": runid,
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