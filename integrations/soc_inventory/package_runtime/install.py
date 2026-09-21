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

# This file is package-created but runtime-owned after installation: SOC policy
# delivery and identity rebinding update it atomically. Treating those expected
# writes as package drift makes a healthy Hook fail its next verification.
RUNTIME_MUTABLE={'.soc-hook/artifacts/autonomous-service/hook-control-client.json'}

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
  // Identity may be rebound after the Agent starts. Read the tiny local
  // binding on every delivery so the process never keeps reporting to the
  // previous instance merely because hook.loaded happened first.
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
    if not calls or 'hook_control_client' not in content:return content
    client=json.dumps(str(Path(asg_root)/'runtime/hook_control_client.mjs'))
    config=json.dumps(str(Path(asg_root)/'artifacts/autonomous-service/hook-control-client.json'))
    content=re.sub(r'(["\'])[^"\'\n]*hook_control_client\.(?:py|mjs)\1',client,content)
    content=re.sub(r'(["\'])[^"\'\n]*hook-control-client\.json\1',config,content)
    # The common learned client is a three-item argv array. Once both ASG-owned
    # suffixes are present, replace its interpreter too; never retain the
    # learning machine's /Library, venv, or Windows Python path.
    pattern=(r'(\[\s*)["\'][^"\'\n]+["\'](\s*,\s*'
             +re.escape(client)+r'\s*,\s*'+re.escape(config)+r'\s*,?\s*\])')
    return re.sub(pattern,lambda match:match.group(1)+'process.execPath'+match.group(2),content)

def wire_model_request_payload(content):
    """Capture DSH's complete semantic LLM request without changing execution.

    ``agent/request`` only exposes routing state.  DSH's ``llm/stream``
    waterfall receives the immutable ``GenerateOptions`` object that is sent
    to the provider adapter, including messages and tools.  Short requests are
    emitted as one event; large requests are losslessly split into base64 JSON
    chunks so the SOC can reconstruct them instead of accepting truncation.
    """
    if "ctx.on('llm/stream'" in content:return content
    route="""        content: {
          provider: (resolved && resolved.provider) ?? null,
          model: (resolved && resolved.model) ?? null,
          reasoningEffort: (resolved && resolved.reasoningEffort) ?? null,
        },
        content_complete: true,"""
    if route not in content or "ctx.on('agent/request'" not in content:return content
    helper="""const redactRequest = (value, key = '', depth = 0) => {
  if (/token|secret|password|api[_-]?key|authorization|cookie/i.test(key)) return '[REDACTED]'
  if (Array.isArray(value)) return value.map((item) => redactRequest(item, key, depth + 1))
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([name, item]) => [name, redactRequest(item, name, depth + 1)]))
  }
  if (typeof value === 'string') return value.replace(/Bearer\\s+[^\\s]+/gi, 'Bearer [REDACTED]')
  return value
}

const publishModelRequest = (options) => {
  const safe = redactRequest(options)
  let json
  try { json = JSON.stringify(safe) } catch { json = JSON.stringify({ serialization_error: true }) }
  const bytes = Buffer.from(json, 'utf8')
  const requestId = String((options && (options.requestId || options.id)) || ('llm:' + pid + ':' + (sequence + 1)))
  const sessionId = idOf((options && options.session) || options) || null
  const turn = (options && (options.turn ?? options.turnId)) ?? null
  const step = (options && options.step) ?? null
  if (bytes.length <= maxBytes) {
    publish({ event: 'model.request', session_id: sessionId, turn_id: turn, step,
      event_id: requestId + ':model.request', call_id: requestId, content: safe,
      content_complete: true })
    return
  }
  const chunkCount = Math.ceil(bytes.length / maxBytes)
  for (let index = 0; index < chunkCount; index += 1) {
    const data = bytes.subarray(index * maxBytes, Math.min(bytes.length, (index + 1) * maxBytes)).toString('base64')
    publish({ event: index === 0 ? 'model.request' : 'model.request.chunk',
      session_id: sessionId, turn_id: turn, step,
      event_id: requestId + ':model.request:' + index, call_id: requestId,
      content: { request_id: requestId, chunk_index: index, chunk_count: chunkCount,
        encoding: 'base64-json', data, request_payload_complete: true },
      content_complete: true })
  }
}

"""
    anchor="  // ── 2. 模型请求路由（waterfall：必须原样返回 next() 结果）──\n"
    if anchor in content:content=content.replace(anchor,helper+anchor,1)
    section=content.find("ctx.on('agent/request'")
    resolved=content.find("    const resolved = await next()\n",section)
    if resolved < 0:return content
    replacement="""        content: {
          provider: (resolved && resolved.provider) ?? null,
          model: (resolved && resolved.model) ?? null,
          reasoningEffort: (resolved && resolved.reasoningEffort) ?? null,
        },
        content_complete: true,"""
    content=content.replace(route,replacement,1)
    content=content.replace("        event: 'model.request',\n","        event: 'model.route',\n",1)
    model_listener="""

  // Complete provider-adapter input. Returning next() unchanged preserves the
  // target's streaming semantics and makes capture observational only.
  ctx.on('llm/stream', (options, next) => {
    try { publishModelRequest(options) } catch { /* capture must not break the model call */ }
    return next()
  })
"""
    tools_anchor="\n  // ── 3. 工具执行前：同步 decision 门控 ──\n"
    if tools_anchor not in content:return content
    return content.replace(tools_anchor,model_listener+tools_anchor,1)

