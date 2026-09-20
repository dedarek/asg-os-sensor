"""Outbound bridge to existing ASG runtime. Never exposes the endpoint dashboard.

Commands are fixed operations, pinned to an exact instance. A crash after dispatch
is uncertain and is not retried automatically (installation must not run twice).
"""
import json
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, ProxyHandler
from .protocol import canonical

PATHS={'scan':'/api/scan','investigate':'/api/reinvestigate','continue':'/api/reinvestigate/continue',
       'cancel':'/api/reinvestigate/cancel','install':'/api/onboarding/execute','verify':'/api/onboarding/verify',
       'trust_refresh':'/api/native-trust/refresh','scan_interval':'/api/scan-interval',
       'collect':'/api/scan','control_policy':'/api/hook-control/policy','control_resolve':'/api/hook-control/resolve','model_settings':'/api/model-settings'}

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
    def run_once(self):
        state=self.local('/api/state')
        controls=self.local('/api/hook-control/status')
        model_settings=self.local('/api/model-settings')
        for agent in self.endpoint.agents.values():
            try:
                self._runtime_cycle(agent,state,controls,model_settings)
            except OSError as exc:
                # One unhealthy agent (e.g. a rejected oversized report) must not
                # strand the remaining enrolled agents for this cycle.
                import logging
                logging.getLogger('asg.soc.bridge').warning('runtime cycle failed for %s: %s',agent.get('agent_id'),type(exc).__name__)

    # The SOC gateway accepts at most 4 MiB per request; keep the whole report
    # under 3.5 MiB by dropping oldest hook records and label the trimming.
    REPORT_BUDGET=3*1024*1024+512*1024

    def _runtime_cycle(self,agent,state,controls,model_settings):
            instance=agent.get('asg_instance_id')
            target=next((a for a in state.get('agents',[]) if a.get('instance_id')==instance),None)
            if target is None and agent.get('identity_refreshed'):
                old=next((a for a in state.get('agents',[]) if a.get('instance_id')==agent.get('source_instance_id')),None)
                if old:
                    target={'pid':old['pid'],'name':agent['name'],'instance_id':instance,'status':'rediscovered','adapter':{'agent_classification':{'status':'pending','roles':[]},'onboarding':{'status':'requires_fresh_plan'}}} 
            hooks=self.local('/api/hook-data?'+urlencode({'instance_id':instance,'limit':200,'max_bytes':1048576}))
            self.events.collect(agent,hooks)
            self.events.flush(agent)
            if target is None:return
            revision=int(time.time_ns())
            payload={'collector_id':(self.endpoint.config.get('state_dir') or ''),'agent':target,'hook_data':hooks,'capture_scope':'bounded_hook_view','scan_interval':state.get('scan_interval'),'control':controls,'model_settings':model_settings,'native_trust':state.get('native_trust')}
            records=hooks.get('records') or []
            total_records=len(records)
            while len(canonical({'instance_id':instance,'revision':revision,'payload':payload}))>self.REPORT_BUDGET and records:
                drop=max(1,len(records)//4)
                records=records[drop:]
                hooks['records']=records
                hooks['records_trimmed']=total_records-len(records)
            if len(records)<total_records:
                coverage=hooks.get('coverage') or {}
                limitations=coverage.get('limitations') or []
                limitations.append('上报体积超过网关上限：最早 %d 条记录本轮省略（记录本身已入事件流）'%(total_records-len(records)))
                coverage['limitations']=limitations;hooks['coverage']=coverage
            self.endpoint.request(agent,'/api/asg/runtime',canonical({'instance_id':instance,'revision':revision,'payload':payload}))
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
                                if command['operation'] not in ('collect','scan','trust_refresh','scan_interval','control_policy','control_resolve','model_settings'):path+='?'+urlencode({'pid':target['pid']})
                                arguments=command.get('arguments') or {}
                                if isinstance(arguments,str):arguments=json.loads(arguments)
                                result=self.local(path,arguments)
                                if command['operation']=='collect':
                                    with self.endpoint.db:
                                        self.endpoint.db.execute('CREATE TABLE IF NOT EXISTS collection_requests(id INTEGER PRIMARY KEY AUTOINCREMENT)')
                                        self.endpoint.db.execute('INSERT INTO collection_requests DEFAULT VALUES')
                                    result={'status':'requested','message':'Local discovery and collection queued'}
                                status='failed' if result.get('error') else 'completed'
                            except OSError as exc:status='uncertain';result={'error':type(exc).__name__}
                    with self.endpoint.db:self.endpoint.db.execute('INSERT OR REPLACE INTO runtime_commands VALUES(?,?,?)',(key,status,json.dumps(result)))
                self.endpoint.request(agent,'/api/asg/commands/'+key+'/receipt',canonical({'status':status,'result':result}))
