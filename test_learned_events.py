"""Contract fixtures, explicitly not real Agent event evidence."""
import json,os,tempfile,time,unittest
from pathlib import Path
import psutil
from runtime.learned_events import verify

class LearnedEventTests(unittest.TestCase):
 def test_fresh_correlated_events_and_reject_old_nonce(self):
  with tempfile.TemporaryDirectory() as tmp:
   path=Path(tmp)/'events.jsonl';target={'pid':os.getpid(),'create_time':psutil.Process().create_time()};now=time.time()
   rows=[{'pid':target['pid'],'nonce':'fresh-test','timestamp':now,'event_type':kind,'call_id':'c1','tool_name':'read'} for kind in ['hook.loaded','tool.execute.before','tool.execute.after']]
   path.write_text('\n'.join(json.dumps(row) for row in rows))
   good=verify(path,target,nonce='fresh-test',not_before=now)
   self.assertEqual(good['status'],'observing');self.assertEqual(good['invalid_events'],0)
   self.assertNotIn('fresh-test',json.dumps(good))
   bad=verify(path,target,nonce='different',not_before=now)
   self.assertEqual(bad['valid_events'],0);self.assertFalse(bad['observing'])
   rows=rows[:1]+rows[2:];path.write_text('\n'.join(json.dumps(row) for row in rows))
   self.assertFalse(verify(path,target,nonce='fresh-test',not_before=now)['observing'])
 def test_wrong_process_generation_rejected(self):
  with self.assertRaises(ValueError):verify(Path('/unused'),{'pid':os.getpid(),'create_time':1.0},nonce='test',not_before=time.time())

if __name__=='__main__':unittest.main()
