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
let socOutboxDropped = 0;

function socOutboxPath() { return join(dirname(LOG_PATH), "soc-outbox.jsonl"); }
const SOC_OUTBOX_LIMIT = 20000;

function loadSocEventConfig() {
  if (socEventConfig) return socEventConfig;
  socEventConfig = JSON.parse(readFileSync(CONTROL_CONFIG, "utf8"));
  socEventToken = readFileSync(socEventConfig.token_file, "utf8").trim();
  return socEventConfig;
}

function socOutboxRead() {
  try {
    if (!existsSync(socOutboxPath())) return [];
    return readFileSync(socOutboxPath(), "utf8").split("\n").filter(Boolean);
  } catch (error) { noteError("soc.events.read", error); return []; }
}

function socOutboxRewrite(lines) {
  try {
    mkdirSync(dirname(socOutboxPath()), { recursive: true });
    const temporary = socOutboxPath() + ".tmp";
    writeFileSync(temporary, lines.length ? lines.join("\n") + "\n" : "", "utf8");
    renameSync(temporary, socOutboxPath());
  } catch (error) { noteError("soc.events.rewrite", error); }
}

function forwardSocEvent(row) {
  try {
    const config = loadSocEventConfig();
    const eventId = createHash("sha256").update(String(config.instance_id) + "\\n" + JSON.stringify(row)).digest("hex");
    const entry = { event_id: eventId, instance_id: config.instance_id, event_type: row.event, timestamp: row.timestamp, channel: "direct", payload: row };
    let lines = socOutboxRead();
    const known = new Set(lines.map((line) => { try { return JSON.parse(line).event_id; } catch (e) { return ""; } }));
    if (!known.has(eventId)) lines.push(JSON.stringify(entry));
    if (lines.length > SOC_OUTBOX_LIMIT) {
      // Capacity gaps stay explicit; pending events are never silently spliced.
      socOutboxDropped += lines.length - SOC_OUTBOX_LIMIT;
      lines = lines.slice(lines.length - SOC_OUTBOX_LIMIT);
      lines.push(JSON.stringify({ event_id: createHash("sha256").update("gap\\n" + String(socOutboxDropped)).digest("hex"), instance_id: config.instance_id, event_type: "collection.overflow", timestamp: new Date().toISOString(), channel: "direct", payload: { dropped: socOutboxDropped } }));
    }
    socOutboxRewrite(lines);
    if (!socEventTimer) socEventTimer = setTimeout(flushSocEvents, 50);
  } catch (error) {
    noteError("soc.events.prepare", error);
  }
}

async function flushSocEvents() {
  socEventTimer = null;
  if (socEventSending) return;
  const lines = socOutboxRead();
  if (lines.length === 0) return;
  socEventSending = true;
  const batch = lines.slice(0, 50);
  try {
    const config = loadSocEventConfig();
    const response = await fetch(config.backend_url.replace(/\/$/, "") + "/api/asg/events", {
      method: "POST",
      headers: { "Content-Type": "application/json", "Authorization": "Bearer " + socEventToken },
      body: JSON.stringify({ instance_id: config.instance_id, events: batch.map((line) => JSON.parse(line)) })
    });
    if (!response.ok) throw new Error("HTTP " + String(response.status));
    // The cursor advances only after the server accepted this batch; the file
    // rewrite removes exactly the delivered prefix, so a restart at any point
    // replays unconfirmed events without loss.
    socOutboxRewrite(lines.slice(batch.length));
  } catch (error) {
    noteError("soc.events.send", error);
  } finally {
    socEventSending = false;
    if (socOutboxRead().length && !socEventTimer) socEventTimer = setTimeout(flushSocEvents, 1000);
  }
}

