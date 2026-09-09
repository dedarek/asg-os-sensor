# -*- coding: utf-8 -*-
"""指纹匹配器: 新 pid 的实测结构 vs 指纹库.

Stage1 变更(work/discovery-goose-fingerprint):
- match() 改为纯读: 不再修改 match_count/last_seen, 不再隐式保存。
  计数改由显式 record_hit(entry_id) 完成, 由调用方在需要统计时调用。
- 指纹库路径可隔离: ASG_FINGERPRINT_DB 覆盖默认 runtime/fingerprints.json,
  测试/并行观察永不触碰生产库。
- 持久化采用单写者机制: 读-改-写整体持锁(跨进程文件锁 + 进程内线程锁),
  临界区内完成 读 -> 改 -> fsync -> os.replace 原子替换。
  原子替换只防半写文件, 不能代替并发控制; 任何写入必须走 _locked_update。

匹配键(全结构, 无名字无域名): exe basename + runtime + argv flag 交集>=2
  + config_dirs 交集>=1. 命中返回 (entry副本, 耗时ms); 未命中返回 (None, 耗时ms).
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

try:  # POSIX
    import fcntl  # type: ignore

    _POSIX = True
except ImportError:  # pragma: no cover
    _POSIX = False

try:  # Windows
    import msvcrt  # type: ignore

    _WINDOWS = not _POSIX
except ImportError:  # pragma: no cover
    _WINDOWS = False

_DEFAULT_DB = Path(__file__).resolve().parent / "fingerprints.json"
_THREAD_LOCK = threading.Lock()


def db_path() -> Path:
    """指纹库路径; ASG_FINGERPRINT_DB 非空时用于隔离(测试/并存观察)。"""
    override = os.environ.get("ASG_FINGERPRINT_DB", "").strip()
    if override:
        return Path(override)
    return _DEFAULT_DB


class _FileLock:
    """跨进程单写者锁: 锁文件 + fcntl/msvcrt 排他锁。

    与进程内 _THREAD_LOCK 叠加: 线程锁防同进程竞争, 文件锁防跨进程竞争。
    锁文件残留无害(下次进入正常加锁), 不做删除避免解锁竞态。
    """

    def __init__(self, path: Path) -> None:
        self._lock_path = Path(str(path) + ".lock")

    def __enter__(self) -> "_FileLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lock_path, "a+b")
        if _POSIX:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        elif _WINDOWS:
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if _POSIX:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            elif _WINDOWS:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fh.close()


def load() -> dict:
    """只读快照; 返回深拷贝, 调用方改动不会污染库。"""
    with _THREAD_LOCK:
        try:
            raw = db_path().read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return {"fingerprints": [], "version": 1}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"fingerprints": [], "version": 1}
        return deepcopy(data)


def save(db: dict) -> None:
    """外部显式整库保存: 单写者提交(锁 + 原子替换)。仅用于整体重建/测试。"""
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        with _FileLock(path):
            _write_atomic(path, db)


def _write_atomic(path: Path, db: dict) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".fp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(db, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _locked_update(mutator: Callable[[dict], Any]) -> Any:
    """锁内原子读改写: 线程锁 + 跨进程文件锁覆盖整个 读->改->原子替换。

    所有写入路径(remember/record_hit)必须经由此函数, 否则并发会丢更新。
    mutator(db) 就地修改 db 并返回结果; 返回值透传给调用方。
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCK:
        with _FileLock(path):
            try:
                raw = path.read_text(encoding="utf-8")
                db = json.loads(raw)
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                db = {"fingerprints": [], "version": 1}
            result = mutator(db)
            _write_atomic(path, db)
            return result


def features_of(struct: dict) -> dict:
    """提取命令行中具有区分度的结构化模块/入口 (如 -m hermes_cli.main 或 script.js)。

    原生二进制(无脚本入口)允许 entry_token 为空; 匹配不强制 entry_token 存在。
    """
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


def match(struct: dict):
    """纯读匹配: 绝不修改指纹库, 绝不写盘。

    命中返回 (entry深拷贝, 耗时ms); 未命中返回 (None, 耗时ms)。
    统计命中请显式调用 record_hit(entry["id"])。
    """
    t0 = time.time()
    db = load()
    f = features_of(struct)

    for e in db.get("fingerprints", []):
        ef = e.get("features", {})
        if ef.get("exe") != f["exe"] or ef.get("runtime") != f["runtime"]:
            continue

        # 1. 相同核心入口模块/脚本 (如都执行 -m hermes_cli.main): 同类 Agent。
        if f.get("entry_token") and ef.get("entry_token") == f["entry_token"]:
            return deepcopy(e), int((time.time() - t0) * 1000)

        fover = len(set(ef.get("flags", [])) & set(f["flags"]))
        cover = len(set(ef.get("config_dirs", [])) & set(f["config_dirs"]))

        # 2. 脚本型/原生 Agent: 配置目录命中 + flags 兼容, 或两边都无 flags 时以结构吻合为准。
        if ef.get("config_dirs") or f["config_dirs"]:
            if cover >= 1 and (fover >= 1 or len(f.get("flags", [])) == 0):
                pass
            elif cover < 1 or fover < 2:
                continue
        elif fover < 1 and not (len(ef.get("flags", [])) == 0 and len(f.get("flags", [])) == 0):
            continue
        return deepcopy(e), int((time.time() - t0) * 1000)
    return None, int((time.time() - t0) * 1000)


def record_hit(entry_id: str) -> int:
    """显式命中计数: 锁内读改写后原子落盘, 返回新计数。

    这是 match() 副作用的唯一去处; 不调用则不产生任何写。
    未找到 entry_id 返回 0 且不写盘。
    """

    def _mutate(db: dict) -> int:
        for e in db.get("fingerprints", []):
            if e.get("id") == entry_id:
                count = int(e.get("match_count", 0)) + 1
                e["match_count"] = count
                e["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                return count
        return 0

    if not entry_id:
        return 0
    return _locked_update(_mutate)


def remember(struct: dict, recipe: dict, mount_ms: int) -> dict:
    """写入路径(仅 Supervisor/分析完成后调用): 新增或演进已有指纹。

    返回新条目深拷贝; 不修改传入对象。锁内原子读改写, 并发安全。
    """
    agent_name = recipe.get("agent_identity_name") or recipe.get("agent_name")
    if not agent_name or agent_name == "unknown-runtime":
        agent_name = struct.get("runtime_class", "unknown-runtime")

    f = features_of(struct)

    evolves_target = ""
    mf = recipe.get("match_features", {})
    if isinstance(mf, dict):
        evolves_target = mf.get("evolves_prior_harness", "")

    def _mutate(db: dict) -> dict:
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
                return deepcopy(existing)

        entry = {
            "id": f"harness-{len(db.get('fingerprints', [])) + 1:02d}",
            "name": agent_name,
            "first_seen": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "match_count": 1,
            "mount_ms": mount_ms,
            "features": f,
            "children_classes": struct.get("children_classes", []),
            "hook_recipe": recipe,
            "observed_exe_full": struct.get("exe_full", ""),
        }
        db.setdefault("fingerprints", []).append(entry)
        return deepcopy(entry)

    return _locked_update(_mutate)

