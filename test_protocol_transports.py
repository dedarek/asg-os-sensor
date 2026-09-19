"""Protocol-family conformance checks, not real-product acceptance claims."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import psutil
from runtime import protocol_discovery as discovery, otlp_ingest as otlp
from runtime.acp_bridge import Recorder, permission_response
from runtime.observation_registry import Registry


class ProtocolTransportTests(unittest.TestCase):
    def test_jsonc_toml_and_linked_configs_without_product_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config.toml').write_text('[hooks]\nfiles=["hooks.jsonc"]\n[otel]\nexporter="otlp-http"\n', encoding='utf-8')
            (root/'hooks.jsonc').write_text('// comment\n{"hooks":{"PreToolUse":[{"hooks":[{"type":"command","command":"existing"}]}]},}', encoding='utf-8')
            result = discovery.inspect_paths([root/'config.toml'])
            self.assertEqual({c['family'] for c in result['candidates']}, {'command_hooks','otel'})
            self.assertEqual(len(result['checked_paths']), 2)
            self.assertFalse(result['errors'])
            self.assertNotIn('existing', json.dumps(result))

    def test_secret_files_and_reference_cycles_are_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); path=root/'config.json'
            path.write_text(json.dumps({'hooks':{'files':['config.json','auth.json']}}))
            (root/'auth.json').write_text('{"secret":"sensitive"}')
            result=discovery.inspect_paths([path])
            self.assertEqual(result['checked_paths'],[str(path.resolve())])

    def test_acp_real_subprocess_bridge_and_complete_stream(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); peer=root/'peer.py'
            peer.write_text('''import json,sys
for line in sys.stdin:
 m=json.loads(line)
 if m['method']=='initialize':
  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'protocolVersion':1,'agentCapabilities':{}}}),flush=True)
 else:
  for text in ('测试',' response'):
   print(json.dumps({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':text}}}}),flush=True)
  print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':{'stopReason':'end_turn'}}),flush=True)
''', encoding='utf-8')
            rows=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':1}},
                  {'jsonrpc':'2.0','id':2,'method':'session/prompt','params':{'sessionId':'s','prompt':[{'type':'text','text':'输入'}]}}]
            env={**os.environ,'PYTHONUTF8':'1'}
            result=subprocess.run([sys.executable,str(Path(__file__).parent/'runtime/acp_bridge.py'),'--run-dir',str(root/'run'),'--',sys.executable,str(peer)],
                input=''.join(json.dumps(r)+'\n' for r in rows),capture_output=True,text=True,encoding='utf-8',env=env,timeout=15,cwd=root)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(len(result.stdout.splitlines()),4)
            records=[json.loads(s) for s in next((root/'run/acp').glob('*/events.jsonl')).read_text(encoding='utf-8').splitlines()]
            self.assertTrue(any(r['event']=='hook.loaded' for r in records))
            answer=next(r for r in records if r['event']=='assistant.output')
            self.assertEqual(answer['content'],'测试 response');self.assertTrue(answer['content_complete'])
            bindings=json.loads((root/'run/observations.json').read_text())
            self.assertEqual(len(bindings),1)
            self.assertNotIn('--',next(iter(bindings))) # visible to primary instance status/selfcheck

    def test_acp_cancel_is_not_complete_and_permission_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            rec=Recorder({'pid':1,'create_time':1},Path(tmp)/'events.jsonl')
            rec.observe({'id':1,'method':'session/prompt','params':{'sessionId':'s'}},'client')
            rec.observe({'method':'session/update','params':{'sessionId':'s','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'partial'}}}},'agent')
            rec.observe({'id':1,'result':{'stopReason':'cancelled'}},'agent')
            rows=[json.loads(s) for s in rec.log_path.read_text().splitlines()]
            self.assertFalse(next(r for r in rows if r['event']=='assistant.output')['content_complete'])
        msg={'id':9,'params':{'options':[{'optionId':'no','kind':'reject_once'}]}}
        self.assertEqual(permission_response(msg,'deny')['result']['outcome']['optionId'],'no')
        self.assertEqual(permission_response({'id':9},'deny')['result']['outcome']['outcome'],'cancelled')

    def test_otlp_json_protobuf_binding_and_genai_content(self):
        from google.protobuf.json_format import ParseDict
        from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve(); target={'pid':os.getpid(),'create_time':psutil.Process().create_time()}
            binding=root/'binding.json'; log=root/'events.jsonl'
            binding.write_text(json.dumps({'target':target,'log_path':str(log),'fields':{'event':'event','pid':'pid','timestamp':'timestamp'}}))
            Registry(root).register(binding,target)
            attrs=[{'key':'process.pid','value':{'intValue':str(target['pid'])}},
                   {'key':'gen_ai.input.messages','value':{'stringValue':'[{"role":"user","parts":[{"type":"text","content":"hi"}]}]'}},
                   {'key':'gen_ai.output.messages','value':{'stringValue':'[{"role":"assistant","parts":[{"type":"text","content":"hello"}]}]'}}]
            payload={'resourceSpans':[{'resource':{'attributes':attrs},'scopeSpans':[{'spans':[{'name':'chat','endTimeUnixNano':str(time.time_ns())}]}]}]}
            raw=ParseDict(payload,ExportTraceServiceRequest()).SerializeToString()
            decoded=otlp.decode(raw,'traces','application/x-protobuf')
            self.assertEqual(otlp.ingest(decoded,'traces',root),(1,0))
            self.assertEqual(otlp.ingest(payload,'traces',root),(1,0)) # multiple bindings same instance are not ambiguous
            rows=[json.loads(s) for s in next((root/'otel').glob('*/events.jsonl')).read_text().splitlines()]
            self.assertEqual({r['event'] for r in rows},{'otel.span','model.request','model.response'})
            self.assertTrue(all(not r['content_complete'] and not r['control_supported'] for r in rows))
            attrs[0]['value']['intValue']='99999999'
            self.assertEqual(otlp.ingest(payload,'traces',root),(0,1))

    def test_otlp_real_http_requires_auth_and_returns_protobuf(self):
        from http.server import ThreadingHTTPServer
        import threading, urllib.request, urllib.error
        from runtime.hook_service import Handler
        from runtime.hook_control import token_path
        from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest, ExportLogsServiceResponse
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'ASG_RUN_DIR':tmp}):
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            url='http://127.0.0.1:%s/v1/logs' % server.server_port
            try:
                request=urllib.request.Request(url,data=b'{}',headers={'Content-Type':'application/json'})
                with self.assertRaises(urllib.error.HTTPError) as denied: urllib.request.urlopen(request)
                self.assertEqual(denied.exception.code,403)
                # A resource with no log records is a valid empty OTLP export.
                message=ExportLogsServiceRequest();message.resource_logs.add()
                request=urllib.request.Request(url,data=message.SerializeToString(),headers={'Content-Type':'application/x-protobuf','Authorization':'Bearer '+token_path().read_text().strip()})
                with urllib.request.urlopen(request) as response:
                    self.assertEqual(response.status,200)
                    self.assertEqual(response.headers['Content-Type'],'application/x-protobuf')
                    ExportLogsServiceResponse().ParseFromString(response.read())
            finally: server.shutdown();server.server_close();thread.join(timeout=5)

    def test_acp_config_wrap_preserves_launch_arguments_and_rolls_back(self):
        from runtime.protocol_fastpath import install_acp
        from runtime.learned_install import rollback
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp).resolve(); root=base/'workspace'; root.mkdir(); path=root/'config.json'
            original=json.dumps({'agent_servers':{'unknown':{'command':'agent.exe','args':['--acp'],'env':{'MODE':'local'}}},'other':7})
            path.write_text(original)
            target={'pid':os.getpid(),'create_time':psutil.Process().create_time()}
            with patch.dict(os.environ,{'ASG_ONBOARDING_AUTO_INSTALL':'1','ASG_ONBOARDING_AUTHORIZED':'1','ASG_ONBOARDING_SCOPE':'project','ASG_ONBOARDING_WORKSPACE_ROOTS':str(root)}):
                result=install_acp(path,target,base/'run',base/'control.json')
            self.assertEqual(result['status'],'installed')
            entry=json.loads(path.read_text())['agent_servers']['unknown']
            self.assertEqual(entry['args'][-2:],['agent.exe','--acp']);self.assertEqual(entry['env'],{'MODE':'local'})
            rollback(root,Path(result['state_dir']),approved_workspace=root,approved_digest=result['plan_digest'])
            self.assertEqual(path.read_text(),original)


if __name__=='__main__': unittest.main()
