"""The dashboard conversation view groups by session and is expandable."""
import os
import shutil
import subprocess
import unittest
from pathlib import Path


class ConversationViewTests(unittest.TestCase):
    def test_render_conversation_groups_by_session(self):
        node = os.environ.get('ASG_TEST_NODE') or shutil.which('node')
        if not node:
            self.skipTest('node unavailable')
        source = (Path(__file__).resolve().parent / 'web/dashboard.html').read_text(encoding='utf-8')
        start = source.index('function renderConversation(conversation)')
        end = source.index('function renderTools(tools)', start)
        function = source[start:end]
        harness = (
            "const assert=require('node:assert/strict');\n"
            "const escapeHtml=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;');\n"
            + function + "\n"
            "const conv=[{role:'user',text:'hi',session_id:'ses_A',timestamp:'t1'},"
            "{role:'assistant',text:'yo',session_id:'ses_A',timestamp:'t2',truncated:true},"
            "{role:'user',text:'q',session_id:'ses_B',timestamp:'t3'}];\n"
            "const out=renderConversation(conv);\n"
            "assert(out.includes('2 个会话'));\n"
            "assert(out.includes('会话 ses_A') && out.includes('会话 ses_B'));\n"
            "assert(out.includes('点击展开原始内容'));\n"
            "assert((out.match(/<details/g)||[]).length===2);\n"
            "assert(out.includes('已截断'));\n"
            "assert(renderConversation([]).includes('0 条'));\n"
            "console.log('conversation view PASS');\n"
        )
        proc = subprocess.run([node, '-e', harness], capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('PASS', proc.stdout)


if __name__ == '__main__':
    unittest.main()
