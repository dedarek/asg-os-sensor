import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from .event_spool import EventSpool

class EventSpoolTest(unittest.TestCase):
    def test_hook_child_pid_maps_to_bound_agent_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(str(Path(tmp)/'state.db'))
            sent=[]
            endpoint=SimpleNamespace(db=db,request=lambda _agent,_path,data:
                                     (sent.append(json.loads(data)) or {'accepted':True}))
            agent={'agent_id':'stable-asset','asg_instance_id':'42:100'}
            path=Path(tmp)/'hook.jsonl'
            path.write_text(json.dumps({'pid':999,'agent_pid':42,'timestamp':101,
                                        'event':'user.input','content':{'prompt':'hello'}})+'\n')
            binding={'instance_id':'42:100','target':{'pid':42,'create_time':100},
                     'config':{'log_path':str(path),'fields':{'pid':'agent_pid',
                                                               'event':'event','timestamp':'timestamp'}}}
            with patch('runtime.hook_data._registry_bindings',return_value=([binding],[],None)):
                EventSpool(endpoint).collect(agent,{'run_dir':tmp})
                agent['asg_instance_id']='43:200'
                EventSpool(endpoint).flush(agent)
            self.assertEqual(len(sent),1)
            self.assertEqual(sent[0]['instance_id'],'42:100')
            self.assertEqual(sent[0]['events'][0]['payload']['agent_pid'],42)
            self.assertEqual(sent[0]['events'][0]['payload']['pid'],999)

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
    def test_glued_and_oversized_records_recover_stream(self):
        # Crazytest B16: a torn append glued two JSON objects onto one physical
        # line, and a 966 KB record exceeded the 512 KB readline cap; the old
        # decoder dropped both cases as gaps and desynced every later read.
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(str(Path(tmp)/'state.db'))
            endpoint=SimpleNamespace(db=db,request=lambda *a:(_ for _ in ()).throw(OSError()))
            agent={'agent_id':'a','asg_instance_id':'42:100'}
            path=Path(tmp)/'events.jsonl'
            good={'pid':42,'timestamp':101,'event':'user.input','detail':{'text':'hello'}}
            glued=json.dumps(good)+json.dumps({**good,'event':'assistant.output'})+chr(10)
            oversized=json.dumps({**good,'event':'tool.execute.after','blob':'x'*9000000})+chr(10)
            path.write_text(glued+oversized+json.dumps({**good,'event':'stop'})+chr(10))
            binding={'instance_id':'42:100','target':{'pid':42,'create_time':100},'config':{'log_path':str(path)}}
            spool=EventSpool(endpoint)
            with patch('runtime.hook_data._registry_bindings',return_value=([binding],[],None)):
                spool.collect(agent,{'run_dir':tmp})  # oversized line exhausts this round's read budget
                spool.collect(agent,{'run_dir':tmp})  # resumes after the skipped record
            rows=[json.loads(r[0]) for r in db.execute('SELECT body FROM event_outbox ORDER BY rowid')]
            kinds=[r['event_type'] for r in rows]
            self.assertEqual(kinds,['user.input','assistant.output','collection.gap','stop'])
            self.assertEqual(rows[2]['reason'],'oversized_or_incomplete_record')
            # Resynchronization is durable: a second pass finds nothing new.
            with patch('runtime.hook_data._registry_bindings',return_value=([binding],[],None)):
                EventSpool(endpoint).collect(agent,{'run_dir':tmp})
            self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],4)

    def test_mixed_instances_flush_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(str(Path(tmp)/'state.db'))
            calls=[]
            endpoint=SimpleNamespace(db=db,request=lambda *args:(calls.append(json.loads(args[2])) or {'accepted':True}))
            agent={'agent_id':'a','asg_instance_id':'42:200'}
            db.execute('CREATE TABLE IF NOT EXISTS event_outbox(id TEXT PRIMARY KEY,agent TEXT,body BLOB NOT NULL)')
            for eid,inst in (('e1','42:100'),('e2','42:200'),('e3','42:100')):
                body=json.dumps({'event_id':eid,'instance_id':inst,'event_type':'user.input'}).encode()
                db.execute('INSERT INTO event_outbox VALUES(?,?,?)',(eid,'a',body))
            db.commit()
            EventSpool(endpoint).flush(agent)
            self.assertEqual([c['instance_id'] for c in calls],['42:100','42:200'])
            self.assertEqual(sorted(e['event_id'] for e in calls[0]['events']),['e1','e3'])
            self.assertEqual(calls[1]['events'][0]['event_id'],'e2')
            self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],0)

    def test_oversized_record_is_stubbed_not_head_blocking(self):
        # crazytest B19: a single >1.5 MB row used to sit at the head of its
        # instance group forever -- the gateway 413s any request over 2 MiB and
        # the size selection always keeps the first row. It must be replaced by
        # a bounded collection.gap stub so the rest of the stream keeps flowing.
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(str(Path(tmp)/'state.db'))
            calls=[]
            endpoint=SimpleNamespace(db=db,request=lambda *args:(calls.append(json.loads(args[2])) or {'accepted':True}))
            agent={'agent_id':'a','asg_instance_id':'42:100'}
            db.execute('CREATE TABLE IF NOT EXISTS event_outbox(id TEXT PRIMARY KEY,agent TEXT,body BLOB NOT NULL)')
            big=json.dumps({'event_id':'big','instance_id':'42:100','event_type':'tool.execute.after',
                            'payload':{'blob':'x'*1_600_000}}).encode()
            ok=json.dumps({'event_id':'ok','instance_id':'42:100','event_type':'stop'}).encode()
            db.execute('INSERT INTO event_outbox VALUES(?,?,?)',('big','a',big))
            db.execute('INSERT INTO event_outbox VALUES(?,?,?)',('ok','a',ok))
            db.commit()
            EventSpool(endpoint).flush(agent)
            sent=[e for call in calls for e in call['events']]
            self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],0)
            stubs=[e for e in sent if e['event_type']=='collection.gap']
            self.assertEqual(len(stubs),1)
            self.assertEqual(stubs[0]['event_id'],'big')
            self.assertEqual(stubs[0]['reason'],'oversized_record_truncated')
            self.assertGreater(stubs[0]['original_bytes'],1_500_000)
            self.assertEqual(len(stubs[0]['body_sha256']),64)
            self.assertIn('ok',[e['event_id'] for e in sent])
            # Second flush is a no-op: the stub replaces the row, it does not
            # reappear and it does not strand anything.
            EventSpool(endpoint).flush(agent)
            self.assertEqual(len(calls),1)
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(str(Path(tmp)/'state.db'))
            calls=[]
            endpoint=SimpleNamespace(db=db,request=lambda *args:(calls.append(json.loads(args[2])) or {'accepted':True}))
            agent={'agent_id':'a','asg_instance_id':'42:200'}
            db.execute('CREATE TABLE IF NOT EXISTS event_outbox(id TEXT PRIMARY KEY,agent TEXT,body BLOB NOT NULL)')
            for eid,inst in (('e1','42:100'),('e2','42:200'),('e3','42:100')):
                body=json.dumps({'event_id':eid,'instance_id':inst,'event_type':'user.input'}).encode()
                db.execute('INSERT INTO event_outbox VALUES(?,?,?)',(eid,'a',body))
            db.commit()
            EventSpool(endpoint).flush(agent)
            self.assertEqual([c['instance_id'] for c in calls],['42:100','42:200'])
            self.assertEqual(sorted(e['event_id'] for e in calls[0]['events']),['e1','e3'])
            self.assertEqual(calls[1]['events'][0]['event_id'],'e2')
            self.assertEqual(db.execute('SELECT count(*) FROM event_outbox').fetchone()[0],0)

if __name__=='__main__':unittest.main()
