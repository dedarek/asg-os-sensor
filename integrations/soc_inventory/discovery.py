"""Register live ASG candidates and confirmed instances with a provisioned SOC application.

Changed instances require a fresh matching process entrypoint and lose prior
classification and Hook evidence. Candidate names never establish Agent identity. Target environment is read locally for a bounded path allowlist.
"""
import json
import base64
import hashlib
import os
import uuid
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlsplit, urlencode
from .protocol import canonical, collect_contract

ENV_PATHS=('CODEX_HOME','OPENCODE_CONFIG_DIR','HERMES_HOME','HERMES_OPTIONAL_SKILLS_DIR','OPENCLAW_STATE_DIR')

SERVER_KEYS=('mcpServers','mcp_servers','mcp','servers')


def _servers_map(value):
    if not isinstance(value,dict) or not value:return False
    return any(isinstance(d,dict) and any(k in d for k in ('command','url','transport','type')) for d in value.values())


def first_mcp_field(data,prefix=()):
    """Locate the MCP servers block by declaration structure, not brand paths."""
    if not isinstance(data,dict):return None
    for key,value in data.items():
        if key in SERVER_KEYS:
            if _servers_map(value):return '.'.join((*prefix,key))
            if isinstance(value,dict) and _servers_map(value.get('servers')):return '.'.join((*prefix,key,'servers'))
        found=first_mcp_field(value,(*prefix,key))
        if found:return found
    return None


def learned_mcp_sources(value):
    """Turn structured investigation findings into MCP inventory scopes.

    Only explicit declaration-file paths recorded by the investigation count;
    runtime-only observations stay unscoped instead of guessing a brand path.
    The field path is verified by parsing the file, so a wrong guess reports a
    failed scope rather than pretending an empty scan succeeded.
    """
    from .protocol import config
    out=[];seen=set()
    items=value.get('items') if isinstance(value,dict) else None
    for item in items or []:
        if not isinstance(item,dict):continue
        for key in ('config_path','source_path','path'):
            raw=item.get(key)
            if not isinstance(raw,str) or not raw.strip():continue
            path=Path(raw).expanduser()
            if not path.is_absolute():continue
            path=path.resolve()
            if path in seen or path.suffix.lower() not in ('.json','.json5','.jsonc','.toml','.yaml','.yml'):continue
            try:
                if not path.is_file() or path.stat().st_size>2*1024*1024:continue
                field=first_mcp_field(config(path))
            except Exception:continue
            if field is None:continue
            seen.add(path)
            out.append({'path':str(path),'field':field})
    return out



def confirmed(state):
    for target in state.get('agents',[]):
        adapter=target.get('adapter') or {}
        classification=adapter.get('agent_classification') or {}
        if classification.get('status') not in ('pending','confirmed_agent'):continue
        if classification.get('roles') and 'agent' not in classification['roles']:continue
        instance=target.get('instance_id','');source_instance=instance;changed=False
        try:
            import psutil
            pid,created=instance.split(':',1);process=psutil.Process(int(pid))
            if int(pid)!=target['pid']:continue
            if abs(process.create_time()-float(created))>0.01:
                evidence=(target.get('identity') or {}).get('evidence')
                # Re-discover the current process, never reuse the old identity or its Hook verdict.
                if not isinstance(evidence,str) or evidence not in [process.exe(),*process.cmdline()]:continue
                instance=f'{pid}:{process.create_time()}';changed=True
                classification={'status':'pending','roles':[]}
                adapter={} 
            workspace=process.cwd()
            environment={k:v for k,v in process.environ().items() if k in ENV_PATHS}
        except (ValueError,KeyError,psutil.Error):continue
        identity=target.get('identity') or {}
        platform=str(identity.get('id') or 'unknown')
        agent={'platform':platform,'workspace':workspace,'collection_environment':environment,'asg_instance_id':instance,'source_instance_id':source_instance,'identity_refreshed':changed,
               'hook_fingerprint':((adapter.get('onboarding') or {}).get('plan') or {}).get('fingerprint_id'),'agent_version':str(identity.get('version') or target.get('version') or 'unknown'),'classification':classification.get('status','pending'),'name':target.get('name') or platform,'learned_skill_roots':[],'learned_mcp_configs':[]}
        # Only explicit resource paths in structured evidence, no prose extraction.
        assets=adapter.get('assets') or {}
        skills=(assets.get('skills') or {}).get('value') or {}
        if isinstance(skills,dict):
            for item in skills.get('items',[]):
                path=item.get('path') if isinstance(item,dict) else None
                if isinstance(path,str) and Path(path).is_absolute():
                    p=Path(path);agent['learned_skill_roots'].append(str(p.parent if p.name=='SKILL.md' else p))
        mcp=(assets.get('registered_tools_and_mcp') or assets.get('mcp') or {}).get('value') or {}
        if isinstance(mcp,dict):agent['learned_mcp_configs']=learned_mcp_sources(mcp)
        yield agent

