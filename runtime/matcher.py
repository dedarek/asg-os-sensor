"""指纹匹配器: 新 pid 的实测结构 vs 指纹库, 命中则秒挂(跳过分析).

匹配键(全结构, 无名字无域名): exe basename + runtime + argv flag 交集>=2
  + config_dirs 交集>=1. 命中返回 (entry, 耗时ms); 未命中返回 (None, 耗时ms).
save(entry): 新增或 match_count+1.
"""
import json
import time
from pathlib import Path

import threading

DB = Path(__file__).resolve().parent / "fingerprints.json"
FILE_LOCK = threading.Lock()


def load():
    with FILE_LOCK:
        try:
            return json.loads(DB.read_text(encoding="utf-8"))
        except Exception:
            return {"fingerprints": [], "version": 1}


def save(db):
    with FILE_LOCK:
        # Windows 上直接写，配合锁保护
        DB.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


def features_of(struct):
    # 提取命令行中具有区分度的结构化模块/入口 (如 -m hermes_cli.main 或 script.js)
    argv = struct.get("argv_shape", [])
    entry_token = ""
    for idx, arg in enumerate(argv):
        if arg == "-m" and idx + 1 < len(argv):
            entry_token = str(argv[idx + 1])
            break
        elif isinstance(arg, str) and (arg.endswith(".js") or arg.endswith(".mjs") or arg.endswith(".py")):
            entry_token = arg
            break

    return {
        "exe": struct.get("exe", ""),
        "runtime": struct.get("runtime", ""),
        "entry_token": entry_token,
        "flags": sorted({a for a in argv if isinstance(a, str) and a.startswith("-")}),
        "config_dirs": sorted(struct.get("config_dirs", []))
    }


def match(struct):
    t0 = time.time()
    db = load()
    f = features_of(struct)

    for e in db.get("fingerprints", []):
        ef = e.get("features", {})
        if ef.get("exe") != f["exe"] or ef.get("runtime") != f["runtime"]:
            continue

        # 1. 如果两者拥有相同的核心入口模块/脚本 (如都执行 -m hermes_cli.main)，直接判定为同类 Agent！
        if f.get("entry_token") and ef.get("entry_token") == f["entry_token"]:
            e["match_count"] = int(e.get("match_count", 0)) + 1
            e["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            save(db)
            return e, int((time.time() - t0) * 1000)

        fover = len(set(ef.get("flags", [])) & set(f["flags"]))
        cover = len(set(ef.get("config_dirs", [])) & set(f["config_dirs"]))

        # 针对无 flag 或 flag 较少的脚本型 Agent (如 node start.mjs)：
        # 若配置目录命中，或两边都没 flags 且关键特征吻合，直接放行
        if ef.get("config_dirs") or f["config_dirs"]:
            if cover >= 1 and (fover >= 1 or len(f.get("flags", [])) == 0):
                pass
            elif cover < 1 or fover < 2:
                continue
        elif fover < 1 and not (len(ef.get("flags", [])) == 0 and len(f.get("flags", [])) == 0):
            continue
        e["match_count"] = int(e.get("match_count", 0)) + 1
        e["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save(db)
        return e, int((time.time() - t0) * 1000)
    return None, int((time.time() - t0) * 1000)


def remember(struct, recipe, mount_ms):
    db = load()
    agent_name = recipe.get("agent_identity_name") or recipe.get("agent_name")
    if not agent_name or agent_name == "unknown-runtime":
        agent_name = struct.get("runtime_class", "unknown-runtime")

    f = features_of(struct)

    # 1. 优先遵循 Goose 显式指定的演进目标 (如果 Goose 发现这是对已有 harness 的演进更新)
    evolves_target = ""
    mf = recipe.get("match_features", {})
    if isinstance(mf, dict):
        evolves_target = mf.get("evolves_prior_harness", "")

    for existing in db.get("fingerprints", []):
        is_targeted = evolves_target and existing.get("id") == evolves_target
        ef = existing.get("features", {})
        is_same_harness = (
            (ef.get("exe") == f["exe"] and ef.get("runtime") == f["runtime"] and ef.get("config_dirs") == f["config_dirs"])
            or (f.get("entry_token") and ef.get("entry_token") == f.get("entry_token"))
        )

        if is_targeted or is_same_harness:
            existing["match_count"] = int(existing.get("match_count", 1)) + 1
            existing["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
            existing["mount_ms"] = mount_ms
            if agent_name and agent_name not in ["unknown", "unknown-runtime", "unidentified-agent"]:
                existing["name"] = agent_name
            existing["hook_recipe"] = recipe
            save(db)
            return existing

    entry = {"id": f"harness-{len(db.get('fingerprints', [])) + 1:02d}",
             "name": agent_name,
             "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "match_count": 1, "mount_ms": mount_ms,
             "features": f,
             "children_classes": struct.get("children_classes", []),
             "hook_recipe": recipe,
             "observed_exe_full": struct.get("exe_full", "")}
    db.setdefault("fingerprints", []).append(entry)
    save(db)
    return entry
