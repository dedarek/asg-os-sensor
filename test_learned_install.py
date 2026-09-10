import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from runtime import learned_install as li

class LearnedInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.ws = self.root/'workspace';self.ws.mkdir()
        self.state = self.root/'state';self.state.mkdir()
    def plan(self, path='extensions/observer.js', content='export default async()=>({});', expected=None):
        return {'version':1,'files':[{'path':path,'content':content,'expected_sha256':expected}]}
    def install(self, plan):
        return li.install(plan,self.ws,self.state,approved_workspace=self.ws,approved_digest=li.plan_digest(plan))
    def rollback(self, plan):
        return li.rollback(self.ws,self.state,approved_workspace=self.ws,approved_digest=li.plan_digest(plan))
    def test_create_reuse_rollback(self):
        plan=self.plan(); first=self.install(plan)
        self.assertEqual(first['activation'],'unverified')
        self.assertEqual((self.ws/'extensions/observer.js').read_text(),plan['files'][0]['content'])
        self.assertEqual(self.install(plan)['status'],'already_installed')
        self.assertEqual(self.rollback(plan)['status'],'rolled_back')
        self.assertFalse((self.ws/'extensions').exists())
    def test_edit_restores_original(self):
        path=self.ws/'config.toml';path.write_text('original=true');path.chmod(0o640)
        plan=self.plan('config.toml','changed=true',li.digest(path.read_bytes()))
        self.install(plan);self.rollback(plan)
        self.assertEqual(path.read_text(),'original=true');self.assertEqual(path.stat().st_mode & 0o777,0o640)
    def test_no_approval(self):
        with self.assertRaises(PermissionError):li.install(self.plan(),self.ws,self.state,approved_workspace=self.ws,approved_digest='0'*64)
        self.assertEqual(list(self.ws.iterdir()),[])
    def test_scope_and_symlinks(self):
        for name in ['../escape','/tmp/escape','a/../escape']:
            with self.assertRaises(ValueError):self.install(self.plan(name))
        (self.ws/'extensions').symlink_to(self.state)
        with self.assertRaises(ValueError):self.install(self.plan())
    def test_changed_file_not_overwritten(self):
        plan=self.plan();self.install(plan)
        file=self.ws/'extensions/observer.js';file.write_text('user edit')
        with self.assertRaises(ValueError):self.install(plan)
        with self.assertRaises(ValueError):self.rollback(plan)
        self.assertEqual(file.read_text(),'user edit')
    def test_all_preconditions_checked_before_writing(self):
        (self.ws/'config.toml').write_text('user-owned')
        plan=self.plan();plan['files']+=self.plan('config.toml')['files']
        with self.assertRaises(ValueError):self.install(plan)
        self.assertFalse((self.ws/'extensions').exists())
    def test_failure_rolls_back_previous_writes(self):
        plan=self.plan('first.txt','one');plan['files']+=self.plan('second.txt','two')['files']
        original=li._atomic
        def fail_second(path,*args,**kwargs):
            if path.name=='second.txt':raise OSError('simulated disk failure')
            return original(path,*args,**kwargs)
        with patch.object(li,'_atomic',side_effect=fail_second):
            with self.assertRaises(OSError):self.install(plan)
        self.assertFalse((self.ws/'first.txt').exists())
    def test_no_product_adapter_required(self):
        plan=self.plan('.anything/hook.py','def callback(): pass')
        self.assertEqual(self.install(plan)['status'],'installed')

if __name__=='__main__':unittest.main()
