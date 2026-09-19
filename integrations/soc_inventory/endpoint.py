"""Independent endpoint service: durable outbox -> SOC gateway; no HTTP listener."""
import argparse
import copy
import io
import json
import logging
import os
import random
import threading
import sqlite3
import stat
import time
import uuid
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen, build_opener, ProxyHandler
from urllib.error import HTTPError
from urllib.parse import urlsplit
from .protocol import canonical, sha, collect_contract, workspace_identity

LOG=logging.getLogger('asg.soc.endpoint')


def parts_for(envelope):
    flat=[]
    for category,value in envelope['categories'].items():
        for scope in value.get('scopes',[]):
            items=scope['items']
            for start in range(0,max(1,len(items)),100):
                chunk={**scope,'items':items[start:start+100]};chunk['expected_count']=len(chunk['items'])
                flat.append((category,chunk))
    groups=[flat[n:n+1] for n in range(0,len(flat),1)] or [[]]
    if len(groups)>100:raise ValueError('INVENTORY_SNAPSHOT_LIMIT')
    payloads=[]
    for i,group in enumerate(groups):
        part={**envelope,'part_index':i,'part_count':len(groups),'categories':{}}
        for name,value in envelope['categories'].items():part['categories'][name]={'status':value['status'],**({'scopes':[]} if value['status']!='unsupported' else {})}
        for name,scope in group:part['categories'][name]['scopes'].append(scope)
        data=canonical(part)
        if len(data)>2*1024*1024:raise ValueError('INVENTORY_PART_TOO_LARGE')
        payloads.append(data)
    complete={'snapshot_id':envelope['snapshot_id'],'part_count':len(payloads),
        'parts':[{'part_index':i,'sha256':sha(data)} for i,data in enumerate(payloads)],
        'category_totals':{key:[{'scope_key':s['scope_key'],'expected_count':len(s['items'])} for s in value.get('scopes',[])] for key,value in envelope['categories'].items() if value['status']!='unsupported'}}
    return payloads,canonical(complete)


def pack_skill(folder):
    folder=Path(folder).resolve();manifest=[];files=[];total=0
    for base,dirs,names in os.walk(folder,followlinks=False):
        for name in dirs+names:
            if (Path(base)/name).is_symlink():raise ValueError('SKILL_SYMLINK_UNSUPPORTED')
        for name in names:
            p=Path(base)/name
            if not stat.S_ISREG(p.stat().st_mode):raise ValueError('SKILL_NON_REGULAR_FILE')
            with p.open('rb') as source:data=source.read(50*1024*1024-total+1)
            total+=len(data)
            if total>50*1024*1024 or len(files)>=10000:raise ValueError('SKILL_PACKAGE_LIMIT')
            relative=p.relative_to(folder).as_posix();mode=p.stat().st_mode&0o111
            manifest.append([relative,'file',mode,sha(data)]);files.append((relative,mode,data))
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for relative,mode,data in sorted(files):
            entry=zipfile.ZipInfo(relative,(1980,1,1,0,0,0));entry.create_system=3;entry.external_attr=(stat.S_IFREG|0o644|mode)<<16;entry.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(entry,data)
    data=output.getvalue()
    if len(data)>50*1024*1024:raise ValueError('SKILL_PACKAGE_LIMIT')
    return data,sha(canonical(sorted(manifest)))


