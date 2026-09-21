"""SOC-first package selection. No model calls on a compatible catalog hit."""
import hashlib
import io
import json
from pathlib import Path
import platform
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import Request, build_opener, ProxyHandler
from urllib.parse import urlsplit


def _digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
    return h.hexdigest()


def _version_ok(pin, observed):
    # Pins are explicit release scopes: a wrong or missing version is a
    # mismatch, never a silent pass. Comparison is numeric on dot segments
    # with a lexicographic fallback for pre-release suffixes.
    def key(v):
        parts=[]
        for segment in str(v).split('.'):
            digits=''.join(ch for ch in segment if ch.isdigit())
            parts.append(int(digits) if digits else -1)
        return tuple(parts)
    if pin.get('version') and str(observed or '')!=str(pin['version']):return False
    if pin.get('min_version') and key(observed or '0')<key(pin['min_version']):return False
    if pin.get('max_version') and key(observed or '0')>key(pin['max_version']):return False
    return True


def _build_ok(constraints, exe):
    # Shared build-scope check for native and learned packages. Every pin is
    # optional, but once present it must match the live target exactly; an
    # unreadable executable can never satisfy a pinned build.
    def digest(path):
        try:return _digest(path)
        except OSError:return None
    if constraints.get('executable') is not None:
        observed=digest(exe)
        if observed is None or constraints['executable']!=observed:return False
    for key,value in constraints.items():
        if key.startswith('Contents/'):
            root=next((p for p in exe.parents if p.suffix=='.app'),None)
            if root is None:return False
            observed=digest(root/key)
            if observed is None or observed!=value:return False
    return True


# Each native installer in the SOC package tree implements exactly one
# extension mechanism per platform. A manifest that claims only protocol
# versions outside this set describes an integration this collector cannot
# honestly build, so automatic install must refuse and investigate instead.
NATIVE_PROTOCOLS={'codex':{'codex-hooks-v1'},'opencode':{'opencode-plugin-v1'},
                  'openclaw':{'openclaw-extension-v1'},
                  'hermes':{'hermes-plugin-v1'}}


def _scope_ok(pin, observed):
    # Lists declare the supported scope; an unknown membership never passes.
    if pin is None:return True
    if not isinstance(pin,list) or not pin or observed not in pin:return False
    return True


def compatible(constraints, exe, agent_type, agent_version=None, observed_compatibility=None):
    if constraints.get('adapter')=='soc-native-v1':
        if constraints.get('agent_type')!=agent_type:return False
        # Fail closed: a native package must ship an auditable compatibility
        # manifest (platform/architecture scope, protocol versions, optional
        # version/build pins). Undeclared packages no longer get blind trust.
        if constraints.get('compatibility_declared')is not True:return False
        # An explicit empty/invalid list stays an empty/invalid list: it must
        # never fall through to the absent-key default via truthiness.
        scopes=constraints.get('platforms')
        if scopes is None:scopes=constraints.get('platform')
        if not _scope_ok(scopes,platform.system()):return False
        archs=constraints.get('architectures')
        if archs is None:archs=constraints.get('architecture')
        if not _scope_ok(archs,platform.machine()):return False
        declared=constraints.get('protocol_versions')
        if isinstance(declared,list)and declared:
            known=NATIVE_PROTOCOLS.get(agent_type,set())
            if not known or not set(declared)&known:return False
        if not _version_ok(constraints,agent_version):return False
        if not _build_ok(constraints,exe):return False
        return True
    c=constraints.get('compatibility') or {}
    if c.get('platform')!=platform.system() or c.get('architecture')!=platform.machine():return False
    runtime=c.get('runtime')
    if runtime not in ('native','node','bun','python'):return False
    if not _build_ok(c,exe):return False
    if runtime!='native':
        observed=observed_compatibility or {}
        # Interpreter identity alone is meaningless: the script/module bytes
        # are the portable build pin. Launch path and cwd may differ between
        # machines, so they are intentionally excluded from package matching.
        for field in ('platform','architecture','runtime','executable','entry'):
            if not c.get(field) or c.get(field)!=observed.get(field):return False
        if c.get('entry')=='native':return False
    elif c.get('entry') not in (None,'native'):
        return False
    if not _version_ok(c,agent_version):return False
    return True


