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
        CREATE TABLE IF NOT EXISTS dead_letter(id INTEGER,body BLOB NOT NULL,path TEXT,reason TEXT,at TEXT);
        CREATE TABLE IF NOT EXISTS poison_attempts(id INTEGER PRIMARY KEY,n INTEGER NOT NULL);
        ''')
        self.db.execute('CREATE TABLE IF NOT EXISTS collection_requests(id INTEGER PRIMARY KEY AUTOINCREMENT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS pending_discoveries(instance TEXT PRIMARY KEY,configuration TEXT NOT NULL,envelope BLOB NOT NULL)')
        self.agents={}
        self.health_file=root/'soc-health.json';self.beat_file=root/'loop-beat.json'
        self.reload_agents()
    def reload_agents(self):
        """Rebuild in-memory enrollments from durable state; retire ghosts.

        Crazytest B14: when enrollment dedup replaces an identity (an old card
        is superseded by re-registration), the removed row must disappear from
        every running loop too. A merge-only dictionary let the retired agent
        keep heartbeating and reporting under the same live instance forever,
        so SOC saw two runtime cards for one process. Durable state (config
        plus the enrolled table) is the single source of truth; orphaned key
        files left by retired identities are deleted with their entry.
        """
        agents={a['agent_id']:a for a in self.config.get('agents',[])}
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrolled'").fetchone():
            for row in self.db.execute('SELECT configuration FROM enrolled'):
                a=json.loads(row[0]);agents[a['agent_id']]=a
        self.agents=agents
        keep={str(a.get('key_file','')) for a in agents.values()}|{a+'.key' for a in agents}
        for orphan in self.health_file.parent.glob('asg-*.key'):
            if str(orphan) in keep or orphan.name in keep:continue
            # A concurrent enrollment writes its key before committing the
            # enrolled row; a key newer than this pass must survive until the
            # next reload confirms it is truly orphaned.
            try:
                if time.time()-orphan.stat().st_mtime<300:continue
                orphan.unlink()
            except OSError:pass
    def note(self,path,value):
        # Durable single-file snapshot for the supervisor: work-loop beat and the
        # latest SOC outcome. Reachability and credential rejection stay distinct
        # states, and credential values are never written to disk here.
        try:
            tmp=path.with_name(path.name+'.tmp')
            tmp.write_text(json.dumps(value))
            os.replace(tmp,path)
        except OSError:pass
    def soc_note(self,state,detail=''):
        self.note(self.health_file,{'checked_at':time.time(),'pid':os.getpid(),'state':state,'detail':str(detail)[:200]})
    def retire_local(self,agent_id):
        """Crazytest B17: SOC reported this identity as retired (superseded).

        A retired card never returns to online by design, so continuing to
        heartbeat or report runtime for it only refreshes a closed record and
        makes the platform look inconsistent. Drop the local enrollment (and
        its per-instance receipts/key) so every loop stops speaking for the
        retired card. Discovery independently decides whether the live process
        warrants a fresh identity from current evidence.
        """
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrolled'").fetchone():
            with self.db:
                for inst,cfg in self.db.execute('SELECT instance,configuration FROM enrolled').fetchall():
                    try:
                        if json.loads(cfg).get('agent_id')!=agent_id:continue
                    except ValueError:continue
                    self.db.execute('DELETE FROM enrolled WHERE instance=?',(inst,))
                    for table in ('soc_onboarding','reported_protocol_packages'):
                        try:self.db.execute('DELETE FROM '+table+' WHERE instance=?',(inst,))
                        except Exception:pass
        self.reload_agents()
        try:(self.health_file.parent/(str(agent_id)+'.key')).unlink()
        except OSError:pass
        LOG.warning('Local enrollment retired after SOC verdict: %s',agent_id)
    def beat(self):
        self.note(self.beat_file,{'pid':os.getpid(),'t':time.time()})
    def request(self,agent,path,data,method='POST'):
        key=os.environ.get(agent.get('key_env','')) or Path(agent['key_file']).expanduser().read_text().strip()
        req=Request(self.url+path,data=data,method=method,headers={'Authorization':'Bearer '+key,'Content-Type':'application/octet-stream' if method=='PUT' else 'application/json'})
        opener=build_opener(ProxyHandler({})) if urlsplit(self.url).hostname in ('127.0.0.1','localhost','::1') else build_opener()
        try:
            with opener.open(req,timeout=30) as response:
                payload=json.load(response)
                # A completed inventory exchange clears any earlier rejected/
                # error state. Only the inventory channel marks ok: the runtime
                # bridge probes commands and must not overwrite SOC health.
                if path.startswith('/api/inventory') or path in ('/api/asg/enroll','/api/asg/self'):
                    self.soc_note('ok','HTTP %s %s'%(getattr(response,'status',200),path))
                return payload
        except HTTPError as exc:
            self.soc_note('rejected' if exc.code in (401,403) else 'error','HTTP %s %s'%(exc.code,path));raise
        except OSError as exc:
            self.soc_note('error',type(exc).__name__+' '+path);raise
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
                    # Crazytest B10: a message the platform keeps rejecting
                    # (4xx permanent refusal) must not block every newer report
                    # for this agent forever. Count refusals; after three, move
                    # the poison message to dead_letter (kept for audit) and
                    # continue with the rest of the queue. 5xx and network
                    # errors stay head-of-line: those are transient.
                    if 400 <= int(e.code) < 500 and e.code not in (408, 429):
                        with self.db:
                            n = (self.db.execute('SELECT n FROM poison_attempts WHERE id=?',(identifier,)).fetchone() or (0,))[0] + 1
                            if n >= 3:
                                self.db.execute('INSERT OR REPLACE INTO dead_letter VALUES(?,?,?,?,?)',
                                                (identifier, body, path, 'http_' + str(e.code), time.strftime('%Y-%m-%dT%H:%M:%S')))
                                self.db.execute('DELETE FROM queue WHERE id=?',(identifier,))
                                self.db.execute('DELETE FROM poison_attempts WHERE id=?',(identifier,))
                                LOG.warning('SOC report dead-lettered after %s refusals: status=%s route=%s', n, e.code, path)
                                continue
                            self.db.execute('INSERT OR REPLACE INTO poison_attempts VALUES(?,?)',(identifier,n))
                        LOG.warning('SOC report retained (%s/3): status=%s route=%s', n, e.code, path)
                    else:
                        LOG.warning('SOC report retained: status=%s route=%s',e.code,path)
                    break
                except OSError as e:LOG.warning('SOC report retained: %s',type(e).__name__);break
                with self.db:self.db.execute('DELETE FROM queue WHERE id=?',(identifier,))
    def enqueue_cached_drafts(self):
        """Turn already-captured first snapshots into durable upload tasks."""
        failed=False
        for agent in list(self.agents.values()):
            identity,_=self.stream_identity(agent)
            if not self.db.execute('SELECT 1 FROM drafts WHERE id=?',(identity,)).fetchone():continue
            try:self.enqueue(agent)
            except (OSError,ValueError,KeyError) as e:
                failed=True
                LOG.warning('Cached discovery pending: %s',type(e).__name__)
        self.flush()
        return not failed
    def run(self,once=False):
        discovery=None
        if self.config.get('discovery'):
            from .discovery import Discovery
            discovery=Discovery(self)
            # Prove that this process, with the configured enrollment identity,
            # can reach SOC.  A health file from a previous endpoint PID is not
            # current evidence.  Deep asset collection may be manual, but
            # connectivity status must become truthful immediately after a
            # service restart.
            provision={'key_file':self.config['discovery']['application_key_file']}
            try:self.request(provision,'/api/asg/self',None,'GET')
            except (OSError,ValueError,KeyError):pass
        if self.config.get('runtime_bridge') and not once:
            def runtime_loop():
                from .runtime_bridge import RuntimeBridge
                client=Endpoint(self.config)
                bridge=RuntimeBridge(client)
                while True:
                    try:
                        if self.config.get('discovery'):
                            # Authoritative rebuild: identities retired from
                            # enrolled (dedup/re-registration) must stop
                            # reporting in this thread, not linger in memory.
                            client.reload_agents()
                        bridge.run_once()
                    except (OSError,ValueError,KeyError) as exc:LOG.warning('Runtime report pending: %s',type(exc).__name__)
                    self.note(self.health_file.parent/'bridge-beat.json',{'pid':os.getpid(),'t':time.time()})
                    time.sleep(15)
            threading.Thread(target=runtime_loop,daemon=True).start()
        next_collect={}
        next_heartbeat={}
        while True:
            self.beat()
            manual=self.config.get('collection_mode')=='manual' and not once
            if manual:
                pending=self.db.execute('SELECT id FROM collection_requests ORDER BY id LIMIT 1').fetchone()
                if pending:
                    with self.db:self.db.execute('DELETE FROM collection_requests WHERE id=?',(pending[0],))
                    next_collect={};manual=False
            collection_failed=False
            active=set(self.agents)
            if discovery:
                # Lifecycle discovery, registration and cached-report replay run
                # in every mode: manual only gates deep asset collection, never
                # finding/enrolling new instances or retrying queued receipts.
                try:
                    discovered=set(discovery.refresh())
                    # Crazytest B14: superseded identities must leave every
                    # loop; refresh only adds, so sync against durable truth
                    # and drop discovery results that no longer exist locally.
                    self.reload_agents()
                    discovered &= set(self.agents)
                    active=discovered | {a['agent_id'] for a in self.config.get('agents',[])}
                    discovery_pending=self.db.execute('SELECT count(*) FROM pending_discoveries').fetchone()[0]
                except (OSError,ValueError,KeyError) as e:
                    active=set(self.agents);discovered=set();discovery_pending=1
                    collection_failed=True
                    LOG.warning('Discovery pending: %s',type(e).__name__)
                # Online means this Agent was found in the *current* process
                # snapshot. It does not imply its Hook is installed or loaded.
                # Asset collection remains manual; this lightweight status
                # heartbeat uses the SOC gateway's existing Agent contract.
                for agent_id in discovered:
                    agent=self.agents.get(agent_id)
                    if not agent or time.monotonic()<next_heartbeat.get(agent_id,0):continue
                    try:
                        payload=self.request(agent,'/api/heartbeat',canonical({
                            'agent_id':agent_id,'agent_name':agent.get('name') or agent_id,
                            'platform':agent['platform'],'status':'online',
                            'timestamp':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())}))
                        next_heartbeat[agent_id]=time.monotonic()+60
                    except (OSError,ValueError,KeyError) as e:
                        LOG.warning('Agent heartbeat pending: %s',type(e).__name__)
                    # Crazytest B17: the gateway echoes the stored card; a
                    # retired verdict means this enrollment was superseded on
                    # the platform and the local loop must stop for it.
                    else:
                        if isinstance(payload,dict) and (payload.get('data') or {}).get('status')=='retired':
                            self.retire_local(agent_id)
                self.flush()
                if manual:
                    # Registration captures a bounded first snapshot before any
                    # network dependency.  Reporting that already-cached draft
                    # is lifecycle delivery, not a new deep scan, so it must not
                    # wait for a user collection click.  This also replays a new
                    # Agent first seen while SOC was offline even if the process
                    # exited before connectivity returned.
                    if not self.enqueue_cached_drafts():collection_failed=True
                if not manual:
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
                    if discovery_pending:collection_failed=True
                    if once:return not collection_failed and self.db.execute("SELECT count(*) FROM queue").fetchone()[0] == 0 and self.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
                    time.sleep(15)
                    continue
                if discovery_pending:collection_failed=True
                if once:return not collection_failed and self.db.execute("SELECT count(*) FROM queue").fetchone()[0] == 0 and self.db.execute("SELECT count(*) FROM pending_discoveries").fetchone()[0] == 0 and self.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
                time.sleep(15)
                continue
            if manual:
                if not self.enqueue_cached_drafts():collection_failed=True
                time.sleep(2)
                continue
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
            if once:return not collection_failed and self.db.execute("SELECT count(*) FROM queue").fetchone()[0] == 0 and self.db.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
            time.sleep(15 if discovery else max(10,self.config.get('interval_seconds',600))+random.uniform(0,5))


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--config',required=True);parser.add_argument('--once',action='store_true');args=parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    ok=Endpoint(json.loads(Path(args.config).expanduser().read_text())).run(args.once)
    if args.once and not ok:raise SystemExit(1)
if __name__=='__main__':main()
