"""The injected SOC outbox helper must stay valid JS with true NDJSON separators.

A previous revision embedded double-backslash separators and an invalid regex
into the installed hook script, which silently corrupted multi-line event
payloads and could fail to parse at load time. These checks lock the fix in.
"""
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .package_runtime import install as installer

NL = chr(10)

HEADER = (
    'import { createHash } from "node:crypto";' + NL +
    'import { dirname, join } from "node:path";' + NL +
    'import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";' + NL +
    'const noteError=(k,e)=>{};' + NL
)

class EventHelperSourceTests(unittest.TestCase):
    def test_ndjson_separators_are_real_newlines(self):
        for line in installer._EVENT_HELPER.splitlines():
            if ("split(" in line and "socOutbox" in line) or ("join(" in line and "lines" in line):
                self.assertNotIn(chr(92) + chr(92) + "n", line, "NDJSON separator regressed to literal backslash-n")

    def test_stripped_trailing_slash_regex_is_valid(self):
        helper = installer._EVENT_HELPER
        self.assertNotIn("/" + chr(92) + chr(92) + "/$/", helper)
        self.assertIn("/" + chr(92) + "/$/", helper)

    def test_identity_binding_is_reloaded_for_every_event(self):
        helper=installer._EVENT_HELPER
        self.assertNotIn('if (socEventConfig) return socEventConfig',helper)
        self.assertIn('JSON.parse(readFileSync(CONTROL_CONFIG',helper)

class EventHelperRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.node = shutil.which("node")

    def test_helper_compiles_under_node(self):
        if not self.node:
            self.skipTest("node runtime unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "helper.mjs"
            script.write_text(HEADER + installer._EVENT_HELPER)
            done = subprocess.run([self.node, "--check", str(script)], capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)

    def test_standalone_control_client_compiles_under_node(self):
        if not self.node:
            self.skipTest("node runtime unavailable")
        source=Path(installer.__file__).with_name('soc_client.mjs')
        done=subprocess.run([self.node,'--check',str(source)],capture_output=True,text=True,timeout=30)
        self.assertEqual(done.returncode,0,done.stderr)

    def test_standalone_node_client_persists_offline_event_and_replays(self):
        if not self.node:
            self.skipTest("node runtime unavailable")
        source=Path(installer.__file__).with_name('soc_client.mjs')
        with tempfile.TemporaryDirectory() as tmp:
            directory=Path(tmp);token=directory/'token';token.write_text('k')
            probe=socket.socket();probe.bind(('127.0.0.1',0));port=probe.getsockname()[1];probe.close()
            config=directory/'config.json'
            config.write_text(json.dumps({'backend_url':f'http://127.0.0.1:{port}',
                'agent_id':'a1','instance_id':'42:100','token_file':str(token)}))
            event={'event':'user.input','timestamp':'2026-09-21T00:00:00Z','content':'offline'}
            first=subprocess.run([self.node,str(source),str(config),'event'],input=json.dumps(event),
                capture_output=True,text=True,timeout=30)
            self.assertEqual(first.returncode,0,first.stderr)
            self.assertFalse(json.loads(first.stdout)['accepted'])
            queued=list((directory/'soc-outbox-node').rglob('*.json'))
            self.assertEqual(len(queued),1)
            received=[]
            class Handler(BaseHTTPRequestHandler):
                def do_POST(self):
                    length=int(self.headers.get('Content-Length','0'))
                    received.append(json.loads(self.rfile.read(length)))
                    body=b'{"accepted":true}'
                    self.send_response(200);self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                def log_message(self,*args):pass
            server=HTTPServer(('127.0.0.1',port),Handler)
            thread=threading.Thread(target=server.handle_request,daemon=True);thread.start()
            replay=subprocess.run([self.node,str(source),str(config),'flush'],input='{}',
                capture_output=True,text=True,timeout=30)
            thread.join(5);server.server_close()
            self.assertEqual(replay.returncode,0,replay.stderr)
            self.assertTrue(json.loads(replay.stdout)['accepted'])
            self.assertEqual(received[0]['events'][0]['payload']['content'],'offline')
            self.assertEqual(list((directory/'soc-outbox-node').rglob('*.json')),[])

    def test_outbox_persists_dedupes_and_drains(self):
        if not self.node:
            self.skipTest("node runtime unavailable")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            config = directory / "cfg.json"
            token = directory / "token"
            token.write_text("k")
            config.write_text(json.dumps({"backend_url": "http://127.0.0.1:9/", "instance_id": "tst:1", "token_file": str(token)}))
            outbox = str(directory / "soc-outbox.jsonl")
            header = (HEADER
                + 'const LOG_PATH="' + str(directory) + '/x.jsonl";' + NL
                + 'const CONTROL_CONFIG="' + str(config) + '";' + NL
            )
            driver = (
                'const sentBatches=[];let fetchImpl=async()=>({ok:false,status:503});' + NL
                + installer._EVENT_HELPER
                    .replace('if (!socEventTimer) socEventTimer = setTimeout(flushSocEvents, 50);', 'void flushSocEvents();')
                    .replace('if (socOutboxRead().length && !socEventTimer) socEventTimer = setTimeout(flushSocEvents, 1000);', '')
                    .replace('const response = await fetch(', 'const response = await fetchImpl(')
                + 'const nl=String.fromCharCode(10);const multiline="line1"+nl+"line2";' + NL
                + 'forwardSocEvent({event:"user.input",timestamp:"t",content:multiline});' + NL
                + 'await new Promise(r=>setTimeout(r,80));' + NL
                + 'const lines=readFileSync("' + outbox + '","utf8").split(nl).filter(Boolean);' + NL
                + 'if(lines.length!==1)throw new Error("NDJSON split failed: "+lines.length);' + NL
                + 'JSON.parse(lines[0]);' + NL
                + 'forwardSocEvent({event:"user.input",timestamp:"t",content:multiline});' + NL
                + 'await new Promise(r=>setTimeout(r,50));' + NL
                + 'const again=readFileSync("' + outbox + '","utf8").split(nl).filter(Boolean);' + NL
                + 'if(again.length!==1)throw new Error("dedupe failed: "+again.length);' + NL
                + 'fetchImpl=async(u,o)=>{sentBatches.push({url:u,body:JSON.parse(o.body)});return {ok:true,status:200};};' + NL
                + 'await flushSocEvents();' + NL
                + 'if(readFileSync("' + outbox + '","utf8").trim())throw new Error("outbox not drained");' + NL
                + 'if(sentBatches[0].body.events[0].payload.content!==multiline)throw new Error("content corrupted");' + NL
                + 'if(!sentBatches[0].url.endsWith("/api/asg/events"))throw new Error("url wrong");' + NL)
            script = directory / "driver.mjs"
            script.write_text(header + driver)
            done = subprocess.run([self.node, str(script)], capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)

if __name__ == "__main__":
    unittest.main()
