import json
import tempfile
import unittest
from pathlib import Path
from runtime.investigation_activity import snapshot

class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / 'pid_12_1234'; self.run.mkdir()
        self.write('investigation_lifecycle.json', {'target': {'pid':12,'create_time':4.5},'status':'running'})
    def write(self, name, value):
        (self.run/name).write_text(json.dumps(value))
    def test_projection_no_secrets(self):
        self.write('analyst_tool_calls.jsonl', {'ts':'2026-09-10T12:00:00Z','tool':'read_related_file','args':{'password':'SECRET'},'error':'SECRET','evidence_id':'ev-1'})
        data=snapshot(self.root,12,4.5)
        self.assertNotIn('SECRET',json.dumps(data));self.assertEqual(data['events'][0]['status'],'failed')
        self.assertIsNone(data['events'][0]['duration_ms'])
    def test_wrong_instance(self):
        self.assertEqual(snapshot(self.root,12,8)['status'],'not_started')
    def test_traversal(self):
        with self.assertRaises(ValueError):snapshot(self.root,12,4.5,'../pid_12_1234')
    def test_symlink_audit(self):
        outside=self.root/'outside';outside.write_text('{"tool":"secret"}')
        (self.run/'analyst_tool_calls.jsonl').symlink_to(outside)
        data=snapshot(self.root,12,4.5);self.assertEqual(data['events'],[]);self.assertEqual(data['audit_status'],'unreadable')
    def test_limit_and_partial_line(self):
        (self.run/'analyst_tool_calls.jsonl').write_text('\n'.join(json.dumps({'tool':'search','ts':'2026-09-10T12:00:00Z'}) for _ in range(10))+'\n{"tool":')
        data=snapshot(self.root,12,4.5,limit=3);self.assertEqual(len(data['events']),3);self.assertTrue(data['truncated'])
    def test_findings(self):
        self.write('investigation_findings.json',{'target':{'pid':12,'create_time':4.5},'history':[{'kind':'asset','asset':'mcp','status':'collected','value':'SECRET'}]})
        data=snapshot(self.root,12,4.5);self.assertEqual(data['events'][0]['type'],'finding_saved');self.assertNotIn('SECRET',json.dumps(data))

if __name__ == '__main__':unittest.main()
