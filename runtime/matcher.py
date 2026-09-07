"""指纹匹配器: 新 pid 的实测结构 vs 指纹库, 命中则秒挂(跳过分析).

匹配键(全结构, 无名字无域名): exe basename + runtime + argv flag 交集>=2
  + config_dirs 交集>=1. 命中返回 (entry, 耗时ms); 未命中返回 (None, 耗时ms).
save(entry): 新增或 match_count+1.
"""
import json
import time
from pathlib import Path

DB = Path(__file__).resolve().parent / "fingerprints.json"


def load():
    try:
        return json.loads(DB.read_text(encoding="utf-8"))
    except Exception:
        return {"fingerprints": [], "version": 1}


def save(db):
    DB.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


def features_of(struct):
    return {"exe": struct.get("exe", ""),
            "runtime": struct.get("runtime", ""),
            "flags": sorted({a for a in struct.get("argv_shape", [])
                             if isinstance(a, str) and a.startswith("-")}),
            "config_dirs": sorted(struct.get("config_dirs", []))}


def match(struct):
    t0 = time.time()
    db = load()
    f = features_of(struct)
    
    # 提取脚本与包特征
    argv_str = " ".join([str(x) for x in struct.get("argv_shape", [])]).lower()
    exe_str = str(struct.get("exe_full", "")).lower()

    for e in db.get("fingerprints", []):
        ef = e.get("features", {})
        
        # 1. 如果有明确的已知 Agent 命名标识匹配
        fp_name = (e.get("name") or "").lower()
        if fp_name and fp_name != "unknown-runtime":
            if fp_name in argv_str or fp_name in exe_str or fp_name == f["exe"].lower():
                e["match_count"] = int(e.get("match_count", 0)) + 1
                e["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                save(db)
                return e, int((time.time() - t0) * 1000)

        # 2. 通用结构匹配规则
        if ef.get("exe") != f["exe"] or ef.get("runtime") != f["runtime"]:
            continue
        fover = len(set(ef.get("flags", [])) & set(f["flags"]))
        cover = len(set(ef.get("config_dirs", [])) & set(f["config_dirs"]))
        # config 门: 至少一边观测到配置目录时要求交集>=1;
        # 两边都没观测到时, flag 交集提到>=3 补偿精度 (门槛经实测校准).
        if ef.get("config_dirs") or f["config_dirs"]:
            if cover < 1 or fover < 2:
                continue
        elif fover < 3:
            continue
        e["match_count"] = int(e.get("match_count", 0)) + 1
        e["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save(db)
        return e, int((time.time() - t0) * 1000)
    return None, int((time.time() - t0) * 1000)


def remember(struct, recipe, mount_ms):
    db = load()
    # 提取有意义的 Agent 名称 (优先从 recipe 中的 agent_identity_name，其次从 struct)
    agent_name = recipe.get("agent_identity_name") or recipe.get("agent_name")
    if not agent_name or agent_name == "unknown-runtime":
        # 尝试从 exe_full 或 cmdline 推断包名
        cmd_str = " ".join(struct.get("argv_shape", []))
        exe_full = struct.get("exe_full", "")
        for token in ("pi-coding-agent", "piagent", "claude-code", "codex", "opencode", "goose"):
            if token in cmd_str.lower() or token in exe_full.lower():
                agent_name = token
                break
    if not agent_name:
        agent_name = struct.get("runtime_class", "unknown-runtime")

    entry = {"id": f"harness-{len(db.get('fingerprints', [])) + 1:02d}",
             "name": agent_name,
             "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "match_count": 1, "mount_ms": mount_ms,
             "features": features_of(struct),
             "children_classes": struct.get("children_classes", []),
             "hook_recipe": recipe,
             "observed_exe_full": struct.get("exe_full", "")}
    db.setdefault("fingerprints", []).append(entry)
    save(db)
    return entry
