"""Hand-built evidence/recipe fixtures test the bridge, not autonomous synthesis."""
import json,os,tempfile,unittest
from pathlib import Path
import psutil
from runtime.learned_onboarding import prepare,execute

class BridgeTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.root=Path(self.tmp.name).resolve();self.ws=self.root/'workspace';self.ws.mkdir()
  self.state=self.root/'state';self.state.mkdir();self.ev=self.root/'evidence';self.ev.mkdir()
  self.target={'pid':os.getpid(),'create_time':psutil.Process().create_time()}
  ref='ev-123456789-0123456789'
  (self.ev/(ref+'.json')).write_text(json.dumps({'target':self.target,'tool':'get_target_context','result':{'path':'fixture'},'error':None}))
  self.recipe={'agent_identity_name':'random-test','match_features':{'runtime':'fixture'},'observation':'fixture',
   'fallback':'leave untouched','evidence_refs':[ref],
   'hook':{'method':'file_plan','workspace':str(self.ws),'restart_required':True,'capabilities':['observation'],
    'verification':'real target event still required','rollback':'generic transaction','limitations':[]},
   'install_plan':{'version':1,'files':[{'path':'extensions/custom.js','content':'// test fixture','expected_sha256':None}]}}
 def test_evidence_to_install_no_product_registry(self):
  p=prepare(self.recipe,self.ev,self.target,self.ws)
  r=execute(p,self.state,approved_workspace=self.ws,approved_candidate_digest=p['candidate_digest'])
  self.assertEqual(r['status'],'installed');self.assertEqual(r['activation'],'unverified')
 def test_workspace_mismatch(self):
  self.recipe['hook']['workspace']='/wrong'
  with self.assertRaises(ValueError):prepare(self.recipe,self.ev,self.target,self.ws)
 def test_unsupported_never_executes(self):
  self.recipe['hook']['method']='unsupported'
  with self.assertRaises(ValueError):prepare(self.recipe,self.ev,self.target,self.ws)
 def test_wrong_candidate_approval(self):
  p=prepare(self.recipe,self.ev,self.target,self.ws)
  with self.assertRaises(PermissionError):execute(p,self.state,approved_workspace=self.ws,approved_candidate_digest='wrong')
  self.assertEqual(list(self.ws.iterdir()),[])

if __name__=='__main__':unittest.main()
