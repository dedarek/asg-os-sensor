import unittest
from runtime.llm_config import goose_env

class RouteContextTests(unittest.TestCase):
 def test_per_route_capacity_not_total_time(self):
  r={'provider':'openai','base_url':'http://localhost:1234/v1','model':'arbitrary-model','context_limit':32768,'max_output_tokens':4096}
  e=goose_env(r,'fixture')
  self.assertEqual(e['GOOSE_CONTEXT_LIMIT'],'32768');self.assertEqual(e['GOOSE_MAX_TOKENS'],'4096')
  self.assertNotIn('ASG_GOOSE_TIMEOUT',e)
  del r['context_limit'];self.assertNotIn('GOOSE_CONTEXT_LIMIT',goose_env(r,'fixture'))
 def test_bad_capacity_rejected(self):
  for value in (True,0,-1,'32768'):
   with self.assertRaises(ValueError):goose_env({'context_limit':value},'fixture')

if __name__=='__main__':unittest.main()