class Endpoint:
    def __init__(self,configuration):
        self.config=configuration;self.url=configuration['backend_url'].rstrip('/')
        url=urlsplit(self.url)
        if url.scheme!='https' and not(url.scheme=='http' and url.hostname in ('127.0.0.1','localhost','::1')):
            raise ValueError('Remote backend_url requires HTTPS')
        root=Path(configuration['state_dir']).expanduser();root.mkdir(parents=True,exist_ok=True)
        if os.name!='nt':root.chmod(0o700)
        self.db=sqlite3.connect(root/'outbox.sqlite');self.db.executescript('''
        CREATE TABLE IF NOT EXISTS streams(id TEXT PRIMARY KEY,epoch TEXT,revision INTEGER);
        CREATE TABLE IF NOT EXISTS queue(id INTEGER PRIMARY KEY,agent TEXT NOT NULL,path TEXT NOT NULL,method TEXT NOT NULL,body BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,body BLOB NOT NULL);
        CREATE TABLE IF NOT EXISTS known_scopes(id TEXT,scope TEXT,PRIMARY KEY(id,scope));
        CREATE TABLE IF NOT EXISTS packages(agent TEXT,installation TEXT,digest TEXT,PRIMARY KEY(agent,installation));
        ''')
        self.agents={a['agent_id']:a for a in configuration.get('agents',[])}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrolled'").fetchone():
            for row in self.db.execute('SELECT configuration FROM enrolled'):
                a=json.loads(row[0]);self.agents[a['agent_id']]=a
    def request(self,agent,path,data,method='POST'):
        key=os.environ.get(agent.get('key_env','')) or Path(agent['key_file']).expanduser().read_text().strip()
        req=Request(self.url+path,data=data,method=method,headers={'Authorization':'Bearer '+key,'Content-Type':'application/octet-stream' if method=='PUT' else 'application/json'})
        opener=build_opener(ProxyHandler({})) if urlsplit(self.url).hostname in ('127.0.0.1','localhost','::1') else build_opener()
        with opener.open(req,timeout=30) as response:return json.load(response)
    def stream_identity(self,agent):
        workspace=workspace_identity(agent,agent.get('collection_home'),agent.get('collection_environment'))
        return sha(canonical([agent['agent_id'],'soc-inventory-'+agent['platform'],workspace])),workspace
    def stream(self,agent):
        collector='soc-inventory-'+agent['platform'];identity,workspace=self.stream_identity(agent)
        row=self.db.execute('SELECT epoch,revision FROM streams WHERE id=?',(identity,)).fetchone()
        if not row:
            result=self.request(agent,'/api/inventory/collectors',canonical({'agent_id':agent['agent_id'],'collector_id':collector,'workspace_id':workspace}))
            row=(result['collector_epoch'],0)
            with self.db:self.db.execute('INSERT INTO streams VALUES(?,?,?)',(identity,*row))
        return identity,row
    def enqueue(self,agent):
        if self.db.execute('SELECT 1 FROM queue WHERE agent=?',(agent['agent_id'],)).fetchone():return
        identity,_=self.stream_identity(agent)
        draft=self.db.execute('SELECT body FROM drafts WHERE id=?',(identity,)).fetchone()
        if draft:envelope=json.loads(draft[0])
        else:
            previous=[r[0] for r in self.db.execute('SELECT scope FROM known_scopes WHERE id=?',(identity,))]
            envelope=collect_contract(agent,'pending-registration',1,home=agent.get('collection_home'),env=agent.get('collection_environment'),previous_scopes=previous,bounded=True)
            with self.db:self.db.execute('INSERT OR REPLACE INTO drafts VALUES(?,?)',(identity,canonical(envelope)))
        identity,(epoch,revision)=self.stream(agent)
        envelope.update(collector_epoch=epoch,revision=revision+1)
        payloads,complete=parts_for(envelope)
        with self.db:
            for payload in payloads:self.db.execute('INSERT INTO queue(agent,path,method,body) VALUES(?,?,?,?)',(agent['agent_id'],'/api/inventory','POST',payload))
            self.db.execute('INSERT INTO queue(agent,path,method,body) VALUES(?,?,?,?)',(agent['agent_id'],'/api/inventory/'+envelope['snapshot_id']+'/complete','POST',complete))
            if self.config.get('upload_skill_content',False):
                for scope in envelope['categories']['skill']['scopes']:
                    if scope['status']=='failed':continue
                    for item in scope['items']:
                        try:
                            folder=Path(scope['scope_key'][len('skill:'):])/item['relative_path'];data,digest=pack_skill(folder)
                            prior=self.db.execute('SELECT digest FROM packages WHERE agent=? AND installation=?',(agent['agent_id'],item['installation_key'])).fetchone()
                            if prior and prior[0]==sha(data):continue
                            uid=str(uuid.uuid4());chunks=[data[i:i+1048576] for i in range(0,len(data),1048576)]
                            body={'upload_id':uid,'snapshot_id':envelope['snapshot_id'],'installation_key':item['installation_key'],'archive_sha256':sha(data),'manifest_digest':digest,'size':len(data),'part_count':len(chunks)}
                            tasks=[('/api/skill/uploads','POST',canonical(body))]+[(f'/api/skill/uploads/{uid}/parts/{i}','PUT',chunk) for i,chunk in enumerate(chunks)]+[(f'/api/skill/uploads/{uid}/complete','POST',b'{}')]
                            for path,method,body in tasks:self.db.execute('INSERT INTO queue(agent,path,method,body) VALUES(?,?,?,?)',(agent['agent_id'],path,method,body))
                            # Cache reflects durable queued bytes; failures retain queue for retry.
                            self.db.execute('INSERT OR REPLACE INTO packages VALUES(?,?,?)',(agent['agent_id'],item['installation_key'],sha(data)))
                        except (OSError,ValueError) as e:LOG.warning('Skill package pending: %s',type(e).__name__)
            self.db.execute('UPDATE streams SET revision=? WHERE id=?',(revision+1,identity))
            for category in envelope['categories'].values():
                for scope in category.get('scopes',[]):
                    self.db.execute('INSERT OR IGNORE INTO known_scopes VALUES(?,?)',(identity,scope['scope_key']))
            self.db.execute('DELETE FROM drafts WHERE id=?',(identity,))
    def flush(self):
        for agent_id,agent in self.agents.items():
            while True:
                row=self.db.execute('SELECT id,path,method,body FROM queue WHERE agent=? ORDER BY id LIMIT 1',(agent_id,)).fetchone()
                if not row:break
                identifier,path,method,body=row
                try:self.request(agent,path,body,method)
                except HTTPError as e:
                    LOG.warning('SOC report retained: status=%s route=%s',e.code,path);break
                except OSError as e:LOG.warning('SOC report retained: %s',type(e).__name__);break
                with self.db:self.db.execute('DELETE FROM queue WHERE id=?',(identifier,))
    def run(self,once=False):
        discovery=None
        if self.config.get('discovery'):
            from .discovery import Discovery
            discovery=Discovery(self)
        if self.config.get('runtime_bridge') and not once:
            def runtime_loop():
                from .runtime_bridge import RuntimeBridge
                client=Endpoint(self.config)
                bridge=RuntimeBridge(client)
                while True:
                    try:
                        if self.config.get('discovery'):
                            for row in client.db.execute('SELECT configuration FROM enrolled'):
                                a=json.loads(row[0]);client.agents[a['agent_id']]=a
                        bridge.run_once()
                    except (OSError,ValueError,KeyError) as exc:LOG.warning('Runtime report pending: %s',type(exc).__name__)
                    time.sleep(15)
            threading.Thread(target=runtime_loop,daemon=True).start()
        self.db.execute('CREATE TABLE IF NOT EXISTS collection_requests(id INTEGER PRIMARY KEY AUTOINCREMENT)')
        next_collect={}
        while True:
            if self.config.get('collection_mode')=='manual' and not once:
                pending=self.db.execute('SELECT id FROM collection_requests ORDER BY id LIMIT 1').fetchone()
                if not pending:
                    time.sleep(2)
                    continue
                with self.db:self.db.execute('DELETE FROM collection_requests WHERE id=?',(pending[0],))
                next_collect={}
            collection_failed=False
            active=set(self.agents)
            if discovery:
                try:active=set(discovery.refresh()) | {a['agent_id'] for a in self.config.get('agents',[])}
                except (OSError,ValueError,KeyError) as e:
                    active=set();collection_failed=True
                    LOG.warning('Discovery pending: %s',type(e).__name__)
            self.flush()
            for agent in list(self.agents.values()):
                identity,_=self.stream_identity(agent)
                retained=self.db.execute('SELECT 1 FROM drafts WHERE id=?',(identity,)).fetchone()
                if not retained and (agent['agent_id'] not in active or time.monotonic()<next_collect.get(agent['agent_id'],0)):continue
                try:
                    self.enqueue(agent)
                    next_collect[agent['agent_id']]=time.monotonic()+max(10,self.config.get('interval_seconds',600))
                except (OSError,ValueError,KeyError) as e:
                    collection_failed=True
                    LOG.warning('Collection pending: %s',type(e).__name__)
            self.flush()
            if discovery and self.db.execute('SELECT count(*) FROM pending_discoveries').fetchone()[0]:collection_failed=True
            if once:return not collection_failed and self.db.execute("SELECT count(*) FROM queue").fetchone()[0] == 0 and self.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
            time.sleep(15 if discovery else max(10,self.config.get('interval_seconds',600))+random.uniform(0,5))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    ok=Endpoint(json.loads(Path(args.config).expanduser().read_text())).run(args.once)
    if args.once and not ok:raise SystemExit(1)
if __name__=='__main__':main()