class Discovery:
    def __init__(self,endpoint):
        self.endpoint=endpoint;self.options=endpoint.config['discovery']
        self.base=endpoint.config.get('asg_url','http://127.0.0.1:8081').rstrip('/')
        parsed=urlsplit(self.base)
        if parsed.scheme!='http' or parsed.hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('ASG must be loopback')
        root=Path(endpoint.config['state_dir']).expanduser();self.root=root
        self.host=root/'host-id'
        if not self.host.exists():self.host.write_text(str(uuid.uuid4()))
        endpoint.db.execute('CREATE TABLE IF NOT EXISTS enrolled(instance TEXT PRIMARY KEY,configuration TEXT NOT NULL)')
        endpoint.db.execute('CREATE TABLE IF NOT EXISTS pending_discoveries(instance TEXT PRIMARY KEY,configuration TEXT NOT NULL,envelope BLOB NOT NULL)')
        endpoint.db.execute('CREATE TABLE IF NOT EXISTS soc_onboarding(instance TEXT PRIMARY KEY,result TEXT)')
    def refresh(self):
        try:
            with build_opener(ProxyHandler({})).open(self.base+'/api/state',timeout=15) as r:state=json.load(r)
        except OSError:state={'agents':[]}
        found=[];current=list(confirmed(state))
        # Capture before the first network enrollment. An exited instance's
        # retained snapshot remains historical evidence, never a live heartbeat.
        for agent in current:
            instance=agent['asg_instance_id']
            if self.endpoint.db.execute('SELECT 1 FROM enrolled WHERE instance=?',(instance,)).fetchone():continue
            # Same live process, drifted identity stamp: macOS create_time reads
            # can flap by ~1s, which would double-enroll one process, duplicate
            # SOC cards and strand queued commands. Transfer the enrollment and
            # onboarding receipt instead (pid match within 2s).
            try:
                dpi,dct=instance.split(':',1);dct=float(dct)
                match=next((row for row in self.endpoint.db.execute('SELECT instance,configuration FROM enrolled').fetchall()
                    if row[0].split(':',1)[0]==dpi and len(row[0].split(':',1))==2
                    and row[0].split(':',1)[1].replace('.','',1).isdigit()
                    and abs(float(row[0].split(':',1)[1])-dct)<=2.0),None)
            except ValueError:
                match=None
            if match:
                prior=json.loads(match[1])
                prior['asg_instance_id']=instance;prior['identity_refreshed']=True
                with self.endpoint.db:
                    self.endpoint.db.execute('DELETE FROM enrolled WHERE instance=?',(match[0],))
                    self.endpoint.db.execute('INSERT OR REPLACE INTO enrolled VALUES(?,?)',(instance,json.dumps(prior)))
                    row=self.endpoint.db.execute('SELECT result FROM soc_onboarding WHERE instance=?',(match[0],)).fetchone()
                    if row:
                        self.endpoint.db.execute('DELETE FROM soc_onboarding WHERE instance=?',(match[0],))
                        self.endpoint.db.execute('INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)',(instance,row[0]))
                continue
            if self.endpoint.db.execute('SELECT 1 FROM pending_discoveries WHERE instance=?',(instance,)).fetchone():continue
            envelope=collect_contract({**agent,'agent_id':'pending-enrollment'},'pending-registration',1,
                env=agent.get('collection_environment'),bounded=True)
            with self.endpoint.db:self.endpoint.db.execute('INSERT OR IGNORE INTO pending_discoveries VALUES(?,?,?)',(instance,json.dumps(agent),canonical(envelope)))
        for instance,configuration,raw in self.endpoint.db.execute('SELECT instance,configuration,envelope FROM pending_discoveries').fetchall():
            agent=json.loads(configuration);provision={'key_file':self.options['application_key_file']}
            try:
                result=self.endpoint.request(provision,'/api/asg/enroll',canonical({'host_id':self.host.read_text().strip(),'instance_id':instance,'name':agent['name'],'platform':agent['platform']}))
            except OSError:continue
            key=self.root/(result['agent_id']+'.key')
            fd=os.open(str(key),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:f.write(result['api_key'])
            agent.update(agent_id=result['agent_id'],key_file=str(key))
            envelope=json.loads(raw);envelope['agent_id']=agent['agent_id']
            identity,_=self.endpoint.stream_identity(agent)
            with self.endpoint.db:
                self.endpoint.db.execute('INSERT OR REPLACE INTO enrolled VALUES(?,?)',(instance,json.dumps(agent)))
                self.endpoint.db.execute('INSERT OR IGNORE INTO drafts VALUES(?,?)',(identity,canonical(envelope)))
                self.endpoint.db.execute('DELETE FROM pending_discoveries WHERE instance=?',(instance,))
            self.endpoint.agents[agent['agent_id']]=agent;found.append(agent['agent_id'])
        for agent in current:
            row=self.endpoint.db.execute('SELECT configuration FROM enrolled WHERE instance=?',(agent['asg_instance_id'],)).fetchone()
            if not row:continue
            prior=json.loads(row[0]);agent.update({k:prior[k] for k in ('agent_id','key_file')})
            with self.endpoint.db:self.endpoint.db.execute('UPDATE enrolled SET configuration=? WHERE instance=?',(json.dumps(agent),agent['asg_instance_id']))
            self.endpoint.agents[agent['agent_id']]=agent;found.append(agent['agent_id'])
        def onboard(request_investigation):
            if not self.endpoint.config.get('soc_installation', False):return
            from .soc_onboarding import install
            import psutil
            self.endpoint.db.execute('CREATE TABLE IF NOT EXISTS soc_onboarding(instance TEXT PRIMARY KEY,result TEXT)')
            for candidate in current:
                instance=candidate['asg_instance_id']
                registered=next((row for row in self.endpoint.agents.values() if row.get('asg_instance_id')==instance),None)
                if not registered:continue
                row=self.endpoint.db.execute('SELECT result FROM soc_onboarding WHERE instance=?',(instance,)).fetchone()
                prior=json.loads(row[0]) if row else None
                installed=prior and prior.get('status') in ('installed','already_installed')
                if installed and not request_investigation:continue
                # Keep durable lifecycle markers when merging a fresh install pass
                # result; a later needs_investigation must not erase the record that
                # an investigation was already requested for this instance.
                result=dict(prior) if prior else {}
                try:
                    pid,started=instance.split(':');process=psutil.Process(int(pid))
                    if abs(process.create_time()-float(started))>0.01:continue
                    if installed:
                        # Drift check: the live catalog must still contain the exact
                        # artifact that this instance installed. Re-selection drift or
                        # an incompatible new build triggers an explicit upgrade pass,
                        # not a silent skip and not a blind overwrite.
                        from .soc_onboarding import select, integrity
                        current_choice=select(self.endpoint,registered,process.exe())
                        if (current_choice and current_choice['checksum']==prior.get('checksum')
                                and current_choice['id']==prior.get('artifact_id')
                                and integrity(self.endpoint,registered,prior)):
                            # Same catalog artifact and the installed bytes still match
                            # the receipt: no write, no model call, no status churn.
                            continue
                        if (not prior.get('drift_investigation_requested')
                                and current_choice is None and prior.get('transport')=='soc-direct-v1'
                                and not integrity(self.endpoint,registered,prior)):
                            # The learned artifact left the catalog AND local bytes were
                            # tampered with: one-shot escalation so the investigation
                            # pipeline can re-learn, without entering install loops.
                            prior['drift_investigation_requested']=True
                            with self.endpoint.db:self.endpoint.db.execute('INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)',(instance,json.dumps(prior)))
                        result['upgrade_from']=prior.get('artifact_id')
                        fresh=install(self.endpoint,registered,process.exe(),execute=True,upgrade=True,selected=current_choice)
                        if fresh.get('status')=='needs_investigation':
                            # Catalog no longer offers a compatible package: keep the
                            # previous install evidence and reopen investigation.
                            fresh={'status':'needs_investigation','route':'protocol_then_goose','reason':'catalog_package_removed_or_incompatible'}
                        result.update(fresh)
                        with self.endpoint.db:self.endpoint.db.execute('INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)',(instance,json.dumps(result)))
                        continue
                    fresh=install(self.endpoint,registered,process.exe(),execute=True)
                    result.update(fresh)
                    if fresh.get('status')=='needs_investigation' and request_investigation and not (prior and prior.get('investigation_requested')):
                        # Existing investigation pipeline owns protocol discovery and Goose
                        # fallback. Request once per instance: while a prior request is live,
                        # repeated scans must not re-arm identical investigations.
                        with build_opener(ProxyHandler({})).open(Request(self.base+'/api/reinvestigate?'+urlencode({'pid':pid}),data=b'{}',headers={'Content-Type':'application/json'}),timeout=15) as response:
                            result['investigation']=json.load(response)
                            result['investigation_requested']=True
                    with self.endpoint.db:self.endpoint.db.execute('INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)',(instance,json.dumps(result)))
                except (OSError,ValueError,psutil.Error) as exc:
                    result['status']='failed';result['reason']=type(exc).__name__
                    with self.endpoint.db:self.endpoint.db.execute('INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)',(instance,json.dumps(result)))
        # SOC packages are always tried before protocol/Goose investigation.
        onboard(True)
        self.endpoint.db.execute('CREATE TABLE IF NOT EXISTS reported_recipes(agent TEXT,fingerprint TEXT,revision TEXT,PRIMARY KEY(agent,fingerprint,revision))')
        for agent in self.endpoint.agents.values():
            fingerprint=agent.get('hook_fingerprint')
            if not fingerprint:continue
            try:
                with build_opener(ProxyHandler({})).open(self.base+'/api/recipe-bundle/export?'+urlencode({'fingerprint_id':fingerprint}),timeout=15) as response:bundle=json.load(response)
                revision=str((bundle.get('fingerprint') or {}).get('revision'))
                payload={'bundle':bundle,'agent_version':agent.get('agent_version','unknown')}
                # An installation package is offered only when the learned plan
                # actually builds into a direct-event archive; the builder is the
                # authority, so no separate declaration flag can drift out of sync.
                try:
                    from .deployment_package import build as build_installation_package, installer_revision
                    archive=build_installation_package(bundle)
                    payload['installation_archive']=base64.b64encode(archive).decode()
                    # Include installer code changes, excluding per-export timestamps.
                    revision+=':installation:'+installer_revision()+':'+hashlib.sha256(canonical(bundle['recipe'])).hexdigest()
                except (ValueError,KeyError,OSError):
                    pass
                if self.endpoint.db.execute('SELECT 1 FROM reported_recipes WHERE agent=? AND fingerprint=? AND revision=?',(agent['agent_id'],fingerprint,revision)).fetchone():continue
                result=self.endpoint.request(agent,'/api/asg/artifact',canonical(payload))
                if result.get('accepted') is True:
                    with self.endpoint.db:self.endpoint.db.execute('INSERT OR IGNORE INTO reported_recipes VALUES(?,?,?)',(agent['agent_id'],fingerprint,revision))
            except (OSError,ValueError):continue
        # A synchronous protocol investigation may have exported a new package in
        # this same refresh. Install it immediately instead of waiting two more
        # collection cycles. This pass never schedules another investigation.
        onboard(False)
        return list(dict.fromkeys(found))
