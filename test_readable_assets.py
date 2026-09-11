"""Render-only checks: debug payloads remain escaped and collapsed by default."""
import ast
import subprocess
import unittest
from pathlib import Path
import shutil


class ReadableAssetTests(unittest.TestCase):
    def test_summary_and_expandable_evidence(self):
        node = shutil.which('node') or '/Users/mac/.nvm/versions/node/v24.16.0/bin/node'
        source = Path(__file__).with_name('monitor_dashboard.py').read_text()
        tree = ast.parse(source)
        source = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'HTML_PAGE' for t in n.targets))
        renderer = source[source.index('const readableOpen ='):source.index('function classificationText(')]
        checks = r'''
const assert = require('assert');
function escapeHtml(s) {return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
const item={status:'collected', value:{configuration_status:'configured',items:[{model:'test-model',base_url:'http://127.0.0.1:9999/v1'}]}, evidence_refs:['ev-test'],uncertainty:['very long diagnostic']};
const html=assetText({assets:{model_routing:item}},'model_routing');
const summary=html.replace(/<[^>]*>/g,'');
assert(summary.includes('test-model') && summary.includes('来源：配置'));
assert(!summary.includes('ev-test') && !summary.includes('very long diagnostic'));
assert(html.includes('openDetail') && !html.includes('very long diagnostic'));
const nodes={};
const document={activeElement:null,getElementById(id){return nodes[id] || (nodes[id]={classList:{add(){},remove(){}},focus(){}})}};
openDetail('model_routing:ev-test');
assert(nodes['inspect-drawer-body'].innerHTML.includes('very long diagnostic'));
assert(nodes['inspect-drawer-body'].innerHTML.includes('原始记录（JSON）'));
item.value.items[0].model='<img src=x onerror=alert(1)>';
assert(!assetText({assets:{model_routing:item}},'model_routing').includes('<img'));
const rules=assetText({assets:{system_prompt_rules:{status:'collected',value:{items:[{type:'runtime_permission_policy',policy:'[{permission:"*", pattern:"*", action:"deny"},{permission:"read", pattern:"*", action:"allow"}]'}]}}}},'system_prompt_rules');
assert(rules.split('<details')[0].includes('其他操作：拒绝；read：允许'));
const empty=assetText({assets:{skills:{status:'empty',value:[]}}},'skills');
assert(empty.includes('检查范围内未发现'));
assert(!empty.includes('不代表全局不存在'));
assert(!summary.includes('未验证实际使用'));
'''
        proc = subprocess.run([node, '-e', renderer+'\n'+checks],text=True,capture_output=True)
        self.assertEqual(proc.returncode,0,proc.stderr)


if __name__ == '__main__':
    unittest.main()
