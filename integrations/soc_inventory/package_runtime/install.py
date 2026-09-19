"""Standalone learned file-plan installer, with bounded writes and rollback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time
import re

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
from runtime import learned_install,recipe_bundle

_EVENT_CALL='  appendLine(LOG_PATH, JSON.stringify(row));\n  forwardSocEvent(row);'
_EVENT_HELPER=r'''
let socEventConfig = null;
let socEventToken = null;
let socEventTimer = null;
let socEventSending = false;
const socEventQueue = [];

function loadSocEventConfig() {
  if (socEventConfig) return socEventConfig;
  socEventConfig = JSON.parse(readFileSync(CONTROL_CONFIG, "utf8"));
  socEventToken = readFileSync(socEventConfig.token_file, "utf8").trim();
  return socEventConfig;
}

function forwardSocEvent(row) {
  try {
    const config = loadSocEventConfig();
    const eventId = createHash("sha256").update(String(config.instance_id) + "\n" + JSON.stringify(row)).digest("hex");
    socEventQueue.push({ event_id: eventId, instance_id: config.instance_id, event_type: row.event, timestamp: row.timestamp, payload: row });
    if (socEventQueue.length > 500) socEventQueue.splice(0, socEventQueue.length - 500);
    if (!socEventTimer) socEventTimer = setTimeout(flushSocEvents, 50);
  } catch (error) {
    noteError("soc.events.prepare", error);
  }
}

async function flushSocEvents() {
  socEventTimer = null;
  if (socEventSending || socEventQueue.length === 0) return;
  socEventSending = true;
  const batch = socEventQueue.splice(0, 50);
  try {
    const config = loadSocEventConfig();
    const response = await fetch(config.backend_url.replace(/\/$/, "") + "/api/asg/events", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + socEventToken },
      body: JSON.stringify({ instance_id: config.instance_id, events: batch })
    });
    if (!response.ok) throw new Error("HTTP " + String(response.status));
  } catch (error) {
    socEventQueue.unshift(...batch);
    if (socEventQueue.length > 500) socEventQueue.length = 500;
    noteError("soc.events.send", error);
  } finally {
    socEventSending = false;
    if (socEventQueue.length && !socEventTimer) socEventTimer = setTimeout(flushSocEvents, 1000);
  }
}
'''

def wire_soc_events(content):
    if 'forwardSocEvent(row);' in content:return content
    call='  appendLine(LOG_PATH, JSON.stringify(row));'
    anchor='function redact(value, depth) {'
    required=(call,anchor,'createHash','readFileSync','CONTROL_CONFIG')
    if not all(marker in content for marker in required):return content
    return content.replace(anchor,_EVENT_HELPER+'\n'+anchor,1).replace(call,_EVENT_CALL,1)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--platform');p.add_argument('--target',required=True)
    p.add_argument('--exe',required=True);p.add_argument('--asg-root')
    p.add_argument('--backend-url');p.add_argument('--agent-id');p.add_argument('--agent-name',default='')
    p.add_argument('--token-file');p.add_argument('--instance-id',default='')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--verify-pid',type=int)
    p.add_argument('--uninstall',action='store_true');p.add_argument('--state-dir')
    a=p.parse_args();workspace=Path(a.target).expanduser().resolve()
    state=Path(a.state_dir).expanduser().resolve() if a.state_dir else workspace.parent/('.asg-install-'+hashlib.sha256(str(workspace).encode()).hexdigest()[:16])
    bundle=json.loads((ROOT/'recipe-bundle.json').read_text())
    recipe_bundle.validate_bundle(bundle)
    constraint=bundle['constraints'].get('compatibility') or {}
    exe=Path(a.exe).expanduser().resolve()
    def digest(path):
        h=hashlib.sha256()
        with path.open('rb') as f:
            for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
        return h.hexdigest()
    if constraint.get('platform')!=platform.system() or constraint.get('architecture')!=platform.machine():raise ValueError('target platform/build mismatch')
    if not constraint.get('executable') or digest(exe)!=constraint['executable']:raise ValueError('target executable build mismatch; investigate before installing')
    for key,value in constraint.items():
        if key.startswith('Contents/'):
            app=next((x for x in exe.parents if x.suffix=='.app'),None)
            if app is None or digest(app/key)!=value:raise ValueError('target bundle build mismatch')
    # A script interpreter requires entrypoint validation; do not infer it from its name.
    if constraint.get('runtime')!='native':raise ValueError('interpreter entrypoint validation is required')
    direct=bool(a.backend_url)
    if direct:
        if not all((a.agent_id,a.platform,a.token_file)):raise ValueError('SOC agent-id, platform and token-file are required')
        asg_root=workspace/'.soc-hook'
    else:
        if not a.asg_root:raise ValueError('SOC backend-url is required')
        asg_root=Path(a.asg_root).expanduser().resolve()
        if not (asg_root/'runtime/hook_control_client.py').is_file():raise ValueError('ASG collector runtime is required')
    resolved=recipe_bundle.resolve_bundle(bundle,asg_root=asg_root,target_workspace=workspace)
    plan=learned_install.validate_plan(resolved['recipe']['install_plan'])
    if direct:
        # Preserve the learned Hook mechanism; replace only its generic control transport.
        for item in plan['files']:
            item['content']=re.sub(r'(const CONTROL_PYTHON\s*=\s*)[\"\'][^\"\']+[\"\']',lambda m:m[1]+json.dumps(sys.executable),item['content'])
            item['content']=wire_soc_events(item['content'])
        cfg={'backend_url':a.backend_url,'agent_id':a.agent_id,'agent_name':a.agent_name,'platform':a.platform,'instance_id':a.instance_id or a.agent_id,'token_file':str(asg_root/'agent.key')}
        for rel,content in [('runtime/hook_control_client.py',(ROOT/'soc_client.py').read_text()),('artifacts/autonomous-service/hook-control-client.json',json.dumps(cfg)),('agent.key',Path(a.token_file).read_text().strip())]:
            plan['files'].append({'path':'.soc-hook/'+rel,'content':content,'expected_sha256':None})
        plan=learned_install.validate_plan(plan)
    # Full file contents are present: absent files are a clean install, never an overwrite.
    for item in plan['files']:
        target=workspace/item['path']
        if not target.exists():item['expected_sha256']=None
    binding={'transport':'soc-direct-v1' if direct else 'asg-local','backend_url':a.backend_url,'agent_id':a.agent_id}
    receipt=state/'package-receipt.json'
    if receipt.exists() and not a.uninstall:
        previous=json.loads(receipt.read_text())
        if previous.get('binding',{'transport':'asg-local','backend_url':None,'agent_id':None}) != binding:raise ValueError('installed transport/identity differs; uninstall before switching control plane')
        if previous.get('bundle_digest') != bundle['integrity']['digest']:
            raise ValueError('another package is installed; uninstall it before changing recipes')
        plan=learned_install.validate_plan(previous['installed_plan'])
    if a.verify_pid:
        import psutil
        process=psutil.Process(a.verify_pid)
        if Path(process.exe()).resolve()!=exe:raise ValueError('verification process executable differs')
        if Path(process.cwd()).resolve()!=workspace:raise ValueError('verification process workspace differs')
        previous=json.loads(receipt.read_text())
        for item in previous['installed_plan']['files']:
            if digest(workspace/item['path'])!=hashlib.sha256(item['content'].encode()).hexdigest():raise ValueError('installed file changed; verification rejected')
        source=resolved['recipe']['observation_source']
        log=Path(source['log_path'])
        if log.is_absolute() or '..' in log.parts:raise ValueError('observation log must be workspace-relative')
        from datetime import datetime
        loaded=[]
        with (workspace/log).open() as stream:
            for line in stream:
                try:
                    event=json.loads(line)
                    fields=source['fields']
                    stamp=datetime.fromisoformat(event[fields['timestamp']].replace('Z','+00:00')).timestamp()
                    if int(event.get(fields['pid'],0))==process.pid and stamp>=max(process.create_time(),previous['installed_at']) and event.get(fields['event'])=='hook.loaded':loaded.append(event)
                except (ValueError,KeyError,TypeError):continue
        if not loaded:raise ValueError('no fresh hook.loaded callback from this installed instance')
        result={'status':'activation_verified','pid':process.pid,'create_time':process.create_time(),'package_digest':bundle['integrity']['digest'],'plan_digest':previous['plan_digest'],'event_count':len(loaded),'log_path':str(workspace/log),'verified_at':time.time()}
        (state/'activation-receipt.json').write_text(json.dumps(result))
    elif a.uninstall:
        previous=json.loads(receipt.read_text())
        result=learned_install.rollback(workspace,state,approved_workspace=workspace,approved_digest=previous['plan_digest'])
    else:
        if a.dry_run:
            print(json.dumps({'status':'validated','files':[f['path'] for f in plan['files']],'target':str(workspace),'activation':'unverified'}));return
        workspace.mkdir(parents=True,exist_ok=True)
        state.mkdir(parents=True,exist_ok=True)
        result=learned_install.install(plan,workspace,state,approved_workspace=workspace,approved_digest=learned_install.plan_digest(plan))
        receipt.write_text(json.dumps({**result,'bundle_digest':bundle['integrity']['digest'],'installed_plan':plan,'binding':binding,'installed_at':previous.get('installed_at',time.time()) if receipt.exists() else time.time()}));receipt.chmod(0o600)
    print(json.dumps(result))
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)}),file=sys.stderr);sys.exit(1)
