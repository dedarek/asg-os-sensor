import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from runtime import model_settings as m
from runtime.investigation_findings import validate_finding

class SettingsTests(unittest.TestCase):
    def test_only_assistant_stream_deltas_count_as_reply(self):
        lines = [json.dumps({'role':'user','content':[{'type':'text','text':'SECRET'}]}),
                 json.dumps({'type':'message','message':{'role':'assistant','content':[{'type':'text','text':'AS'}]}}),
                 json.dumps({'type':'message','message':{'role':'assistant','content':[{'type':'text','text':'G-123'}]}})]
        self.assertEqual(m.assistant_text('\n'.join(lines)), 'ASG-123')

    def test_changed_endpoint_never_receives_previous_key(self):
        route={'base_url':'https://original.example/v1','model':'old'}
        with patch('runtime.llm_config.analyst_route',return_value=route), patch('runtime.llm_config.analyst_key',return_value='private'):
            with self.assertRaises(ValueError):
                m.prepare({'base_url':'https://other.example/v1','model':'new'})
            candidate=m.prepare({'base_url':'https://original.example/v1/chat/completions','model':'new'})
            self.assertEqual(candidate['key'],'private')
            self.assertEqual(candidate['route']['base_url'],route['base_url'])

    def test_failed_goose_does_not_change_saved_configuration(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'ASG_RUN_DIR':tmp}), \
             patch('runtime.llm_config.goose_env',return_value={}), patch('shutil.which',return_value='/goose'), \
             patch('subprocess.run') as run:
            m.path().write_text('{"unchanged": true}')
            run.return_value.returncode=0;run.return_value.stdout='{"role":"user","content":[]}'
            m._test({'route':{'model':'test'},'key':'secret'})
            self.assertEqual(json.loads(m.path().read_text()),{'unchanged':True})
            self.assertEqual(m.JOB['status'],'failed')

    def test_standard_display_rejects_opaque_object_fact(self):
        f={'kind':'identity','status':'identified','value':{'name':'arbitrary'},'evidence_refs':['ev-test'],
           'display':{'version':1,'summary':'用途','scope':'本机','facts':[{'label':'用途','value':{}}]}}
        with self.assertRaises(ValueError):validate_finding(f)
        f['display']['facts'][0]['value']='转发请求'
        self.assertEqual(validate_finding(f)['display']['summary'],'用途')
