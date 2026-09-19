import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from .event_spool import EventSpool

class EventSpoolTest(unittest.TestCase):
    def test_offline_replay_identity_and_partial_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(str(Path(tmp)/'state.db'))
            calls=[]
            def offline(*args):raise OSError('offline')
            endpoint=SimpleNamespace(db=db,request=offline)
            agent={'agent_id':'a','asg_instance_id':'42:100'}
            path=Path(tmp)/'events.jsonl'
            good={'pid':42,'timestamp':101,'event':'user.input','detail':{'text':'hello'}}
            path.write_text(json.dumps(good)+'\n'+json.dumps({**good,'pid':43})+'\n'+json.dumps(good))
            binding={'instance_id':'42:100','target':{'pid':42,'create_time':100},'config':{'log_path':str(path)}}
            spool=EventSpool(endpoint)
            with patch('runtime.hook_data._registry_bindings',return_value=([binding],[],None)):
                spool.collect(agent,{'run_dir':tmp});spool.flush(agent)
                self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],1)
                spool=EventSpool(endpoint)
                spool.collect(agent,{'run_dir':tmp})
                self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],1)
                with path.open('a') as f:f.write('\n')
                spool.collect(agent,{'run_dir':tmp})
                self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],2)
                endpoint.request=lambda *args:(calls.append(json.loads(args[2])) or {'accepted':True})
                spool.flush(agent)
                self.assertEqual(len(calls[0]['events']),2)
                self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],0)
                spool.collect(agent,{'run_dir':tmp})
                self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],0)
if __name__=='__main__':unittest.main()
