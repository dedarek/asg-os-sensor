"""Run local authenticated collector -> HTTP receiver -> SOC Vue preview.

Explicit --source files seed learned paths; no current-instance binding is inferred.
This localhost receiver is not the deployed SOC gateway or its frozen P0 protocol.
"""
import argparse
import hmac
import json
import secrets
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .collector import collect, digest
from .store import Store
from .packages import package

def discover_sources(root, since):
    """Consume newly identified Agent findings, never infrastructure or guesses."""
    for path in Path(root).glob('*/investigation_findings.json'):
        try:
            if path.stat().st_mtime < since:
                continue
            data = json.loads(path.read_text())
            identity = data.get('findings', {}).get('identity', {})
            value = identity.get('value') or {}
            target = data.get('target') or {}
            if identity.get('status') != 'identified' or 'agent' not in value.get('roles', []):
                continue
            if not target.get('pid') or not target.get('create_time'):
                continue
            yield 'instance-' + digest([target['pid'],target['create_time']])[:20], str(path)
        except (OSError, ValueError, TypeError, AttributeError):
            continue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', action='append', required=True)
    parser.add_argument('--port', type=int, default=8092)
    parser.add_argument('--interval', type=int, default=60)
    parser.add_argument('--db', default='artifacts/soc-inventory-preview.sqlite')
    parser.add_argument('--watch-root', help='Watch new ASG investigation results after startup')
    parser.add_argument('--hooks-root', help='Trusted SOC hooks source tree for local package builds')
    args = parser.parse_args()
    Path(args.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(args.db)
    key = secrets.token_urlsafe(32)
    errors = {}
    refresh = threading.Event()
    allowed = {'local-' + digest(str(Path(p).resolve()))[:12]: p for p in args.source}
    if args.watch_root:
        watch_root = Path(args.watch_root).resolve()
        for agent, source in store.sources().items():
            if watch_root in Path(source).resolve().parents:
                allowed[agent] = source
    started = time.time()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, status, payload):
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path == '/api/agent-inventory':
                return self.send(200, {'reports':[store.inventory(a['id']) for a in store.agents()]})
            def record(agent):
                runtime_state='unknown'
                target=store.inventory(agent['id']).get('source_target',{})
                try:
                    import psutil
                    process=psutil.Process(int(target['pid']))
                    runtime_state='running' if abs(process.create_time()-float(target['create_time']))<0.1 else 'exited'
                except (KeyError,ValueError,TypeError,ImportError):pass
                except Exception as exc:
                    runtime_state='exited' if type(exc).__name__=='NoSuchProcess' else 'unknown'
                return {**agent, 'agent_id':agent['id'], 'type':agent['type_id'], 'type_name':agent['name'],
                        'process_state':runtime_state,
                        'status':'unknown', 'application_name':'本地资源预览 · 历史调查路径',
                        'registered':True, 'registered_at':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(agent['first_seen'])),
                        'observe_mode':True}
            if path == '/api/admin/artifact-platforms':
                return self.send(200, {'platforms':store.platforms(),'merge_token':key})
            if path.startswith('/api/admin/artifacts/hooks/') and path.endswith('/download'):
                parts=path.split('/')
                if len(parts)!=8 or not args.hooks_root:return self.send(404,{})
                built=package(args.hooks_root,store.resolve_type(parts[5]),Path(args.db).parent/'soc-hook-packages')
                if not built or built[0]['id']!=parts[6]:return self.send(404,{})
                data=built[1].read_bytes();self.send_response(200)
                self.send_header('Content-Type','application/gzip')
                self.send_header('Content-Disposition','attachment; filename="'+built[0]['file_name']+'"')
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if path.startswith('/api/admin/artifacts/hooks/') and len(path.split('/')) == 6:
                if path.split('/')[-1] not in {p['id'] for p in store.platforms()}:
                    return self.send(404, {})
                built=package(args.hooks_root,store.resolve_type(path.split('/')[-1]),Path(args.db).parent/'soc-hook-packages') if args.hooks_root else None
                return self.send(200, {'items':[built[0]] if built else [], 'total':1 if built else 0, 'status':'source_built' if built else 'no_integration_source'})
            if path == '/api/agent/status':
                agents = [record(a) for a in store.agents()]
                return self.send(200, {'agents':agents, 'total':len(agents)})
            if path == '/api/admin/applications/options':
                return self.send(200, {'applications':[]})
            if path.startswith('/api/agent/') and len(path.split('/')) == 4:
                agent = next((a for a in store.agents() if a['id']==path.split('/')[3]), None)
                return self.send(200, record(agent)) if agent else self.send(404, {})
            if path.endswith('/skill-tree'):
                return self.send(200, {'versions':[], 'collection_status':'not_collected'})
            if path.endswith('/skill-risks'):
                return self.send(200, {'skill_risks':[], 'collection_status':'not_collected'})
            if path == '/api/inventory-preview/agents':
                return self.send(200, {'agents': store.agents(), 'errors': errors, 'mode': 'local-preview'})
            if path.startswith('/api/agent/') and path.endswith('/inventory'):
                try:
                    return self.send(200, store.inventory(path.split('/')[3]))
                except KeyError:
                    return self.send(404, {'message': 'Unknown local preview agent'})
            return self.send(404, {})

        def do_POST(self):
            if self.path in ('/api/admin/artifact-platforms/merge','/api/admin/artifact-platforms/unmerge'):
                if not hmac.compare_digest(self.headers.get('X-ASG-Preview-Token',''),key):return self.send(403,{})
                try:
                    size=int(self.headers.get('Content-Length','0'))
                    if not 0<size<8192:return self.send(400,{})
                    body=json.loads(self.rfile.read(size))
                    if self.path.endswith('/unmerge'):store.undo_merge(body['source'])
                    else:store.merge_type(body['source'],body['target'],str(body.get('reason','user'))[:500])
                    return self.send(200,{'ok':True})
                except (ValueError,KeyError,TypeError) as exc:return self.send(400,{'message':str(exc)})
            if self.path != '/api/asg-inventory-preview':
                return self.send(404, {})
            if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + key):
                return self.send(401, {'message': 'Unauthorized'})
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if size <= 0 or size > 2 * 1024 * 1024:
                    return self.send(413, {})
                report = json.loads(self.rfile.read(size))
                if report['agent_id'] not in allowed:
                    return self.send(403, {})
                return self.send(200, store.accept(report))
            except (ValueError, KeyError, TypeError):
                return self.send(400, {'message':'Invalid inventory'})

    def poll():
        while True:
            if args.watch_root:
                for agent, source in discover_sources(args.watch_root, started):
                    if source not in allowed.values():
                        allowed[agent] = source
            for agent, source in list(allowed.items()):
                try:
                    report = collect(source, agent)
                    request = urllib.request.Request(f'http://127.0.0.1:{args.port}/api/asg-inventory-preview',
                        data=json.dumps(report, ensure_ascii=False).encode(),
                        headers={'Authorization':'Bearer '+key, 'Content-Type':'application/json'})
                    with urllib.request.urlopen(request, timeout=15) as response:
                        json.load(response)
                    errors.pop(agent, None)
                except Exception as exc:
                    errors[agent] = type(exc).__name__
            refresh.wait(max(10, args.interval))
            refresh.clear()

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    threading.Thread(target=poll, daemon=True).start()
    print(f'ASG inventory local receiver: http://127.0.0.1:{args.port}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
