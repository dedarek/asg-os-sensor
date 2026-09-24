"""Outbound bridge to existing ASG runtime. Never exposes the endpoint dashboard.

Commands are fixed operations, pinned to an exact instance. A crash after dispatch
is uncertain and is not retried automatically (installation must not run twice).
"""
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
from .protocol import canonical

PATHS={'scan':'/api/scan','investigate':'/api/reinvestigate','continue':'/api/reinvestigate/continue',
       'cancel':'/api/reinvestigate/cancel','install':'/api/onboarding/execute','verify':'/api/onboarding/verify',
       'trust_refresh':'/api/native-trust/refresh','scan_interval':'/api/scan-interval',
       'collect':'/api/scan','control_policy':'/api/hook-control/policy','control_resolve':'/api/hook-control/resolve','model_settings':'/api/model-settings','fingerprints':'/api/recipe-bundle/export',
       # crazytest B20: the SOC admin console offers fingerprint_import and the
       # platform controller whitelists it, but this bridge lost the route when
       # the release was re-synced from the repository, so the command only
       # ever receipted "operation_unsupported". Route it to the engine import
       # endpoint; the bundle itself arrives as the POST body (arguments).
       'fingerprint_import':'/api/recipe-bundle/import'}


def fingerprint_summary(rows):
    """Compact the local fingerprint store for periodic reporting.

    Only identity, freshness and recipe-shape fields are sent; the full
    recipe body stays on the endpoint and is fetched on demand through the
    fingerprints command (recipe-bundle/export).
    """
    out = []
    for row in rows[:64]:
        recipe = row.get('hook_recipe') if isinstance(row.get('hook_recipe'), dict) else {}
        hook = recipe.get('hook') if isinstance(recipe.get('hook'), dict) else {}
        hooks = hook.get('hooks') if isinstance(hook.get('hooks'), list) else None
        out.append({'id': row.get('id'), 'name': row.get('name'),
                    'first_seen': row.get('first_seen'), 'last_seen': row.get('last_seen'),
                    'match_count': row.get('match_count'),
                    'features': row.get('features') if isinstance(row.get('features'), dict) else {},
                    'recipe': {'confidence': recipe.get('confidence'),
                               'host_platform': recipe.get('host_platform'),
                               'integration_kind': (recipe.get('integration') if isinstance(recipe.get('integration'), dict) else {}).get('kind'),
                               'install_kind': (recipe.get('install_plan') if isinstance(recipe.get('install_plan'), dict) else {}).get('kind'),
                               'hook_count': len(hooks) if hooks is not None else (1 if recipe.get('hook') else 0)}})
    return out


def fingerprints_for_target(rows, target):
    """Return only the fingerprint that actually matched this target.

    The endpoint fingerprint store is global.  A per-Agent runtime report must
    never make unrelated Agent recipes look like assets owned by this Agent.
    """
    adapter = target.get('adapter') if isinstance(target, dict) else None
    harness_id = adapter.get('harness_id') if isinstance(adapter, dict) else None
    if not harness_id or not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict) and row.get('id') == harness_id]

