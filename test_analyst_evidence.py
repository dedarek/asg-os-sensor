import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from runtime.analyst_evidence import entry_surface, find_related_files, metadata_candidates, read_related_file
from runtime.analyst_tools import _validate_investigation_summary


class AnalystEvidenceTests(unittest.TestCase):
    def test_paged_file_search_retains_access_without_bulk_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(45):
                (root / ('item-%02d.json' % i)).write_text('{}')
            surface = {'related_roots': [{'id': 'root-0', 'path': str(root)}]}
            names, offset = [], 0
            while True:
                page = find_related_files(surface, '*.json', 'root-0', limit=100, offset=offset)
                self.assertLessEqual(len(page['files']), 20)
                names.extend(f['path'] for f in page['files'])
                offset = page['next_offset']
                if offset is None:
                    break
            self.assertEqual(len(names), 45)
            self.assertEqual(len(set(names)), 45)

    def test_search_descends_into_evidenced_package_scope_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            package = root / '.runtime' / 'deps' / '@random' / 'extension' / 'dist'
            package.mkdir(parents=True)
            (root / 'manifest.json').write_text('{}')
            (package / 'entry.d.ts').write_text('export type Hooks = {}')
            surface = {'related_roots': [{'id': 'root-0', 'path': str(root)}]}
            page = find_related_files(surface, '*.ts', str(package))
            self.assertEqual([f['name'] for f in page['files']], ['entry.d.ts'])
            self.assertTrue(find_related_files(surface, '*', str(root))['files'])
            with self.assertRaises(ValueError):
                find_related_files(surface, '*', str(root.parent))

    def test_preserved_evidence_is_retrievable_by_pointer_and_page(self):
        from runtime import analyst_tools as at
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = 'ev-123456789-0123456789'
            target = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}
            payload = {'tool': 'read_related_file', 'target': target, 'result': {'content': 'x' * 8001}}
            (root / (ref + '.json')).write_text(json.dumps(payload))
            with patch.object(at, 'EVIDENCE_DIR', root), patch.object(at, 'TARGET_PID', target['pid']), \
                 patch.object(at, 'TARGET_CREATE_TIME', target['create_time']):
                first = at.call_tool('read_evidence', {'evidence_id': ref, 'select': '/content'})
                last = at.call_tool('read_evidence', {'evidence_id': ref, 'select': '/content', 'offset': 8000})
                self.assertEqual(len(first['value']), 4000)
                self.assertEqual(first['next_offset'], 4000)
                self.assertEqual(last['value'], 'x')
                self.assertEqual(first['source_evidence_id'], ref)
                with self.assertRaises(ValueError):
                    at.call_tool('read_evidence', {'evidence_id': '../outside'})
    def test_entry_metadata_and_bounded_file_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "random-runtime.py"
            script.write_text("import time; time.sleep(20)\n", encoding="utf-8")
            (root / "package.json").write_text(json.dumps({
                "name": "runtime-under-test",
                "version": "1.2.3",
                "bin": {"runtime-under-test": "random-runtime.py"},
                "dependencies": {"some-sdk": "*"},
            }), encoding="utf-8")
            (root / "settings.json").write_text(json.dumps({
                "model": "gateway/model-a",
                "mcpServers": {"local-tool": {"command": "ignored"}},
                "apiKey": "do-not-export",
            }), encoding="utf-8")
            (root / "authorized_keys").write_text("ssh-ed25519 AAAA-not-exported\n", encoding="utf-8")
            proc = subprocess.Popen([sys.executable, str(script)], cwd=str(root),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                observed = psutil.Process(proc.pid)
                surface = entry_surface(observed)
                self.assertEqual(surface["target"]["pid"], proc.pid)
                self.assertTrue(any(item["resolved"] == str(script.resolve())
                                    for item in surface["entry_candidates"]))
                metadata = metadata_candidates(surface)
                self.assertIn("runtime-under-test", json.dumps(metadata))
                self.assertNotIn("agent_identity", surface)

                cwd_root = next(item for item in surface["related_roots"]
                                if item["source"] == "process.cwd")
                root_token = f"root-{surface['related_roots'].index(cwd_root)}"
                found = find_related_files(surface, "*.json", scope=root_token)
                settings = next(item for item in found["files"] if item["name"] == "settings.json")
                self.assertEqual(find_related_files(surface, "authorized_keys", scope=root_token)["files"], [])
                read = read_related_file(surface, settings["path"])
                self.assertEqual(read["parse_status"], "json")
                self.assertIn("gateway/model-a", json.dumps(read))
                self.assertNotIn("do-not-export", json.dumps(read))
                with self.assertRaises(ValueError):
                    read_related_file(surface, str(Path.home() / "outside.json"))
            finally:
                proc.terminate()
                proc.wait(timeout=3)

    def test_home_or_root_cwd_is_not_a_recursive_search_root(self):
        fake = type("FakeProcess", (), {
            "pid": 1,
            "exe": lambda self: "/usr/bin/runtime",
            "cmdline": lambda self: ["runtime"],
            "cwd": lambda self: "/",
            "parent": lambda self: None,
            "children": lambda self, recursive=False: [],
            "open_files": lambda self: [],
            "ppid": lambda self: 0,
            "status": lambda self: "running",
            "create_time": lambda self: 1.0,
        })()
        surface = entry_surface(fake)
        self.assertFalse(any(item["path"] == "/" for item in surface["related_roots"]))

    def test_script_mcp_exposes_generic_entry_surface_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(__file__).resolve().parent
            audit = Path(tmp) / "audit"
            env = dict(os.environ)
            env.pop("PYTHONPATH", None)
            env.update({
                "ASG_TARGET_PID": str(os.getpid()),
                "ASG_TARGET_CREATE_TIME": str(psutil.Process(os.getpid()).create_time()),
                "ASG_AUDIT_DIR": str(audit),
                "ASG_FINGERPRINT_DB": str(Path(tmp) / "isolated-fingerprints.json"),
                "ASG_RECIPE_DIR": str(Path(tmp) / "recipes"),
            })
            proc = subprocess.Popen(
                [sys.executable, "-B", str(root / "runtime" / "analyst_tools.py")],
                cwd=str(Path(tmp)), env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            request = json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "inspect_entry_surface", "arguments": {}},
            }) + "\n"
            out, err = proc.communicate(request, timeout=20)
            self.assertEqual(proc.returncode, 0, err)
            messages = [json.loads(line) for line in out.splitlines() if line.strip()]
            result = next(message["result"]["content"][0]["text"]
                          for message in messages if message.get("id") == 1)
            payload = json.loads(result)
            self.assertEqual(payload["target"]["pid"], os.getpid())
            self.assertIn("entry_candidates", payload)
            evidence = list((audit / "evidence").glob("ev-*.json"))
            self.assertEqual(len(evidence), 1)
            recorded = json.loads(evidence[0].read_text(encoding="utf-8"))
            self.assertEqual(recorded["tool"], "inspect_entry_surface")
            self.assertEqual(recorded["target"]["pid"], os.getpid())

    def test_investigation_summary_requires_cited_identity_and_asset_states(self):
        summary = {
            "identity_evidence": {"sources": ["ev-identity"], "uncertainty": []},
            "assets": {
                "model_gateway": {"status": "unknown", "sources": [], "uncertainty": ["not observed"]},
                "mcp": {"status": "collected", "sources": ["ev-mcp"], "uncertainty": []},
                "skills": {"status": "empty", "sources": ["ev-skills"], "uncertainty": []},
                "rules": {"status": "failed", "sources": ["ev-rules"], "uncertainty": ["read failed"]},
            },
        }
        _validate_investigation_summary({"investigation": summary})
        with self.assertRaises(ValueError):
            _validate_investigation_summary({"investigation": {"identity_evidence": {"sources": []}, "assets": {}}})

    def test_search_target_image_hit_miss_paging_and_wrong_pid(self):
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        create_time = psutil.Process(proc.pid).create_time()
        from runtime import analyst_tools as at
        with patch.object(at, "TARGET_PID", proc.pid), \
             patch.object(at, "TARGET_CREATE_TIME", create_time):
            # "Python" appears in the stub exe; hits are bounded by MAX_HITS
            first = at.search_target_image({"query": "Python"})
            self.assertEqual(first["status"], "collected")
            self.assertGreater(len(first["hits"]), 0)
            self.assertLessEqual(len(first["hits"]), at.SEARCH_IMAGE_MAX_HITS)
            self.assertEqual(first["truncated"], len(first["hits"]) == at.SEARCH_IMAGE_MAX_HITS)
            self.assertEqual(first["target"]["pid"], proc.pid)
            for hit in first["hits"]:
                self.assertGreater(len(hit["context"]), 0)
                self.assertLess(len(hit["context"]), 1200)
                self.assertLess(hit["offset"], first["size"])
            miss = at.search_target_image({"query": "NO-SUCH-LITERAL-STRING"})
            self.assertEqual(miss["status"], "collected")
            self.assertEqual(miss["hits"], [])
            self.assertIsNone(miss["next_offset"])
            for bad in ({"query": ""}, {"query": "x" * 300}, {}):
                with self.assertRaises(ValueError):
                    at.search_target_image(bad)

    def test_search_target_image_rejects_wrong_create_time(self):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        from runtime import analyst_tools as at
        with patch.object(at, "TARGET_PID", proc.pid), \
             patch.dict(os.environ, {"ASG_TARGET_CREATE_TIME": "1.0"}):
            with self.assertRaises(RuntimeError):
                at.search_target_image({"query": "anything"})

    def test_image_pages_absolute_offsets_and_byte_limit(self):
        from runtime import analyst_tools as at
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'image'
            path.write_bytes(b'prefix-' + b'needle-' * 12)
            with patch.object(at, 'target_process', return_value=SimpleNamespace(exe=lambda: str(path))), \
                 patch.object(at, 'SEARCH_IMAGE_CHUNK_BYTES', 16):
                first = at.search_target_image({'query': 'needle'})
                second = at.search_target_image({'query': 'needle', 'offset': first['next_offset']})
                self.assertEqual([h['offset'] for h in first['hits'] + second['hits']], list(range(7, 91, 7)))
                self.assertIsNone(second['next_offset'])
                self.assertEqual(at.search_target_image({'query': 'needle', 'offset': 1000})['hits'], [])
                for offset in (1.1, True, '12', -1):
                    with self.assertRaises(ValueError):
                        at.search_target_image({'query': 'needle', 'offset': offset})
                with self.assertRaises(ValueError):
                    at.search_target_image({'query': '中' * 86})
                path.write_bytes(b'')
                self.assertEqual(at.search_target_image({'query': 'needle'})['searched_range'], [0, 0])

    def test_identity_value_preserves_roles_and_accepts_image_evidence(self):
        from runtime import analyst_tools as at
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = 'ev-123456789-0123456789'
            target = {'pid': os.getpid(), 'create_time': psutil.Process().create_time()}
            (root / (ref + '.json')).write_text(json.dumps({'target': target, 'tool': 'search_target_image',
                'result': {'status': 'collected', 'hits': [{'context': 'test role evidence'}]}}))
            with patch.object(at, 'EVIDENCE_DIR', root), patch.object(at, 'FINDINGS_PATH', root / 'findings.json'), \
                 patch.object(at, 'TARGET_PID', target['pid']), patch.object(at, 'TARGET_CREATE_TIME', target['create_time']):
                saved = at._submit_investigation_finding({'kind': 'identity', 'status': 'identified',
                    'value': {'name': 'neutral-fixture', 'roles': ['model_gateway'], 'role_reasoning': 'fixture forwards requests'},
                    'evidence_refs': [ref]})
            self.assertEqual(saved['finding']['value']['roles'], ['model_gateway'])
            self.assertEqual(saved['finding']['value']['name'], 'neutral-fixture')


if __name__ == "__main__":
    unittest.main()
