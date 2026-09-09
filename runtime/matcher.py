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
_NOCHANGE = object()  # mutator 未修改库的哨兵: 命中时不落盘
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
        except FileNotFoundError:
            return {"fingerprints": [], "version": 1}
        # 损坏或读取失败必须报错并保留原文件, 禁止静默当作空库后被覆盖。
        data = json.loads(raw)
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
            except FileNotFoundError:
                db = {"fingerprints": [], "version": 1}
            # 损坏/读取失败: 报错且不落盘, 保留原文件。
            result = mutator(db)
            if result is not _NOCHANGE:
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
    """Compatibility wrapper: only exact may be reused; similar is available via classify."""
    result = classify(struct)
    return (result['entry'] if result['status'] == 'exact' else None), result['match_ms']


def record_hit(entry_id: str) -> int:
    """显式命中计数: 锁内读改写后原子落盘, 返回新计数。

    这是 match() 副作用的唯一去处; 不调用则不产生任何写。
    未找到 entry_id 返回 0 且不写盘。
    """

    def _mutate(db: dict):
        for e in db.get("fingerprints", []):
            if e.get("id") == entry_id:
                count = int(e.get("match_count", 0)) + 1
                e["match_count"] = count
                e["last_seen"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                return count
        return _NOCHANGE  # 未命中: 不产生任何磁盘写

    if not entry_id:
        return 0
    result = _locked_update(_mutate)
    return 0 if result is _NOCHANGE else result


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



def classify(struct: dict) -> dict:
    """Pure read: legacy fingerprints are references, never compatible proof.

    exact 只证明「可执行文件内容、入口（脚本/模块）、运行时/平台、启动参数与工作目录」
    在本次观测中未变 —— 配置内容、依赖版本、插件/MCP 集合、模型路由等变更不在覆盖范围内，
    因此 exact 命中不代表 Hook 已安装、已生效或防线健全。
    """
    start = time.monotonic()
    f = features_of(struct)
    compatible = struct.get('compatibility')
    similar = None
    EXACT_BOUNDS = [
        'covered: executable content, entry content (script/module digest), runtime, platform, launch argv+cwd',
        'not covered: config file contents, dependency versions, MCP/plugin/marketplace sets, model routing, network behavior',
        'exact reuse only provides a recipe for investigation; hook install and effectiveness remain unverified',
    ]
    for entry in load().get('fingerprints', []):
        ef = entry.get('features', {})
        same_runtime = f['exe'] and ef.get('exe') == f['exe'] and ef.get('runtime') == f['runtime']
        if same_runtime and compatible and entry.get('investigation_verified') is True:
            for revision in reversed(entry.get('revisions', [])):
                if revision.get('compatibility') == compatible:
                    return {'status': 'exact', 'entry': deepcopy(dict(entry, hook_recipe=revision['recipe'], revision=revision['revision'])),
                            'bounds': EXACT_BOUNDS,
                            'reason': 'Observed build and launch constraints unchanged; Hook still unverified',
                            'match_ms': int((time.monotonic()-start)*1000)}
        if f['exe'] and ef.get('exe') == f['exe'] and ef.get('runtime') == f['runtime']:
            similar = deepcopy(entry)
    return {'status': 'similar' if similar else 'miss', 'entry': similar,
            'reason': 'Historical reference only' if similar else 'No family reference',
            'match_ms': int((time.monotonic()-start)*1000)}


def _family_identity_matches(observed, prior) -> bool:
    """同一家族证据：同一可执行内容（解释器/二进制）+ 入口身份一致。

    仅凭 exe basename + runtime 相同不足（node/python 等共享运行时广泛存在）；相似命中
    只是调查参考，升级/演进必须由本函数用 digest 证据裁断，禁止凭模型提供的旧 id 直接合并。
    prior 取自目标库条目最近一次 revision 的 compatibility（含 digest）。
    """
    if not isinstance(observed, dict) or not isinstance(prior, dict):
        return False
    o_exe = observed.get('executable'); p_exe = prior.get('executable')
    if not o_exe or not p_exe or o_exe != p_exe:
        return False  # 解释器/可执行文件内容不同 → 不是同一家族
    o_entry = observed.get('entry'); p_entry = prior.get('entry')
    if o_entry and p_entry and o_entry == p_entry:
        return True  # 入口文件内容一致
    o_path = observed.get('entry_path'); p_path = prior.get('entry_path')
    if o_path and p_path and o_path == p_path:
        return True  # 入口路径一致（内容演进后仍同属一族）
    return False


def remember_verified(struct, recipe, evidence, mount_ms=0):
    """Supervisor-only entry: evidence validated before a revision may be reusable.

    演进（evolves_prior_harness）必须通过入口/包身份证据门禁：先比较可执行文件 digest
    与入口身份（entry digest 或 entry_path），再决定是否挂到既有家族；证据不足时拒绝
    合并并明确报错，调用方应当新建家族或重新调查。
    """
    if not struct.get('compatibility') or not evidence:
        raise ValueError('Observed compatibility and MCP evidence required')
    if not isinstance(recipe.get('hook'), dict) or not recipe.get('agent_identity_name'):
        raise ValueError('Invalid investigation recipe')
    f = features_of(struct)

    def mutate(db):
        target = recipe.get('match_features', {}).get('evolves_prior_harness')
        entry = next((e for e in db.get('fingerprints', []) if e['id'] == target), None) if target else None
        if target and entry is not None:
            prior_compat = None
            for rev in reversed(entry.get('revisions') or []):
                if rev.get('compatibility') is not None:
                    prior_compat = rev['compatibility']
                    break
            if prior_compat is None:
                prior_compat = entry.get('compatibility') or {}
            if not _family_identity_matches(struct.get('compatibility') or {}, prior_compat or {}):
                raise ValueError('Evolution target lacks family identity evidence (exe/entry/package); similar is reference only, do not merge')
        elif target and entry is None:
            raise ValueError('Evolution target not found in fingerprint DB')
        if entry is None:
            entry = {'id': 'harness-' + __import__('uuid').uuid4().hex[:12], 'features': f,
                     'first_seen': time.strftime('%Y-%m-%dT%H:%M:%S'), 'revisions': [], 'match_count': 0}
            db.setdefault('fingerprints', []).append(entry)
        if not entry.get('revisions') and entry.get('hook_recipe'):
            entry['revisions'] = [{'revision': 0, 'recipe': deepcopy(entry['hook_recipe']), 'compatibility': None,
                                   'evidence': [], 'status': 'legacy-unverified'}]
        revision = max((r['revision'] for r in entry['revisions']), default=0) + 1
        entry['revisions'].append({'revision': revision, 'recipe': deepcopy(recipe),
                                  'compatibility': deepcopy(struct['compatibility']), 'evidence': deepcopy(evidence),
                                  'status': 'recipe_validated_hook_unverified'})
        entry.update(name=recipe['agent_identity_name'], hook_recipe=deepcopy(recipe), revision=revision,
                     investigation_verified=True, hook_verified=False, mount_ms=mount_ms)
        return deepcopy(entry)

    return _locked_update(mutate)

