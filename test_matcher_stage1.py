# -*- coding: utf-8 -*-
"""Stage1 matcher 回归测试(新增, 区别于原有 test_discovery.py 8 用例).

覆盖:
- 指纹库隔离配置 (ASG_FINGERPRINT_DB)
- match() 纯读: 调用前后库文件逐字节不变, 不隐式计数
- load() 返回深拷贝, 调用方篡改不污染库
- remember() 新增/演进语义 + 返回副本
- 原生二进制(无脚本 entry_token)可存可匹配
- record_hit() 显式计数; 并发(线程)计数不丢
- 跨进程并发写入(多进程 remember)不丢失、无半写(文件锁 + 原子替换)
"""
import json
import multiprocessing
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runtime import matcher  # noqa: E402

NATIVE_STRUCT = {
    "exe": "nativesrv",
    "runtime": "native",
    "argv_shape": ["nativesrv", "--serve", "--port", "9000", "--json"],
    "config_dirs": ["/tmp/nativesrv-cfg"],
    "exe_full": "/usr/local/bin/nativesrv",
    "children_classes": ["worker"],
}


def _recipe(name):
    return {
        "agent_identity_name": name,
        "match_features": {"evolves_prior_harness": ""},
        "observation": "test",
        "hook": {"type": "stream-tap"},
        "fallback": "none",
    }


def _worker_remember(name, distinct=False):
    """跨进程测试用的模块级工作函数 (multiprocessing fork 子进程执行)。"""
    struct = dict(NATIVE_STRUCT)
    if distinct:
        struct = dict(struct)
        struct["exe"] = f"nativesrv-{name}"
        struct["argv_shape"] = [struct["exe"], "--serve", "--port", "9000", "--json"]
    matcher.remember(struct, _recipe(name), 5)


def _worker_remember_spawn(name):
    """spawn 跨进程工作函数(模块级, 可 pickle)。不同进程各自建独立指纹。"""
    struct = dict(NATIVE_STRUCT)
    struct["exe"] = "nativesrv-" + str(name)
    struct["argv_shape"] = [struct["exe"], "--serve", "--port", "9000", "--json"]
    matcher.remember(struct, _recipe(name), 5)


def _mcp_get_prior(env: dict):
    """真实脚本路径启动 analyst_tools 并调用 MCP get_prior_recipe。

    返回 {"returncode", "result": dict, "stderr"}。result 为 MCP 工具返回的
    content[0].text 解析后的 dict (含 path/value)。
    """
    root = Path(__file__).resolve().parent
    isolated = tempfile.TemporaryDirectory()
    env = dict(env, ASG_AUDIT_DIR=str(Path(isolated.name) / 'audit'),
               ASG_RECIPE_DIR=str(Path(isolated.name) / 'recipes'))
    proc = subprocess.Popen(
        [sys.executable, "-B", "runtime/analyst_tools.py"],
        cwd=str(root), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    req = (
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2024-11-05", "capabilities": {}}})
        + "\n"
        + json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                      "params": {"name": "get_prior_recipe", "arguments": {}}})
        + "\n"
    )
    out, err = proc.communicate(req, timeout=30)
    result = {"returncode": proc.returncode, "result": None, "stderr": err}
    for line in (out or "").splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("id") == 2 and "error" in msg:
            result["error"] = msg["error"]
        if msg.get("id") == 2 and "result" in msg:
            content = (msg["result"].get("content") or [])
            if content:
                try:
                    result["result"] = json.loads(content[0].get("text", ""))
                except (json.JSONDecodeError, TypeError):
                    result["result"] = content[0].get("text")
    isolated.cleanup()
    return result