'''

FS_IMPORT_RE = None

def _fs_import_fix(content):
    global FS_IMPORT_RE
    if FS_IMPORT_RE is None:
        import re
        FS_IMPORT_RE = re.compile('import \\{([^}]*)\\} from "node:fs"')
    match = FS_IMPORT_RE.search(content)
    if not match:
        return content
    have = [x.strip() for x in match.group(1).split(',')]
    needed = ('appendFileSync', 'existsSync', 'mkdirSync', 'readFileSync', 'renameSync', 'writeFileSync')
    missing = [x for x in needed if x not in have]
    if not missing:
        return content
    return content.replace(match.group(0), 'import { ' + ', '.join(have + missing) + ' } from "node:fs"', 1)

def wire_soc_events(content):
    if 'forwardSocEvent(row);' in content:return content
    call='  appendLine(LOG_PATH, JSON.stringify(row));'
    anchor='function redact(value, depth) {'
    required=(call,anchor,'createHash','readFileSync','CONTROL_CONFIG')
    if not all(marker in content for marker in required):return content
    content=content.replace(anchor,_EVENT_HELPER+'\n'+anchor,1).replace(call,_EVENT_CALL,1)
    return _fs_import_fix(content)

def wire_soc_control_client(content, asg_root):
    """Point a learned subprocess client at the package-owned SOC config."""
    calls=all(re.search(r"client\s*\(\s*['\"]"+action+r"['\"]",content)
              for action in ('event','decision','ack'))
    if not calls or 'hook_control_client.py' not in content:return content
    config=str(Path(asg_root)/'artifacts/autonomous-service/hook-control-client.json')
    return re.sub(r'(["\'])[^"\'\n]*hook-control-client\.json\1',
                  lambda match: json.dumps(config),content)

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--platform');p.add_argument('--target',required=True)
    p.add_argument('--exe',required=True);p.add_argument('--entry');p.add_argument('--asg-root')
    p.add_argument('--backend-url');p.add_argument('--agent-id');p.add_argument('--agent-name',default='')
    p.add_argument('--token-file');p.add_argument('--instance-id',default='')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--upgrade',action='store_true')
    p.add_argument('--verify-pid',type=int)
    p.add_argument('--uninstall',action='store_true');p.add_argument('--state-dir')
    a=p.parse_args();workspace=Path(a.target).expanduser().resolve()
    state=Path(a.state_dir).expanduser().resolve() if a.state_dir else workspace.parent/('.asg-install-'+hashlib.sha256(str(workspace).encode()).hexdigest()[:16])
    bundle=json.loads((ROOT/'recipe-bundle.json').read_text())
    transport=json.loads((ROOT/'transport.json').read_text())
    installer_revision=transport.get('installer_revision')
    if not isinstance(installer_revision,str) or len(installer_revision)!=64:
        raise ValueError('installation package has no valid installer revision')
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
    runtime=constraint.get('runtime')
    if runtime not in ('native','node','bun','python'):raise ValueError('unsupported runtime compatibility')
    if runtime=='native':
        if constraint.get('entry') not in (None,'native'):raise ValueError('native compatibility entry is invalid')
        if a.entry:raise ValueError('native package does not accept a script entrypoint')
    else:
        if not a.entry or not constraint.get('entry'):raise ValueError('interpreter entrypoint validation is required')
        entry=Path(a.entry).expanduser().resolve()
        if not entry.is_file() or digest(entry)!=constraint['entry']:
            raise ValueError('target entrypoint build mismatch; investigate before installing')
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
            item['content']=wire_soc_control_client(item['content'],asg_root)
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
    package_changed=False
    if receipt.exists() and not a.uninstall:
        previous=json.loads(receipt.read_text())
        prior_binding=previous.get('binding',{'transport':'asg-local','backend_url':None,'agent_id':None})
        binding_changed=prior_binding != binding
        if binding_changed and not a.upgrade:
            raise ValueError('installed transport/identity differs; use explicit upgrade to rebind this verified package')
        package_changed=(previous.get('bundle_digest') != bundle['integrity']['digest']
                         or previous.get('installer_revision') != installer_revision
                         or binding_changed)
        if package_changed:
            if not a.upgrade:
                raise ValueError('another package is installed; uninstall it before changing recipes')
            # Explicit upgrade: roll the previous plan back first, then install
            # the newly selected package. Failures restore via the transaction
            # manifest; nothing is silently overwritten in place.
            if not a.dry_run:
                learned_install.rollback(workspace,state,approved_workspace=workspace,approved_digest=previous['plan_digest'])
    if a.verify_pid:
        import psutil
        process=psutil.Process(a.verify_pid)
        if Path(process.exe()).resolve()!=exe:raise ValueError('verification process executable differs')
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
        installed_at=(time.time() if package_changed else previous.get('installed_at',time.time())) if receipt.exists() else time.time()
        receipt.write_text(json.dumps({**result,'bundle_digest':bundle['integrity']['digest'],'installer_revision':installer_revision,'installed_plan':plan,'binding':binding,'installed_at':installed_at}));receipt.chmod(0o600)
    print(json.dumps(result))
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)}),file=sys.stderr);sys.exit(1)