# The native installers write platform hooks/plugins into a per-platform home
# directory. Each platform's documented precedence matches its installer:
# explicit environment override first, then the installer's own default.
NATIVE_HOME_ENV={'codex':'CODEX_HOME','hermes':'HERMES_HOME','openclaw':'OPENCLAW_STATE_DIR'}


def native_home(agent):
    """Config home for soc-native-v1 packages: env override wins over default.

    Codex/OpenClaw/Hermes load hooks from their hook home, never from the
    process workspace; passing the workspace here would install next to an
    unrelated cwd. None defers to the installer's standard default.
    """
    environment=agent.get('collection_environment') or {}
    preferred=NATIVE_HOME_ENV.get(agent['platform'])
    if preferred:
        value=environment.get(preferred)
        if isinstance(value,str) and value.strip():return str(Path(value).expanduser())
        return None
    # opencode native install targets OPENCODE_CONFIG_DIR (installer default
    # ~/.config/opencode); its process workspace is a project directory.
    if agent['platform']=='opencode':return None
    return None


def install_target(agent):
    """Target for soc-direct-v1 learned plans: the concrete project workspace.

    Learned file plans are workspace-relative and the installer rejects
    anything else; the filesystem root is never a meaningful workspace.
    """
    # Learned integrations may live in a profile/config root distinct from the
    # process cwd (GUI/web launchers commonly run from / or a temp directory).
    # The investigation must provide that root explicitly; never guess it from
    # a product name or install next to an unrelated cwd.
    workspace=Path(agent.get('hook_workspace') or agent['workspace']).expanduser().resolve()
    # A GUI process launched from the filesystem root has no meaningful project
    # workspace; return None so the installer applies its own standard-directory
    # defaults instead of writing next to the filesystem root.
    if str(workspace)==str(Path(workspace).anchor):return None
    return str(workspace)


def integrity(endpoint, agent, prior):
    """Byte-check learned (soc-direct-v1) installs against their receipt.

    Returns True when every file recorded by the install receipt still matches
    its recorded digest, or when the transport has no local receipt to verify
    (native packages are audited through the target trust mechanism instead).
    A missing receipt for a direct install counts as drift.
    """
    if prior.get('transport') != 'soc-direct-v1':return True
    import hashlib
    ws=Path(agent['workspace']).expanduser().resolve()
    state=ws.parent/('.asg-install-'+hashlib.sha256(str(ws).encode()).hexdigest()[:16])
    receipt=state/'package-receipt.json'
    try:
        record=json.loads(receipt.read_text())
        # Install-time file digests live in the learned_install transaction
        # manifest named by the receipt's plan digest, not the receipt itself.
        tx=json.loads((state/(str(record.get('plan_digest'))+'.json')).read_text())
    except (OSError,json.JSONDecodeError):return False
    if tx.get('status')!='installed':return False
    for change in tx.get('changes',[]):
        path=ws/change['path']
        try:data=path.read_bytes()
        except OSError:return False
        if hashlib.sha256(data).hexdigest()!=change.get('after_sha256'):return False
    return True


def select(endpoint, agent, exe):
    """Current catalog choice for this instance, or None (needs investigation)."""
    exe=Path(exe).resolve()
    observed=None
    try:
        import psutil
        from runtime.compatibility import observe
        pid,started=agent['asg_instance_id'].split(':',1)
        process=psutil.Process(int(pid))
        if abs(process.create_time()-float(started))<=0.01:
            observed=observe(str(exe),process.cmdline(),process.cwd())
    except (KeyError,ValueError,psutil.Error,OSError):
        pass
    catalog=endpoint.request(agent,'/api/asg/artifact/catalog',None,'GET')
    return next((x for x in catalog['items'] if compatible(
        x['constraints'],exe,agent['platform'],agent.get('agent_version'),observed)),None)


