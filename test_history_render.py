"""Execute the real page script: numeric historical bindings must not stop rendering."""
import ast
from pathlib import Path
import shutil
import subprocess
import unittest

class HistoryRenderTests(unittest.TestCase):
    def test_real_update_ui_renders_history_and_continues_to_grid(self):
        tree=ast.parse(Path('monitor_dashboard.py').read_text())
        html=next(ast.literal_eval(n.value) for n in tree.body if isinstance(n,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='HTML_PAGE' for t in n.targets))
        script=html.split('<script>',1)[1].split('</script>',1)[0]
        bootstrap='''
const assert=require('assert');
const nodes={}; const errors=[];
const document={getElementById(id){return nodes[id] || (nodes[id]={style:{},dataset:{},classList:{add(){},remove(){}}});}};
const window={location:{hash:''},addEventListener(){}};
const setInterval=()=>0;
console.error=(...args)=>errors.push(args.join(' '));
const fetch=async()=>({json:async()=>({agents:[],scan_count:2,observation_instances:{old:{instance_pid:42,target_alive:false,events:{valid:0},paired_calls:[]}}})});
'''
        checks='''
setImmediate(()=>{
 assert.deepStrictEqual(errors,[]);
 assert(nodes['observation-history'].innerHTML.includes('PID 42'));
 assert(nodes['observation-history'].innerHTML.includes('有效事件 0'));
 assert(nodes['agents-grid'].innerHTML);
 assert.strictEqual(escapeHtml(0),'0');
 assert.strictEqual(escapeHtml('<img>'),'&lt;img&gt;');
});
'''
        result=subprocess.run([shutil.which('node') or '/Users/mac/.nvm/versions/node/v24.16.0/bin/node','-e',bootstrap+script+checks],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
