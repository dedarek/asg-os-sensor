"""结构发现器: 给定 pid, 实测该进程的全部相关结构, 不查任何先验名单.

产出(全是观测值, 含首次发现的未知 harness 名字与路径 —— 这是发现, 不是先验):
  exe / argv_shape(flag 保留、值掩码) / parent链(3代basename) / cwd /
  config_dirs(家目录下点目录) / children_classes(通用类别) /
  listen(bool) / runtime 猜测 / hook_recipe 选择
"""
import os
import time
from pathlib import Path

import psutil

HOME = str(Path.home()).lower()


def base(p):
    try:
        return os.path.basename(p).lower()
    except Exception:
        return "?"


def depth(proc):
    """迭代版进程树深度, 异常一律吞."""
    best, seen, stack = 0, set(), [(proc, 0)]
    while stack:
        cur, lv = stack.pop()
        try:
            kids = cur.children()
        except Exception:
            continue
        if not kids:
            best = max(best, lv)
            continue
        best = max(best, lv + 1)
        if lv + 1 >= 3:
            continue
        for k in kids:
            try:
                if k.pid in seen:
                    continue
                seen.add(k.pid)
                stack.append((k, lv + 1))
            except Exception:
                continue
    return best


def resolve(pid):
    """壳下钻: cmd/bash/sh/powershell 只是启动壳, 真正的 harness 是有分量的子进程.
    返回 (main_pid, tree_pids). 无壳直跑则返回自身."""
    try:
        p = psutil.Process(pid)
        exe = base(p.exe())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return pid, [pid]
    if exe not in ("cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh",
                   "conhost.exe"):
        return pid, [pid]
    tree = [pid]
    cands = []
    try:
        for k in p.children(recursive=True):
            tree.append(k.pid)
            try:
                kb = base(k.exe())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if kb in ("conhost.exe",):
                continue
            try:
                rss = k.memory_info().rss
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                rss = 0
            cands.append((rss, k.pid))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    if not cands:
        return pid, tree
    cands.sort(reverse=True)
    return cands[0][1], tree


def analyze(pid):
    t0 = time.time()
    p = psutil.Process(pid)
    try:
        exe = p.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        exe = ""
    try:
        cmd = p.cmdline()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        cmd = []
    
    # 抽象命令行形状，但对脚本/分发入口保留语义标识以供指纹与名称关联
    argv_shape = []
    for a in cmd[1:7]:
        if a.startswith("-"):
            argv_shape.append(a)
        elif any(ext in a.lower() for ext in [".js", ".py", ".ts", "dist", "bundle", "cli", "agent"]):
            p_name = Path(a).name
            for token in ("pi-coding-agent", "piagent", "claude-code", "codex", "opencode", "goose"):
                if token in a.lower():
                    p_name = f"<{token}:{p_name}>"
                    break
            argv_shape.append(p_name)
        else:
            argv_shape.append("<val>")
    parents = []
    try:
        q = p
        for _ in range(3):
            q = q.parent()
            if q is None:
                break
            parents.append(base(q.exe() if hasattr(q, "exe") else q.name()))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    try:
        cwd = p.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        cwd = ""
    config_dirs = set()
    try:
        for f in p.open_files():
            fp = f.path.lower()
            if fp.startswith(HOME):
                rel = os.path.relpath(fp, Path.home())
                top = rel.split(os.sep)[0]
                if top.startswith("."):
                    config_dirs.add(top)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    children = set()
    try:
        for k in p.children(recursive=True):
            try:
                children.add(base(k.exe()))
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                try:
                    children.add((k.name() or "").lower())
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    listen = False
    try:
        for c in p.net_connections(kind="inet"):
            if c.status == "LISTEN":
                listen = True
                break
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    eb = base(exe)
    if "node" in eb:
        runtime, recipe = "node", "node-preload-https"
    elif "python" in eb:
        runtime, recipe = "python", "sitecustomize-sdk"
    else:
        runtime, recipe = "native", "sandbox-or-uprobe"
    return {"pid": pid, "exe": eb,
            "exe_full": exe, "argv_shape": argv_shape,
            "parents": parents, "cwd": cwd,
            "config_dirs": sorted(config_dirs),
            "children_classes": sorted(children),
            "listen": listen, "runtime": runtime,
            "hook_recipe": recipe,
            "analyze_ms": int((time.time() - t0) * 1000)}