def install(endpoint, agent, exe, execute=False, upgrade=False, selected=None):
    exe=Path(exe).resolve()
    if selected is None:selected=select(endpoint,agent,exe)
    if selected is None:return {'status':'needs_investigation','route':'protocol_then_goose','reason':'no_compatible_SOC_package','model_calls':0}
    key=Path(agent['key_file']).read_text().strip()
    route=selected['download_path']
    if not route.startswith('/api/asg/artifact/') or '..' in route:raise ValueError('invalid catalog download path')
    opener=build_opener(ProxyHandler({})) if urlsplit(endpoint.url).hostname in ('localhost','127.0.0.1','::1') else build_opener()
    with opener.open(Request(endpoint.url+route,headers={'Authorization':'Bearer '+key}),timeout=30) as response:data=response.read(2*1024*1024+1)
    if len(data)>2*1024*1024 or hashlib.sha256(data).hexdigest()!=selected['checksum']:raise ValueError('SOC package checksum mismatch')
    parent=Path(endpoint.config['state_dir'])/'install-packages';parent.mkdir(parents=True,exist_ok=True)
    root=Path(tempfile.mkdtemp(prefix='verified-',dir=parent))
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        members=archive.getmembers();seen=set();total=0
        for member in members:
            path=Path(member.name);total+=member.size
            if not (member.isfile() or member.isdir()) or path.is_absolute() or str(path)!=member.name.rstrip('/') or '\\' in member.name or '..' in path.parts or member.name in seen or total>3*1024*1024:raise ValueError('unsafe package archive')
            seen.add(member.name)
        archive.extractall(root,members=members)
    if execute and agent.get('asg_instance_id'):
        import psutil
        pid,started=agent['asg_instance_id'].split(':')
        process=psutil.Process(int(pid))
        if abs(process.create_time()-float(started))>0.01 or Path(process.exe()).resolve()!=exe or Path(process.cwd()).resolve()!=Path(agent['workspace']).resolve():
            raise ValueError('target instance changed during package download')
    if selected['transport']=='soc-native-v1':
        # Native packages must target the platform hook home (honouring the
        # instance's own env override), never the process workspace.
        home=native_home(agent)
        args=['bash',str(root/'install/install.sh'),'--platform',agent['platform'],'--backend-url',endpoint.url,'--agent-id',agent['agent_id'],'--agent-name',agent.get('name') or agent['platform'],'--token',key]
        if home is not None:args+=['--target',home]
    elif selected['transport']=='soc-direct-v1':
        target=install_target(agent)
        if target is None:raise ValueError('learned package requires a concrete Agent workspace')
        args=[sys.executable,str(root/'install.py'),'--target',target,'--exe',str(exe),'--platform',agent['platform'],'--backend-url',endpoint.url,'--agent-id',agent['agent_id'],'--agent-name',agent.get('name') or agent['platform'],'--instance-id',agent.get('asg_instance_id',''),'--token-file',agent['key_file']]
        compatibility=(selected.get('constraints') or {}).get('compatibility') or {}
        if compatibility.get('runtime')!='native':
            import psutil
            from runtime.compatibility import observe
            pid,started=agent['asg_instance_id'].split(':',1)
            process=psutil.Process(int(pid))
            if abs(process.create_time()-float(started))>0.01:raise ValueError('target instance changed before entrypoint validation')
            observed=observe(str(exe),process.cmdline(),process.cwd())
            if not observed or observed.get('entry')!=compatibility.get('entry'):
                raise ValueError('interpreter entrypoint build mismatch; investigate before installing')
            args+=['--entry',observed['entry_path']]
    else:
        raise ValueError('unsupported SOC package transport')
    if upgrade:
        # The learned installer understands --upgrade (rollback then install);
        # the native bash installer only understands --force (replace a target
        # it already manages). Passing the wrong flag must fail before writes.
        args+=['--upgrade' if selected['transport']=='soc-direct-v1' else '--force']
    result=subprocess.run(args+([] if execute else ['--dry-run']),capture_output=True,text=True,timeout=45)
    output=(result.stdout or result.stderr).strip()
    try:body=json.loads(output)
    except json.JSONDecodeError:body={'status':'installed' if result.returncode==0 and execute else 'validated' if result.returncode==0 else 'failed','output':output[-4000:]}
    if result.returncode!=0:raise ValueError('SOC package installer failed: '+str(body.get('error') or body.get('output') or result.returncode))
    return {'status':body.get('status','installed' if result.returncode==0 else 'failed'),'artifact_id':selected['id'],'checksum':selected['checksum'],'model_calls':0,'transport':selected['transport'],'result':body}
