"""Register live ASG candidates and confirmed instances with a provisioned SOC application.

Changed instances require a fresh matching process entrypoint and lose prior
classification and Hook evidence. Candidate names never establish Agent identity. Target environment is read locally for a bounded path allowlist.
"""
import json
import base64
import hashlib
import os
import re
import uuid
import psutil
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlsplit, urlencode
from .protocol import canonical, collect_contract

ENV_PATHS=('CODEX_HOME','OPENCODE_CONFIG_DIR','HERMES_HOME','HERMES_OPTIONAL_SKILLS_DIR','OPENCLAW_STATE_DIR')

SERVER_KEYS=('mcpServers','mcp_servers','mcp','servers')


def platform_id(identity):
    """Return a stable SOC type id without embedding machine paths.

    Investigation identities may intentionally include an absolute package
    path (``package:/.../node_modules/@scope/name``). That is useful evidence
    but is not a portable artifact type and creates one type per machine. Keep
    known symbolic ids, derive scoped package ids from package coordinates,
    and slug every fallback into the SOC platform contract.
    """
    raw=str((identity or {}).get('id') or 'unknown').strip()
    candidate=raw
    if raw.startswith('package:'):
        path=raw.split(':',1)[1].replace('\\','/')
        parts=[part for part in path.split('/') if part]
        if 'node_modules' in parts:
            tail=parts[parts.index('node_modules')+1:]
            candidate='-'.join(tail[:2] if tail and tail[0].startswith('@') else tail[:1])
        elif parts:
            candidate=parts[-1]
    candidate=candidate.lower().lstrip('@')
    candidate=re.sub(r'[^a-z0-9_-]+','-',candidate).strip('-_')
    return candidate[:80] or 'unknown'


def durable_asset_id(agent):
    """Identify one installed Agent asset independently of a process restart."""
    platform=str(agent.get('platform') or 'unknown')
    candidates=(agent.get('hook_workspace'),agent.get('executable'),
                agent.get('workspace'))
    root=next((item for item in candidates if isinstance(item,(str,bytes,os.PathLike))
               and str(item)), '')
    try:root=str(Path(root).expanduser().resolve())
    except (OSError,RuntimeError):root=str(root)
    return hashlib.sha256((platform+'\0'+root).encode()).hexdigest()


def same_asset(prior, current):
    if prior.get('asset_id') and current.get('asset_id'):
        return prior['asset_id']==current['asset_id']
    return (prior.get('platform')==current.get('platform')
            and (prior.get('hook_workspace') or prior.get('workspace'))
            ==(current.get('hook_workspace') or current.get('workspace')))


def canonical_asset_instances(agents):
    """Choose one live process to represent each installed Agent asset.

    Desktop Agents can spawn short-lived helper processes with the same binary
    and profile.  Those helpers must not replace the long-lived app-server in
    the single current-runtime row and make the SOC card flap between proven
    and empty states.  Prefer a confirmed identity, then an older live process;
    when the old process exits, the surviving new instance is selected on the
    next refresh.
    """
    selected={};order=[]
    for agent in agents:
        key=agent.get('asset_id') or agent.get('asg_instance_id')
        if key not in selected:
            selected[key]=agent;order.append(key);continue
        def rank(value):
            confirmed_rank=1 if value.get('classification')=='confirmed_agent' else 0
            try:created=float(str(value.get('asg_instance_id','')).split(':',1)[1])
            except (ValueError,IndexError):created=float('inf')
            return confirmed_rank,-created
        if rank(agent)>rank(selected[key]):selected[key]=agent
    return [selected[key] for key in order]


def bound_conversation_proof(instance, report, observation=None):
    """Use an actual current-instance Hook conversation as an Agent signal.

    A configured Hook, package name, or past process is insufficient. The
    observer validates the binding and filters the event file by PID/start time.
    This read is only used to admit otherwise pending candidates to SOC.
    """
    from runtime import event_vocabulary
    if (not isinstance(report, dict) or report.get('status') != 'ok'
            or (report.get('filter') or {}).get('instance_id') != instance
            or not any(binding.get('instance_id') == instance
                       and binding.get('target_alive') is True
                       for binding in report.get('bindings') or [])):
        return False
    rows = report.get('records') or []
    kinds = {row.get('event_type') for row in rows}
    loaded = ('hook.loaded' in kinds or
              isinstance(observation, dict) and observation.get('instance_id') == instance and
              observation.get('loaded_observed') is True and
              observation.get('target_alive') is True)
    if not loaded:
        return False
    return (any(row.get('event_type') == 'user.input' and
                event_vocabulary.text_for(row.get('payload'), 'user') for row in rows)
            and any(row.get('event_type') == 'assistant.output' and
                    event_vocabulary.text_for(row.get('payload'), 'assistant') for row in rows))


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



