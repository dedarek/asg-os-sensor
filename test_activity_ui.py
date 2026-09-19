"""Exercise actual inline activity JS with a minimal DOM/fetch fixture (not a browser)."""
import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

class ActivityUITests(unittest.TestCase):
    def test_real_js_history_and_success(self):
        node=os.environ.get('ASG_TEST_NODE') or shutil.which('node')
        if not node:self.skipTest('node unavailable')
        code=(Path(__file__).resolve().parent / 'web/dashboard.html').read_text(encoding='utf-8')
        js=code[code.index('const activityCache = {};'):code.index('function renderTools(tools)')]
        harness=r'''
const assert=require('node:assert/strict');
const box={innerHTML:''};
const document={getElementById:()=>box};
const escapeHtml=s=>String(s).replaceAll('<','&lt;');
const urls=[];
const fetch=async url=>{urls.push(url);return {ok:true,json:async()=>({events:[],runs:[],status:'running'})}};
'''+js+r'''
(async()=>{
 await loadActivity(12,'pid_12_100'); await loadActivity(12);
 assert(urls[1].includes('run_id=pid_12_100'));
 renderActivity(12,{events:[{type:'tool_completed',status:'succeeded',tool:'search'}]});
 assert(box.innerHTML.includes('color:#34d399'));
 renderActivity(12,{error:'activity_target_or_run_not_found'});
 assert(box.innerHTML.includes('activity_target_or_run_not_found'));
 console.log('activity JS history, status and error checks PASS');
})().catch(e=>{console.error(e);process.exitCode=1});
'''
        p=subprocess.run([node,'-e',harness],capture_output=True,text=True,timeout=10)
        self.assertEqual(p.returncode,0,p.stderr)
        # Assert the behavior, not one exact spacing of the inline template:
        # the open attribute must be driven by activityOpenPids for each pid.
        self.assertRegex(code, r"activityOpenPids\.has\(a\.pid\)\s*\?\s*'open'\s*:\s*''")

if __name__=='__main__':unittest.main()