class RuntimeBridge:
    def __init__(self,endpoint):
        self.endpoint=endpoint
        from .event_spool import EventSpool
        self.events=EventSpool(endpoint)
        self.base=endpoint.config.get('asg_url','http://127.0.0.1:8081').rstrip('/')
        url=urlsplit(self.base)
        if url.scheme!='http' or url.hostname not in ('127.0.0.1','localhost','::1'):raise ValueError('ASG runtime must be loopback')
        endpoint.db.executescript('CREATE TABLE IF NOT EXISTS runtime_commands(id TEXT PRIMARY KEY,status TEXT,result TEXT); CREATE TABLE IF NOT EXISTS runtime_revision(agent TEXT PRIMARY KEY,revision INTEGER);')
    def local(self,path,body=None):
        with build_opener(ProxyHandler({})).open(Request(self.base+path,data=canonical(body) if body is not None else None,headers={'Content-Type':'application/json'}),timeout=15) as r:return json.load(r)
    def install_direct_policy(self,agent,policy):
        """Persist a SOC-issued policy beside an installed direct Hook.

        The Hook still asks SOC for every decision. This bounded last-known
        policy can only make that answer stricter, and remains available if the
        collector is later removed. The client reloads this file per call.
        """
        workspace=agent.get('hook_workspace')
        if not workspace:return False
        path=Path(workspace)/'.soc-hook/artifacts/autonomous-service/hook-control-client.json'
        if not path.is_file():return False
        config=json.loads(path.read_text());config['policy']=policy
        temporary=path.with_name(path.name+'.tmp')
        temporary.write_text(json.dumps(config));temporary.chmod(0o600);os.replace(temporary,path)
        return True
    def run_once(self):
        state=self.local('/api/state')
        controls=self.local('/api/hook-control/status')
        model_settings=self.local('/api/model-settings')
        active_instances={row.get('instance_id') for row in state.get('agents',[])
                          if isinstance(row,dict) and row.get('instance_id')}
        try:
            raw=self.local('/api/fingerprints').get('fingerprints')
            fingerprints=fingerprint_summary(raw) if isinstance(raw, list) else None
        except Exception:
            fingerprints=None
        for agent in self.endpoint.agents.values():
            # Enrollment history is durable, but runtime telemetry is a live
            # channel. Do not query Hook data, commands or SOC runtime cards
            # for exited test/old instances merely because their enrollment is
            # retained for audit.
            if agent.get('asg_instance_id') not in active_instances:
                continue
            try:
                self._runtime_cycle(agent,state,controls,model_settings,fingerprints)
            except OSError as exc:
                # One unhealthy agent (e.g. a rejected oversized report) must not
                # strand the remaining enrolled agents for this cycle.
                import logging
                logging.getLogger('asg.soc.bridge').warning('runtime cycle failed for %s: %s',agent.get('agent_id'),type(exc).__name__)

    # The SOC gateway accepts at most 4 MiB per request; keep the whole report
    # under 3.5 MiB by dropping oldest hook records and label the trimming.
    REPORT_BUDGET=3*1024*1024+512*1024

    def _runtime_cycle(self,agent,state,controls,model_settings,fingerprints=None):
            instance=agent.get('asg_instance_id')
            target=next((a for a in state.get('agents',[]) if a.get('instance_id')==instance),None)
            if target is None and agent.get('identity_refreshed'):
                old=next((a for a in state.get('agents',[]) if a.get('instance_id')==agent.get('source_instance_id')),None)
                if old:
                    target={'pid':old['pid'],'name':agent['name'],'instance_id':instance,'status':'rediscovered','adapter':{'agent_classification':{'status':'pending','roles':[]},'onboarding':{'status':'requires_fresh_plan'}}} 
            if target is not None:
                # SOC-first installation is owned by the endpoint service, so
                # merge its durable receipt into the runtime card. The UI must
                # not keep saying "not installed" after the package selector
                # has installed or failed a concrete artifact.
                try:row=self.endpoint.db.execute('SELECT result FROM soc_onboarding WHERE instance=?',(instance,)).fetchone()
                except Exception:row=None
                if row:
                    try:
                        receipt=json.loads(row[0])
                        target=json.loads(json.dumps(target))
                        adapter=target.setdefault('adapter',{})
                        onboarding=adapter.setdefault('onboarding',{})
                        onboarding['soc_install']=receipt
                        status=receipt.get('status')
                        if status in ('installed','already_installed','installed_waiting_activation','activation_verified'):
                            onboarding['status']=status
                            onboarding['install']={'status':'installed','reason':status,
                                                   'artifact_id':receipt.get('artifact_id'),
                                                   'checksum':receipt.get('checksum')}
                            plan=onboarding.get('plan')
                            if isinstance(plan,dict):
                                plan['authorization']={'status':'granted','source':'soc_install'}
                            # Package installation supersedes the old local
                            # "not installed / authorization required" view.
                            # Activation remains a separate, evidence-bound
                            # state and is never inferred from installation.
                            if status=='activation_verified':
                                adapter['hook_state']={'status':'loaded','verified':True,
                                    'source':'soc_install','label':'SOC Hook 已加载'}
                            else:
                                adapter['hook_state']={'status':'installed','verified':False,
                                    'source':'soc_install','label':'已安装，等待加载'}
                            action=adapter.get('user_action')
                            if isinstance(action,dict) and action.get('status')=='approval_required':
                                adapter.pop('user_action',None)
                        elif status=='failed':
                            onboarding['install']={'status':'failed','reason':receipt.get('reason') or 'package_install_failed'}
                    except (ValueError,TypeError):
                        pass
            hooks=self.local('/api/hook-data?'+urlencode({'instance_id':instance,'limit':200,'max_bytes':1048576}))
            self.events.collect(agent,hooks)
            self.events.flush(agent)
            if target is None:return
            revision=int(time.time_ns())
            target_fingerprints = fingerprints_for_target(fingerprints, target)
            payload={'collector_id':(self.endpoint.config.get('state_dir') or ''),'agent':target,'hook_data':hooks,'capture_scope':'bounded_hook_view','scan_interval':state.get('scan_interval'),'scan_enabled':state.get('scan_enabled',False),'control':controls,'model_settings':model_settings,'native_trust':state.get('native_trust'),'fingerprints':target_fingerprints}
            records=hooks.get('records') or []
            total_records=len(records)
            if records:
                # crazytest B12: measure the trimming dimension (the record
                # array) against the headroom left by the rest of the payload
                # instead of re-serializing the whole report every retry.
                skeleton=dict(payload)
                skeleton_hooks=dict(hooks)
                skeleton_hooks['records']=[]
                skeleton['hook_data']=skeleton_hooks
                base=len(canonical({'instance_id':instance,'revision':revision,'payload':skeleton}))+128
                while records and base+len(canonical(records))>self.REPORT_BUDGET:
                    drop=max(1,len(records)//4)
                    records=records[drop:]
                    hooks['records']=records
                    hooks['records_trimmed']=total_records-len(records)
            if len(records)<total_records:
                coverage=hooks.get('coverage') or {}
                limitations=coverage.get('limitations') or []
                limitations.append('上报体积超过网关上限：最早 %d 条记录本轮省略（记录本身已入事件流）'%(total_records-len(records)))
                coverage['limitations']=limitations;hooks['coverage']=coverage
            accepted=self.endpoint.request(agent,'/api/asg/runtime',canonical({'instance_id':instance,'revision':revision,'payload':payload}))
            # Crazytest B17: the gateway echoes the platform card status with
            # each acceptance. A retired verdict means this identity was
            # superseded upstream (e.g. by enrollment dedup); a retired card
            # never returns online, so keep reporting only refreshes a closed
            # record. Drop the local enrollment and let discovery decide from
            # current evidence whether the live process needs a new identity.
            if isinstance(accepted,dict) and accepted.get('agent_status')=='retired':
                self.endpoint.retire_local(agent.get('agent_id'))
                # This agent's cycle ends here; run_once continues with the
                # other enrolled agents on the next iteration.
                return
            commands=self.endpoint.request(agent,'/api/asg/commands',None,'GET')
            for command in commands.get('commands') or []:
                key=command['id'];prior=self.endpoint.db.execute('SELECT status,result FROM runtime_commands WHERE id=?',(key,)).fetchone()
                if prior:
                    status,result=prior;result=json.loads(result)
                else:
                    status='failed';result={'error':'instance_mismatch_or_operation_unsupported'}
                    if command.get('instance_id')==instance and command.get('operation') in PATHS:
                        with self.endpoint.db:self.endpoint.db.execute('INSERT INTO runtime_commands VALUES(?,?,?)',(key,'uncertain','{"error":"dispatch_interrupted"}'))
                        # Recheck immediately before mutation to reject restarted/reused PIDs.
                        fresh=self.local('/api/state')
                        if any(a.get('instance_id')==instance and a.get('pid')==target['pid'] for a in fresh.get('agents',[])):
                            try:
                                path=PATHS[command['operation']]
                                arguments=command.get('arguments') or {}
                                if isinstance(arguments,str):arguments=json.loads(arguments)
                                if command['operation']=='fingerprints':
                                    # Agent details may export only the recipe that
                                    # matched this exact target.  Global recipe
                                    # administration belongs outside this resource.
                                    requested=str(arguments.get('fingerprint_id') or '')
                                    matched=str(((target.get('adapter') or {}).get('harness_id')) or '')
                                    if not requested or requested != matched:
                                        result={'error':'fingerprint_not_matched_to_target'}
                                    else:
                                        result=self.local(path+'?'+urlencode({'fingerprint_id':requested}))
                                else:
                                    # crazytest B20: body-carrying routes match
                                    # self.path exactly on the engine, so a
                                    # stray ?pid= suffix turns them into 404s.
                                    if command['operation'] not in ('collect','scan','trust_refresh','scan_interval','control_policy','control_resolve','model_settings','fingerprint_import'):path+='?'+urlencode({'pid':target['pid']})
                                    result=self.local(path,arguments)
                                    if command['operation']=='control_policy':
                                        result={**result,'direct_hook_updated':self.install_direct_policy(agent,result)}
                                if command['operation']=='collect':
                                    with self.endpoint.db:
                                        self.endpoint.db.execute('CREATE TABLE IF NOT EXISTS collection_requests(id INTEGER PRIMARY KEY AUTOINCREMENT)')
                                        self.endpoint.db.execute('INSERT INTO collection_requests DEFAULT VALUES')
                                    result={'status':'requested','message':'Local discovery and collection queued'}
                                status='failed' if result.get('error') else 'completed'
                            except HTTPError as exc:
                                # crazytest B21: a 4xx means the engine refused
                                # the request outright (bad/unknown route), so the
                                # operation certainly did NOT run and the receipt
                                # must say failed. Only connectivity/5xx outcomes
                                # are genuinely uncertain; mislabeling 404s as
                                # 'uncertain' hid four deterministic routing bugs
                                # on 2026-09-23 behind an ambiguous status.
                                status='uncertain' if exc.code>=500 else 'failed'
                                result={'error':type(exc).__name__,'http_status':exc.code}
                            except OSError as exc:status='uncertain';result={'error':type(exc).__name__}
                    with self.endpoint.db:self.endpoint.db.execute('INSERT OR REPLACE INTO runtime_commands VALUES(?,?,?)',(key,status,json.dumps(result)))
                self.endpoint.request(agent,'/api/asg/commands/'+key+'/receipt',canonical({'status':status,'result':result}))