def _fingerprint_asset_paths(fingerprint_id):
    """Read asset paths settled by a completed Goose investigation.

    matcher.remember_verified stores skill_roots/mcp_configs on the fingerprint
    entry. Returning them here lets the inventory layer scan those roots
    directly on later sightings of the same agent family - no model call.
    """
    if not fingerprint_id or not isinstance(fingerprint_id, str):
        return {}
    try:
        from runtime import matcher
        db = matcher.load()
    except Exception:
        return {}
    for entry in db.get('fingerprints', []):
        if entry.get('id') == fingerprint_id:
            return entry.get('asset_paths') or {}
    return {}


def confirmed(state, conversation_proof=None):
    for target in state.get('agents',[]):
        adapter=target.get('adapter') or {}
        classification=adapter.get('agent_classification') or {}
        if classification.get('status') not in ('pending','confirmed_agent'):continue
        if classification.get('roles') and 'agent' not in classification['roles']:continue
        # A pending investigation is not, by itself, an Agent identity.  Keep
        # zero-prior discovery generic, but require at least one deterministic
        # Agent signal before a pending process is registered in SOC.  This
        # admits protocol-bearing runtimes and strong process profiles while
        # excluding ordinary package-owned servers (for example a web dev
        # server with one network connection) from the Agent inventory.
        if classification.get('status')=='pending':
            identity=target.get('identity') or {}
            protocols=identity.get('protocol_dependencies') or []
            signals={item.get('source') for item in (target.get('discovery_evidence') or {}).get('signals',[]) if isinstance(item,dict)}
            strong_signal=bool(protocols) or bool(signals & {
                'opened-standard-asset','agent-control-protocol',
                'agent-tool-protocol','model-sdk-runtime',
            })
            if int(target.get('score') or 0)<40 and not strong_signal:
                if conversation_proof is None or not conversation_proof(target.get('instance_id','')):
                    continue
        instance=target.get('instance_id','');source_instance=instance;changed=False
        try:
            pid,created=instance.split(':',1);process=psutil.Process(int(pid))
            if int(pid)!=target['pid']:continue
            create_time_delta=abs(process.create_time()-float(created))
            if create_time_delta>0.01:
                evidence=(target.get('identity') or {}).get('evidence')
                # Re-discover the current process, never reuse the old identity or its Hook verdict.
                if not isinstance(evidence,str) or evidence not in [process.exe(),*process.cmdline()]:continue
                # macOS can report the same live process start one second apart
                # through different process APIs.  Keep the sensor's canonical
                # instance stamp when the entrypoint still proves it is the same
                # process; otherwise the runtime bridge cannot bind this live
                # process to its durable SOC asset.
                if create_time_delta>2.0:
                    instance=f'{pid}:{process.create_time()}';changed=True
                    classification={'status':'pending','roles':[]}
                    adapter={}
            workspace=process.cwd()
            environment={k:v for k,v in process.environ().items() if k in ENV_PATHS}
        except (ValueError,KeyError,psutil.Error):continue
        identity=target.get('identity') or {}
        platform=platform_id(identity)
        plan=(adapter.get('onboarding') or {}).get('plan') or {}
        hook_workspace=plan.get('workspace')
        workspace_binding='investigation_evidence'
        if plan.get('recipe_source') == 'fingerprint_reuse':
            hook_workspace=resolve_reused_workspace(process,plan)
            workspace_binding='live_open_file' if hook_workspace else 'unresolved'
        if not isinstance(hook_workspace,str) or not Path(hook_workspace).is_absolute():
            hook_workspace=None
        # SOC owns one durable asset per Agent type and installation/profile
        # location. Process identity remains pid:create_time and is never used
        # as the durable card id. Hash the local path before it leaves the host.
        agent={'platform':platform,'workspace':workspace,'executable':process.exe(),
               'hook_workspace':hook_workspace,'hook_workspace_binding':workspace_binding,'collection_environment':environment,'asg_instance_id':instance,'source_instance_id':source_instance,'identity_refreshed':changed,
               'hook_fingerprint':plan.get('fingerprint_id'),'agent_version':str(identity.get('version') or target.get('version') or 'unknown'),'classification':classification.get('status','pending'),'name':target.get('name') or platform,'learned_skill_roots':[],'learned_mcp_configs':[]}
        agent['asset_id']=durable_asset_id(agent)
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
        # Goose 沉淀闭环：配方调查成功后保存的资产路径，在下一次发现同族 Agent
        # 时直接成为采集根，不再要求重新调查。只作补充，不覆盖本次调查结果。
        settled=_fingerprint_asset_paths(agent.get('hook_fingerprint'))
        for root_path in settled.get('skill_roots') or []:
            if root_path not in agent['learned_skill_roots']:
                agent['learned_skill_roots'].append(root_path)
        known={item.get('path') for item in agent['learned_mcp_configs']}
        from .protocol import config as _parse_config
        for config_path in settled.get('mcp_configs') or []:
            if config_path in known:continue
            try:
                field=first_mcp_field(_parse_config(Path(config_path)))
            except Exception:
                continue
            if field:agent['learned_mcp_configs'].append({'path':config_path,'field':field});known.add(config_path)
        yield agent