def merge_structured_patch(plan, workspace):
    """Preserve unrelated YAML patch entries on a reused profile.

    A learned plan records the complete patch file seen during investigation.
    A compatible new instance may already contain other user/administrator
    entries, so requiring the historical whole-file digest would turn a safe
    insertion into a false corruption error.  For the narrow, auditable shape
    used by loader patch layers (a top-level YAML list containing only
    ``insert`` groups with stable ids), append the learned group and bind the
    transaction precondition to the *current* bytes. Unknown YAML shapes and
    conflicting ids still fail closed.
    """
    try:import yaml
    except ImportError:return plan
    for item in plan['files']:
        path=workspace/item['path']
        if path.suffix.lower() not in ('.yaml','.yml') or not path.is_file():continue
        try:
            desired=yaml.safe_load(item['content'])
            current_text=path.read_text()
            current=yaml.safe_load(current_text)
        except (OSError,UnicodeError,yaml.YAMLError):continue
        def insertions(value):
            if not isinstance(value,list) or not value:return None
            found=[]
            for group in value:
                if not isinstance(group,dict) or set(group)!= {'insert'} or not isinstance(group['insert'],list) or not group['insert']:
                    return None
                for entry in group['insert']:
                    if not isinstance(entry,dict) or not isinstance(entry.get('id'),str) or not entry['id'] or not isinstance(entry.get('name'),str):
                        return None
                    found.append(entry)
            return found
        wanted=insertions(desired);present=insertions(current)
        if wanted is None or present is None:continue
        # Supersede only the legacy ASG control gate that this project itself
        # installed. Leaving it enabled would make two controllers race and a
        # stale local client could deny before the new SOC-direct Hook runs.
        # Observation remains loaded, unrelated entries are untouched, and the
        # transaction backup restores the exact original YAML on uninstall.
        migrated=False
        if any(entry.get('id')=='asg-observer' for entry in wanted):
            for entry in present:
                control=((entry.get('config') or {}).get('control') or {})
                if (entry.get('id')=='asg-runtime-observer'
                        and 'asg-runtime-observer' in entry.get('name','')
                        and control.get('enabled') is True):
                    control['enabled']=False;migrated=True
        if migrated:
            current_text=yaml.safe_dump(current,sort_keys=False,allow_unicode=True)
        by_id={entry['id']:entry for entry in present}
        additions=[]
        for entry in wanted:
            prior=by_id.get(entry['id'])
            if prior is not None and prior!=entry:
                raise ValueError('conflicting YAML patch id: '+entry['id'])
            if prior is None:additions.append(entry)
        if additions:
            # Append the already validated source fragment verbatim. This
            # preserves comments, custom tags and formatting in the target.
            merged=current_text.rstrip()+"\n"+item['content'].lstrip()
        else:
            merged=current_text
        item['content']=merged
        item['expected_sha256']=hashlib.sha256(current_text.encode()).hexdigest()
    return plan

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--platform');p.add_argument('--target',required=True)
    p.add_argument('--exe',required=True);p.add_argument('--entry');p.add_argument('--asg-root')
    p.add_argument('--backend-url');p.add_argument('--agent-id');p.add_argument('--agent-name',default='')
    p.add_argument('--token-file');p.add_argument('--instance-id',default='')
    p.add_argument('--dry-run',action='store_true')
    p.add_argument('--upgrade',action='store_true')
    p.add_argument('--rebind',action='store_true')
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
            item['content']=wire_model_request_payload(item['content'])
        cfg={'backend_url':a.backend_url,'agent_id':a.agent_id,'agent_name':a.agent_name,'platform':a.platform,'instance_id':a.instance_id or a.agent_id,'token_file':str(asg_root/'agent.key')}
        for rel,content in [('runtime/hook_control_client.mjs',(ROOT/'soc_client.mjs').read_text()),
                            # Preserve the previously managed Python file during
                            # migration so the transactional upgrader never
                            # silently drops a file it owns. New Hooks do not
                            # reference it.
                            ('runtime/hook_control_client.py',(ROOT/'soc_client.py').read_text()),
                            ('artifacts/autonomous-service/hook-control-client.json',json.dumps(cfg)),('agent.key',Path(a.token_file).read_text().strip())]:
            plan['files'].append({'path':'.soc-hook/'+rel,'content':content,'expected_sha256':None})
        plan=learned_install.validate_plan(plan)
    # Keep an unmerged copy. An upgrade/rebind first rolls the previous
    # transaction back; structured files must then be merged against that
    # restored baseline, not against the just-installed file that existed when
    # this process started.
    unmerged_plan=json.loads(json.dumps(plan))
    def prepare_plan():
        prepared=merge_structured_patch(json.loads(json.dumps(unmerged_plan)),workspace)
        # Full file contents are present: absent files are a clean install,
        # never an overwrite.
        for item in prepared['files']:
            target=workspace/item['path']
            if not target.exists():
                item['expected_sha256']=None
            else:
                current=target.read_bytes()
                desired=item['content'].encode()
                # A later instance may reuse files this same verified package
                # already owns. Treat an exact desired byte match as an
                # idempotent precondition; any differing byte still has to
                # match the recipe's explicit precondition or installation
                # fails closed below.
                if hashlib.sha256(current).digest()==hashlib.sha256(desired).digest():
                    item['expected_sha256']=hashlib.sha256(current).hexdigest()
        return learned_install.validate_plan(prepared)
    plan=prepare_plan()
    binding={'transport':'soc-direct-v1' if direct else 'asg-local','backend_url':a.backend_url,'agent_id':a.agent_id}
    receipt=state/'package-receipt.json'
    package_changed=False
    activation_affecting_change=False
    reuse_existing=False
    if receipt.exists() and not a.uninstall:
        previous=json.loads(receipt.read_text())
        prior_binding=previous.get('binding',{'transport':'asg-local','backend_url':None,'agent_id':None})
        binding_changed=prior_binding != binding
        content_changed=(previous.get('bundle_digest') != bundle['integrity']['digest']
                         or previous.get('installer_revision') != installer_revision)
        if a.rebind and content_changed:
            raise ValueError('identity rebind requires the same verified package and installer revision')
        if binding_changed and not (a.upgrade or a.rebind):
            raise ValueError('installed transport/identity differs; use explicit upgrade to rebind this verified package')
        package_changed=content_changed or binding_changed
        if not package_changed:
            prior_files={item['path']:item['content'] for item in previous.get('installed_plan',{}).get('files',[])}
            desired_files={item['path']:item['content'] for item in plan['files']}
            reuse_existing=(prior_files.keys()==desired_files.keys() and all(
                (workspace/name).is_file()
                and (name in RUNTIME_MUTABLE
                     or hashlib.sha256((workspace/name).read_bytes()).hexdigest()==hashlib.sha256(content.encode()).hexdigest())
                for name,content in desired_files.items()))
        if package_changed:
            if not (a.upgrade or (a.rebind and binding_changed and not content_changed)):
                raise ValueError('another package is installed; uninstall it before changing recipes')
            # Upgrade in place only from bytes owned by the prior verified
            # receipt. Rolling back first is incorrect after multiple releases:
            # it restores an older package whose bytes need not satisfy the
            # newest recipe's original preconditions. The new transaction
            # backs up this exact prior version and remains reversible.
            prior_files={item['path']:item['content'] for item in previous.get('installed_plan',{}).get('files',[])}
            desired_files={item['path']:item['content'] for item in plan['files']}
            transport_only=RUNTIME_MUTABLE|{'.soc-hook/agent.key'}
            activation_affecting_change=any(
                prior_files.get(name)!=content for name,content in desired_files.items()
                if name not in transport_only)
            desired_paths={item['path'] for item in plan['files']}
            removed=set(prior_files)-desired_paths
            if removed:
                raise ValueError('verified package upgrade removes managed files; uninstall is required first')
            for name,content in prior_files.items():
                target=workspace/name
                if name in RUNTIME_MUTABLE:
                    continue
                if (not target.is_file()
                        or hashlib.sha256(target.read_bytes()).hexdigest()!=hashlib.sha256(content.encode()).hexdigest()):
                    raise ValueError('managed file changed; refusing package upgrade: '+name)
            for item in plan['files']:
                if item['path'] in prior_files:
                    target=workspace/item['path']
                    item['expected_sha256']=hashlib.sha256(target.read_bytes()).hexdigest()
                    if item['path'] in RUNTIME_MUTABLE:
                        # Preserve only the SOC-issued policy. Package/rebind
                        # arguments remain authoritative for agent identity,
                        # instance identity, endpoint and token path.
                        current_config=json.loads(target.read_text())
                        desired_config=json.loads(item['content'])
                        if 'policy' in current_config:
                            desired_config['policy']=current_config['policy']
                        item['content']=json.dumps(desired_config)
            plan=learned_install.validate_plan(plan)
    if a.verify_pid:
        import psutil
        process=psutil.Process(a.verify_pid)
        if Path(process.exe()).resolve()!=exe:raise ValueError('verification process executable differs')
        previous=json.loads(receipt.read_text())
        for item in previous['installed_plan']['files']:
            if item['path'] in RUNTIME_MUTABLE:continue
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
        rolled=[]
        for approved in [previous['plan_digest']]+list(reversed(previous.get('rollback_chain',[]))):
            # SOC may legitimately change the runtime policy after install.
            # Rebase only that explicitly mutable file's rollback guard to its
            # current bytes; all executable/configuration files retain strict
            # byte-for-byte rollback protection.
            manifest_path=state/(approved+'.json')
            manifest=json.loads(manifest_path.read_text())
            changed=False
            for change in manifest.get('changes',[]):
                if change.get('path') not in RUNTIME_MUTABLE:continue
                current=workspace/change['path']
                if current.is_file():
                    change['after_sha256']=digest(current);changed=True
            if changed:manifest_path.write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
            step=learned_install.rollback(workspace,state,approved_workspace=workspace,approved_digest=approved)
            rolled.append(step)
        result={'status':'rolled_back','transactions':rolled}
    else:
        if reuse_existing:
            result={'status':'already_installed','plan_digest':previous['plan_digest'],
                    'manifest':str(state/(previous['plan_digest']+'.json')),
                    'activation':'unverified'}
        elif a.dry_run:
            print(json.dumps({'status':'validated','files':[f['path'] for f in plan['files']],'target':str(workspace),'activation':'unverified'}));return
        else:
            workspace.mkdir(parents=True,exist_ok=True)
            state.mkdir(parents=True,exist_ok=True)
            result=learned_install.install(plan,workspace,state,approved_workspace=workspace,approved_digest=learned_install.plan_digest(plan))
            if receipt.exists() and package_changed and not activation_affecting_change:
                # Installer-only releases do not require the Agent to reload an
                # unchanged Hook. A callback from this same process remains
                # valid; this also repairs receipts written by older releases
                # that reset installed_at for installer-only changes.
                try:
                    process_started=float(str(a.instance_id).split(':',1)[1])
                except (ValueError,AttributeError,IndexError):
                    process_started=previous.get('installed_at',time.time())
                installed_at=min(previous.get('installed_at',time.time()),process_started)
            else:
                installed_at=(time.time() if package_changed else previous.get('installed_at',time.time())) if receipt.exists() else time.time()
            chain=(list(previous.get('rollback_chain',[]))+[previous['plan_digest']]
                   if receipt.exists() and package_changed else
                   list(previous.get('rollback_chain',[])) if receipt.exists() else [])
            receipt.write_text(json.dumps({**result,'bundle_digest':bundle['integrity']['digest'],'installer_revision':installer_revision,'installed_plan':plan,'binding':binding,'installed_at':installed_at,'rollback_chain':chain}));receipt.chmod(0o600)
    print(json.dumps(result))
if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'status':'failed','error':str(exc)}),file=sys.stderr);sys.exit(1)
