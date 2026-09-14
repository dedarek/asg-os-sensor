import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
import psutil
from runtime import hook_control as c

class ControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.env=patch.dict(os.environ,{'ASG_RUN_DIR':self.tmp.name});self.env.start()
        c._PENDING.clear();self.target={'pid':os.getpid(),'create_time':psutil.Process().create_time()}
        Path(self.tmp.name,'observations.json').write_text(json.dumps({'test':{'target':self.target}}))
        self.request={**self.target,'tool':'write','call_id':'a','input':{'password':'do-not-persist','content':'actual content'}}
    def tearDown(self):self.env.stop();self.tmp.cleanup()
    def test_allow_deny_and_ack_do_not_claim_enforcement(self):
        self.assertEqual(c.decide(self.request)['decision'],'allow')
        c.set_policy({'default':'allow','rules':[{'tool':'write','decision':'deny'}]})
        r=c.decide(self.request);self.assertEqual(r['decision'],'deny')
        self.assertFalse(c.ack({'request_id':r['request_id'],'applied':True,'outcome':'blocked'})['enforcement_verified'])
        with self.assertRaises(ValueError):c.ack({'request_id':r['request_id'],'applied':True,'outcome':'allowed'})
        self.assertNotIn('do-not-persist',json.dumps(c.status()))
        self.assertIn('actual content',json.dumps(c.status()))
    def test_confirmation_and_timeout(self):
        c.set_policy({'default':'ask','rules':[]});result=[]
        t=threading.Thread(target=lambda:result.append(c.decide({**self.request,'timeout_s':3})));t.start()
        end=time.monotonic()+2
        while not c.status()['pending'] and time.monotonic()<end:time.sleep(.01)
        c.resolve({'request_id':c.status()['pending'][0]['request_id'],'decision':'allow'});t.join(4)
        self.assertEqual(result[0]['decision'],'allow')
        self.assertEqual(c.decide({**self.request,'timeout_s':1})['reason'],'confirmation_timeout')
    def test_reject_unbound_or_replaced_process(self):
        with self.assertRaises(ValueError):c.decide({**self.request,'create_time':1})
        Path(self.tmp.name,'observations.json').write_text('{}')
        with self.assertRaises(ValueError):c.decide(self.request)
