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
        self.assertIsNotNone(entry)
        self.assertEqual(entry["id"], "harness-01")
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
        self.assertIsNotNone(hit)
        self.assertEqual(hit["id"], "harness-01")

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


if __name__ == "__main__":
    unittest.main()