def resolve_reused_workspace(process, plan):
    """Bind a portable file plan to the config root used by this live process.

    The recipe contributes only relative file names.  The endpoint derives a
    candidate root from files actually opened by the target process, so a new
    profile never inherits the learning instance's absolute directory.
    """
    recipe=plan.get('reuse_recipe') or {}
    install_plan=recipe.get('install_plan') or {}
    relative=[]
    for item in install_plan.get('files') or []:
        value=item.get('path') if isinstance(item,dict) else None
        if not isinstance(value,str):continue
        path=Path(value)
        if path.is_absolute() or '..' in path.parts or not path.parts:continue
        relative.append(path)
    if not relative:return None
    candidates={}
    try:opened=[Path(item.path).resolve() for item in process.open_files()]
    except (psutil.Error,OSError):return None
    for actual in opened:
        for rel in relative:
            if len(actual.parts)<len(rel.parts) or actual.parts[-len(rel.parts):] != rel.parts:continue
            root=Path(*actual.parts[:-len(rel.parts)])
            if not root.is_absolute() or str(root)==str(root.anchor):continue
            candidates.setdefault(root,set()).add(str(rel))
    if not candidates:return None
    ranked=sorted(candidates.items(),key=lambda pair:(len(pair[1]),len(str(pair[0]))),reverse=True)
    if len(ranked)>1 and len(ranked[0][1])==len(ranked[1][1]):return None
    return str(ranked[0][0])

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
        endpoint.db.execute('CREATE TABLE IF NOT EXISTS reported_protocol_packages(instance TEXT,digest TEXT,PRIMARY KEY(instance,digest))')
    def refresh(self):
        try:
            with build_opener(ProxyHandler({})).open(self.base+'/api/state',timeout=15) as r:state=json.load(r)
        except OSError:state={'agents':[]}
        proof_cache={}
        def conversation_proof(instance):
            if instance not in proof_cache:
                try:
                    query=urlencode({'instance_id':instance,'limit':100,'max_bytes':1048576})
                    with build_opener(ProxyHandler({})).open(
                            self.base+'/api/hook-data?'+query, timeout=5) as response:
                        snapshot=next((a for a in state.get('agents',[])
                                       if a.get('instance_id')==instance),{})
                        observation=(snapshot.get('adapter') or {}).get('observation_evidence')
                        proof_cache[instance]=bound_conversation_proof(
                            instance,json.load(response),observation)
                except (OSError, ValueError, TypeError):
                    proof_cache[instance]=False
            return proof_cache[instance]
        found=[];current=canonical_asset_instances(list(confirmed(state,conversation_proof)))
        # Capture before the first network enrollment. An exited instance's
        # retained snapshot remains historical evidence, never a live heartbeat.
        for agent in current:
            # Rolling upgrades may read cached discoveries written before
            # asset_id existed. Derive it instead of dropping the discovery.
            agent.setdefault('asset_id',durable_asset_id(agent))
            instance=agent['asg_instance_id']
            enrolled=self.endpoint.db.execute(
                'SELECT configuration FROM enrolled WHERE instance=?',(instance,)).fetchone()
            if enrolled:
                prior=json.loads(enrolled[0])
                # Existing stable rows stay idempotent. Legacy instance-scoped
                # rows must pass through enrollment once so SOC can replace
                # restart-created cards with the durable asset card.
                if (prior.get('asset_id')==agent['asset_id']
                        and prior.get('enrollment_scope')=='asset'):continue
            # Same live process, drifted identity stamp: macOS create_time reads
            # can flap by ~1s, which would double-enroll one process, duplicate
            # SOC cards and strand queued commands. Transfer the enrollment and
            # onboarding receipt instead (pid match within 2s).
            try:
                dpi,dct=instance.split(':',1);dct=float(dct)
                match=next((row for row in self.endpoint.db.execute('SELECT instance,configuration FROM enrolled').fetchall()
                    if row[0]!=instance and row[0].split(':',1)[0]==dpi and len(row[0].split(':',1))==2
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
                home=agent.get('collection_home'),
                env=agent.get('collection_environment'),bounded=True)
            with self.endpoint.db:self.endpoint.db.execute('INSERT OR IGNORE INTO pending_discoveries VALUES(?,?,?)',(instance,json.dumps(agent),canonical(envelope)))
        for instance,configuration,raw in self.endpoint.db.execute('SELECT instance,configuration,envelope FROM pending_discoveries').fetchall():
            agent=json.loads(configuration);provision={'key_file':self.options['application_key_file']}
            agent.setdefault('asset_id',durable_asset_id(agent))
            superseded=[]
            for prior_instance,prior_raw in self.endpoint.db.execute(
                    'SELECT instance,configuration FROM enrolled').fetchall():
                if prior_instance==instance or not same_asset(json.loads(prior_raw),agent):continue
                prior_id=json.loads(prior_raw).get('agent_id')
                if prior_id:superseded.append(prior_id)
            existing=self.endpoint.db.execute(
                'SELECT configuration FROM enrolled WHERE instance=?',(instance,)).fetchone()
            if existing:
                prior_id=json.loads(existing[0]).get('agent_id')
                if prior_id:superseded.append(prior_id)
            try:
                result=self.endpoint.request(provision,'/api/asg/enroll',canonical({
                    'host_id':self.host.read_text().strip(),'asset_id':agent['asset_id'],
                    'instance_id':instance,'name':agent['name'],'platform':agent['platform'],
                    'supersedes_agent_ids':sorted(set(superseded))[:256]}))
            except OSError:continue
            key=self.root/(result['agent_id']+'.key')
            fd=os.open(str(key),os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as f:f.write(result['api_key'])
            agent.update(agent_id=result['agent_id'],key_file=str(key),
                         enrollment_scope=('asset' if result.get('asset_id')==agent['asset_id']
                                           else 'instance'))
            envelope=json.loads(raw);envelope['agent_id']=agent['agent_id']
            identity,_=self.endpoint.stream_identity(agent)
            with self.endpoint.db:
                # One durable local enrollment per installed asset. Runtime
                # instances remain in SOC event history; they must not compete
                # to populate the current card after a restart.
                stale=[]
                for prior_instance,prior_raw in self.endpoint.db.execute(
                        'SELECT instance,configuration FROM enrolled').fetchall():
                    if prior_instance==instance:continue
                    prior=json.loads(prior_raw)
                    if same_asset(prior,agent):stale.append(prior_instance)
                for prior_instance in stale:
                    self.endpoint.db.execute('DELETE FROM enrolled WHERE instance=?',(prior_instance,))
                    self.endpoint.db.execute('DELETE FROM soc_onboarding WHERE instance=?',(prior_instance,))
                    self.endpoint.db.execute('DELETE FROM reported_protocol_packages WHERE instance=?',(prior_instance,))
                self.endpoint.db.execute('INSERT OR REPLACE INTO enrolled VALUES(?,?)',(instance,json.dumps(agent)))
                self.endpoint.db.execute('INSERT OR IGNORE INTO drafts VALUES(?,?)',(identity,canonical(envelope)))
                self.endpoint.db.execute('DELETE FROM pending_discoveries WHERE instance=?',(instance,))
            self.endpoint.agents[agent['agent_id']]=agent;found.append(agent['agent_id'])
        for agent in current:
            row=self.endpoint.db.execute('SELECT configuration FROM enrolled WHERE instance=?',(agent['asg_instance_id'],)).fetchone()
            if not row:continue
            prior=json.loads(row[0]);agent.update({k:prior[k] for k in ('agent_id','key_file','enrollment_scope') if k in prior})
            with self.endpoint.db:self.endpoint.db.execute('UPDATE enrolled SET configuration=? WHERE instance=?',(json.dumps(agent),agent['asg_instance_id']))
            self.endpoint.agents[agent['agent_id']]=agent;found.append(agent['agent_id'])

        # A config that the live target actually opened can yield a generic
        # command-Hook package without a model.  Publish it to SOC first; the
        # normal catalog selector below then downloads and installs it exactly
        # like every other artifact.  Directory-name clues never enter here.
        if self.endpoint.config.get('soc_installation', False):
            from .soc_onboarding import select
            from .deployment_package import build as build_installation_package
            from runtime.protocol_package import prepare as prepare_protocol_package
            import psutil
            for candidate in current:
                instance=candidate['asg_instance_id']
                registered=next((row for row in self.endpoint.agents.values()
                                 if row.get('asg_instance_id')==instance),None)
                if not registered:continue
                try:
                    pid,started=instance.split(':',1);process=psutil.Process(int(pid))
                    if abs(process.create_time()-float(started))>0.01:continue
                    if select(self.endpoint,registered,process.exe()) is not None:continue
                    bundle=prepare_protocol_package(process)
                    if bundle is None:continue
                    archive=build_installation_package(bundle)
                    revision=hashlib.sha256(canonical(bundle)).hexdigest()
                    if self.endpoint.db.execute('SELECT 1 FROM reported_protocol_packages WHERE instance=? AND digest=?',(instance,revision)).fetchone():continue
                    result=self.endpoint.request(registered,'/api/asg/artifact',canonical({
                        'bundle':bundle,'agent_version':registered.get('agent_version','unknown'),
                        'installation_archive':base64.b64encode(archive).decode()}))
                    if result.get('accepted') is True:
                        with self.endpoint.db:self.endpoint.db.execute(
                            'INSERT OR IGNORE INTO reported_protocol_packages VALUES(?,?)',(instance,revision))
                except (OSError,ValueError,KeyError,psutil.Error):
                    continue
        def onboard(request_investigation):
            if not self.endpoint.config.get('soc_installation', False):return
            from .soc_onboarding import install
            import psutil
            completed={'installed','already_installed','installed_waiting_activation','activation_verified'}
            def merged_result(prior,fresh):
                # Current lifecycle state must not carry an obsolete failure
                # reason or a one-shot investigation latch after a package has
                # installed. Besides confusing the UI, retaining that latch
                # would suppress a legitimate future drift investigation.
                return dict(fresh) if fresh.get('status') in completed else {**(prior or {}),**fresh}
            self.endpoint.db.execute('CREATE TABLE IF NOT EXISTS soc_onboarding(instance TEXT PRIMARY KEY,result TEXT)')
            for candidate in current:
                instance=candidate['asg_instance_id']
                registered=next((row for row in self.endpoint.agents.values() if row.get('asg_instance_id')==instance),None)
                if not registered:continue
                row=self.endpoint.db.execute('SELECT result FROM soc_onboarding WHERE instance=?',(instance,)).fetchone()
                prior=json.loads(row[0]) if row else None
                # The target already emitted a bound ASG conversation through
                # an installed Hook. Do not lay a second automatic package on
                # top of it just because SOC has no installer receipt yet.
                if (proof_cache.get(instance) is True and not prior):
                    with self.endpoint.db:self.endpoint.db.execute(
                        'INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)',
                        (instance,json.dumps({'status':'existing_hook_observed',
                                              'reason':'current_instance_hook_events',
                                              'installation':'not_attributed_to_soc_package'})))
                    continue
                if prior and prior.get('status')=='existing_hook_observed':
                    # Crazytest B5: an observed foreign hook must not veto a
                    # SOC package forever. If the catalog later gains a
                    # compatible artifact for this agent, fall through to the
                    # normal selection/install pass (the installer is
                    # idempotent and merge-safe); otherwise keep waiting.
                    from .soc_onboarding import select as _select
                    try:
                        _pid,_started=instance.split(':')
                        _proc=psutil.Process(int(_pid))
                        pending_artifact=_select(self.endpoint,registered,_proc.exe())
                    except Exception:
                        pending_artifact=None
                    if pending_artifact is None:
                        continue
                installed=prior and prior.get('status') in (
                    'installed','already_installed','installed_waiting_activation',
                    'activation_verified')
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
                            # A verified install is stable. A package that is still
                            # waiting for its first callback must be re-verified on
                            # later scans, but the idempotent installer will not
                            # rewrite unchanged files. This closes the gap where a
                            # restarted/hot-reloaded target could stay "waiting"
                            # forever after it had actually loaded the Hook.
                            if prior.get('status')!='installed_waiting_activation':
                                continue
                            fresh=install(self.endpoint,registered,process.exe(),
                                          execute=True,selected=current_choice)
                            result=merged_result(prior,fresh)
                            with self.endpoint.db:self.endpoint.db.execute(
                                'INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)',
                                (instance,json.dumps(result)))
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
                        result=merged_result(prior,fresh)
                        with self.endpoint.db:self.endpoint.db.execute('INSERT OR REPLACE INTO soc_onboarding VALUES(?,?)',(instance,json.dumps(result)))
                        continue
                    fresh=install(self.endpoint,registered,process.exe(),execute=True)
                    result=merged_result(prior,fresh)
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