class MatcherStage1Tests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = os.environ.get("ASG_FINGERPRINT_DB")
        os.environ["ASG_FINGERPRINT_DB"] = str(Path(self._tmp.name) / "fp.json")

    def tearDown(self):
        if self._orig_db is None:
            os.environ.pop("ASG_FINGERPRINT_DB", None)
        else:
            os.environ["ASG_FINGERPRINT_DB"] = self._orig_db
        self._tmp.cleanup()

    def test_db_path_env_isolation(self):
        os.environ["ASG_FINGERPRINT_DB"] = "/tmp/iso-fp.json"
        self.assertEqual(matcher.db_path(), Path("/tmp/iso-fp.json"))
        os.environ.pop("ASG_FINGERPRINT_DB", None)
        self.assertEqual(matcher.db_path(), matcher._DEFAULT_DB)

    def test_match_is_pure_read(self):
        db = {
            "version": 1,
            "fingerprints": [
                {
                    "id": "harness-01", "name": "nativesrv", "match_count": 7,
                    "features": matcher.features_of(NATIVE_STRUCT),
                    "hook_recipe": _recipe("nativesrv"),
                }
            ],
        }
        matcher.save(db)
        path = matcher.db_path()
        before = path.read_bytes()

        entry, ms = matcher.match(NATIVE_STRUCT)
        self.assertIsNone(entry)  # Legacy fixture is a reference, not reusable proof.
        self.assertEqual(matcher.classify(NATIVE_STRUCT)['status'], 'similar')
        # 纯读: 文件逐字节不变, 计数不隐式增长
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(matcher.load()["fingerprints"][0]["match_count"], 7)

        # 未命中也不落盘
        miss = dict(NATIVE_STRUCT)
        miss["flags"] = []
        miss["config_dirs"] = []
        miss["argv_shape"] = ["nativesrv", "--weird-flag"]
        entry2, _ = matcher.match(miss)
        if entry2 is not None:
            self.fail("expected miss")
        self.assertEqual(path.read_bytes(), before)

    def test_load_returns_deepcopy(self):
        matcher.save({"version": 1, "fingerprints": [{"id": "x", "match_count": 1}]})
        loaded = matcher.load()
        loaded["fingerprints"][0]["match_count"] = 999
        self.assertEqual(matcher.load()["fingerprints"][0]["match_count"], 1)

    def test_remember_new_and_evolve(self):
        entry = matcher.remember(NATIVE_STRUCT, _recipe("nativesrv"), 12)
        self.assertEqual(entry["id"], "harness-01")
        self.assertEqual(entry["match_count"], 1)
        # 同特征再演进: count+1, name 更新
        entry2 = matcher.remember(NATIVE_STRUCT, _recipe("nativesrv-v2"), 20)
        self.assertEqual(entry2["id"], "harness-01")
        self.assertEqual(entry2["match_count"], 2)
        self.assertEqual(entry2["name"], "nativesrv-v2")
        self.assertEqual(len(matcher.load()["fingerprints"]), 1)
        # 返回副本: 篡改返回值不影响库
        entry2["match_count"] = 0
        self.assertEqual(matcher.load()["fingerprints"][0]["match_count"], 2)

    def test_native_binary_without_entry_token(self):
        # 原生二进制无脚本入口, 不应强制 entry_token
        matcher.remember(NATIVE_STRUCT, _recipe("nativesrv"), 8)
        hit, _ = matcher.match(NATIVE_STRUCT)
        self.assertIsNone(hit)  # Native exact proof is covered in test_goose_stage1.
        self.assertEqual(matcher.classify(NATIVE_STRUCT)['status'], 'similar')

    def test_record_hit_explicit_and_match_stays_clean(self):
        matcher.remember(NATIVE_STRUCT, _recipe("nativesrv"), 8)
        matcher.match(NATIVE_STRUCT)
        matcher.match(NATIVE_STRUCT)
        # match() 不计数
        self.assertEqual(matcher.load()["fingerprints"][0]["match_count"], 1)
        # record_hit 显式计数
        self.assertEqual(matcher.record_hit("harness-01"), 2)
        self.assertEqual(matcher.record_hit("harness-01"), 3)
        self.assertEqual(matcher.load()["fingerprints"][0]["match_count"], 3)
        # 未知 id 返回 0 且不写盘
        before = matcher.db_path().read_bytes()
        self.assertEqual(matcher.record_hit("harness-999"), 0)
        self.assertEqual(matcher.db_path().read_bytes(), before)

    def test_concurrent_thread_record_hit_no_lost_update(self):
        matcher.remember(NATIVE_STRUCT, _recipe("nativesrv"), 8)
        n_threads, per = 4, 25
        errors = []

        def worker():
            try:
                for _ in range(per):
                    matcher.record_hit("harness-01")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(matcher.load()["fingerprints"][0]["match_count"], 1 + n_threads * per)

    def test_cross_process_concurrent_evolve_no_lost_count(self):
        """并发演进同一结构: 锁内读改写, 4 进程应合并为 1 条且计数=4(不丢更新)。"""
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_worker_remember, args=(f"agent-{i}", False)) for i in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0)
        db = matcher.load()
        self.assertEqual(len(db["fingerprints"]), 1)
        self.assertEqual(db["fingerprints"][0]["match_count"], 4)
        json.loads(matcher.db_path().read_text(encoding="utf-8"))

    def test_cross_process_concurrent_create_no_lost_entries(self):
        """并发新增不同结构: 4 条不同指纹应全部保留, 无覆盖无半写。"""
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_worker_remember, args=(f"agent-{i}", True)) for i in range(4)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(30)
            self.assertEqual(p.exitcode, 0)
        db = matcher.load()
        ids = {e["id"] for e in db["fingerprints"]}
        self.assertEqual(len(ids), 4, ids)
        json.loads(matcher.db_path().read_text(encoding="utf-8"))


    def test_record_hit_miss_no_write(self):
        """未命中: 内容与 mtime 均不变, 证明完全没有落盘。"""
        matcher.remember(NATIVE_STRUCT, _recipe("nativesrv"), 8)
        path = matcher.db_path()
        before_data = path.read_bytes()
        before_mtime = path.stat().st_mtime_ns
        self.assertEqual(matcher.record_hit("harness-999"), 0)
        self.assertEqual(path.read_bytes(), before_data)
        self.assertEqual(path.stat().st_mtime_ns, before_mtime)

    def test_corrupt_db_never_overwritten(self):
        """损坏库: load / record_hit 必须报错, 且原文件逐字节保留(禁止静默清库)。"""
        path = matcher.db_path()
        corrupt = b'{broken json!!'
        path.write_bytes(corrupt)
        with self.assertRaises(json.JSONDecodeError):
            matcher.load()
        with self.assertRaises(json.JSONDecodeError):
            matcher.record_hit("harness-01")
        self.assertEqual(path.read_bytes(), corrupt)

    def test_missing_db_inits_only_on_real_write(self):
        """只有文件不存在才能初始化; 纯读与未命中写不得创建库文件。"""
        path = matcher.db_path()
        self.assertFalse(path.exists())
        self.assertEqual(matcher.load(), {"fingerprints": [], "version": 1})
        self.assertFalse(path.exists())
        self.assertEqual(matcher.record_hit("harness-01"), 0)
        self.assertFalse(path.exists())
        matcher.remember(NATIVE_STRUCT, _recipe("nativesrv"), 8)
        self.assertTrue(path.exists())

    def test_cross_process_spawn_concurrent_create(self):
        """spawn 跨进程(非 fork)并发新增: 文件锁仍保证 4 条不丢、无半写。"""
        ctx = multiprocessing.get_context("spawn")
        procs = [ctx.Process(target=_worker_remember_spawn, args=(f"agent-{i}",)) for i in range(4)]
        for pr in procs:
            pr.start()
        for pr in procs:
            pr.join(60)
            self.assertEqual(pr.exitcode, 0)
        db = matcher.load()
        self.assertEqual(len({e["id"] for e in db["fingerprints"]}), 4)
        json.loads(matcher.db_path().read_text(encoding="utf-8"))

    def test_analyst_tools_prior_reads_isolated_db(self):
        """P2: analyst_tools 的 prior 读取统一走隔离库(ASG_FINGERPRINT_DB)。"""
        import importlib.util
        mod_path = str(Path(__file__).resolve().parent / "runtime" / "analyst_tools.py")
        spec = importlib.util.spec_from_file_location("asg_analyst_tools_s1", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(str(mod._prior_db()), os.environ["ASG_FINGERPRINT_DB"])

    def test_e2e_make_run_root_isolation(self):
        """P1: E2E 每次运行创建独立目录与独立库; 生产库文件不被触碰。"""
        import importlib.util
        mod_path = str(Path(__file__).resolve().parent / "e2e" / "e2e_unknown.py")
        spec = importlib.util.spec_from_file_location("asg_e2e_unknown_s1", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        prod_db = mod.ROOT / "runtime" / "fingerprints.json"
        before = None
        if prod_db.exists():
            before = (prod_db.read_bytes(), prod_db.stat().st_mtime_ns)
        tmp = tempfile.TemporaryDirectory()
        try:
            mod.ROOT = Path(tmp.name)  # artifacts 写入重定向到临时目录
            r1 = mod.make_run_root()
            r2 = mod.make_run_root()
            self.assertNotEqual(r1, r2)
            self.assertTrue(r1.is_dir() and r2.is_dir())
            self.assertEqual(os.environ["ASG_FINGERPRINT_DB"], str(r2 / "fingerprints.json"))
            self.assertFalse((r2 / "fingerprints.json").exists())  # 惰性初始化
        finally:
            tmp.cleanup()
        if before is not None:
            self.assertEqual((prod_db.read_bytes(), prod_db.stat().st_mtime_ns), before)

    def test_subprocess_mcp_prior_isolated_db(self):
        """真实启动路径 + 隔离库: 默认库与隔离库放不同标记, 验证 MCP 返回隔离库内容。"""
        iso_path = matcher.db_path()  # setUp 已指向临时隔离库
        iso_path.write_text(json.dumps({
            "version": 2,
            "marker": "ISOLATED-DB",
            "fingerprints": [{
                "id": "iso-1", "name": "iso-agent", "match_count": 3,
                "features": {"exe": "iso", "runtime": "native", "entry_token": "",
                             "flags": [], "config_dirs": []},
                "hook_recipe": {"name_marker": "ISOLATED-RECIPE"},
            }],
        }), encoding="utf-8")
        prod_db = Path(__file__).resolve().parent / "runtime" / "fingerprints.json"
        prod_before = None
        if prod_db.exists():
            prod_before = (prod_db.read_bytes(), prod_db.stat().st_mtime_ns)

        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.setdefault("ASG_FINGERPRINT_DB", str(iso_path))
        res = _mcp_get_prior(env)

        self.assertEqual(res["returncode"], 0, res["stderr"])
        self.assertIsNotNone(res["result"])
        self.assertEqual(res["result"]["path"], str(iso_path))
        self.assertEqual(res["result"]["value"]["marker"], "ISOLATED-DB")
        self.assertEqual(res["result"]["value"]["fingerprints"][0]["name"], "iso-agent")
        # 隔离库路径差异即可证明未回退默认库: 默认库不含该标记
        if prod_before is not None:
            self.assertEqual((prod_db.read_bytes(), prod_db.stat().st_mtime_ns), prod_before)

    def test_subprocess_mcp_prior_default_path_without_pythonpath(self):
        """无 PYTHONPATH、未设隔离库: 导入路径修复后应读到默认库, 而非导入失败回退。"""
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env.pop("ASG_FINGERPRINT_DB", None)
        res = _mcp_get_prior(env)
        self.assertEqual(res["returncode"], 0, res["stderr"])
        self.assertIsNotNone(res["result"])
        self.assertTrue(
            str(res["result"]["path"]).endswith("runtime/fingerprints.json"),
            res["result"],
        )


if __name__ == "__main__":
    unittest.main()
