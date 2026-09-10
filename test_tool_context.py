import copy,json,unittest
from runtime.tool_context import compact

class ToolContextTests(unittest.TestCase):
 def fixture(self):
  ms=[{'role':'system','content':'Do not expose credentials.'},{'role':'user','content':'Learn the mechanism.'}]
  for i in range(8):
   ms += [{'role':'assistant','tool_calls':[{'id':str(i),'type':'function','function':{'name':'read_related_file','arguments':json.dumps({'path':'/fixture/'+str(i)})}}]}, {'role':'tool','tool_call_id':str(i),'content':json.dumps({'evidence_id':'ev-'+str(i),'content':'DATA'*2000})}]
  return {'messages':ms,'tools':[{'type':'function','function':{'name':'read_evidence'}}]}
 def test_bounded_pairs_and_no_mutation(self):
  p=self.fixture();original=copy.deepcopy(p);out,stats=compact(p,3)
  self.assertEqual(p,original);self.assertTrue(stats['applied'])
  self.assertEqual(out['messages'][:2],p['messages'][:2]);self.assertEqual(out['messages'][-6:],p['messages'][-6:])
  self.assertIn('ev-0',out['messages'][2]['content']);self.assertLess(len(json.dumps(out)),len(json.dumps(p)))
  calls={c['id'] for m in out['messages'] for c in m.get('tool_calls',[])}
  replies={m['tool_call_id'] for m in out['messages'] if m['role']=='tool'}
  self.assertEqual(calls,replies)
 def test_incomplete_group_never_removed(self):
  p=self.fixture();del p['messages'][3]
  self.assertFalse(compact(p,3)[1]['applied'])
 def test_small_request_unchanged(self):
  p=self.fixture();self.assertEqual(compact(p,20)[0],p)

if __name__=='__main__':unittest.main()
