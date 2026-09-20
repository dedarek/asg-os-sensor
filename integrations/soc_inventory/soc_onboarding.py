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


def compatible(constraints, exe, agent_type, agent_version=None):
    if constraints.get('adapter')=='soc-native-v1':
        if constraints.get('agent_type')!=agent_type:return False
        # A native package may pin the host shape and build; absent keys stay
        # permissive so centrally built vendor packages keep working, while
        # any explicit pin (platform, architecture, version, executable or
        # bundle digest) must match the live target exactly.
        if constraints.get('platform') not in (None,platform.system()):return False
        if constraints.get('architecture') not in (None,platform.machine()):return False
        if not _version_ok(constraints,agent_version):return False
        if not _build_ok(constraints,exe):return False
        return True
    c=constraints.get('compatibility') or {}
    if c.get('platform')!=platform.system() or c.get('architecture')!=platform.machine() or c.get('runtime')!='native':return False
    if not _build_ok(c,exe):return False
    if not _version_ok(c,agent_version):return False
    return True


def install_target(agent):
    environment=agent.get('collection_environment') or {}
    preferred={
        'codex':'CODEX_HOME',
        'hermes':'HERMES_HOME',
        'openclaw':'OPENCLAW_STATE_DIR',
    }.get(agent['platform'])
    value=environment.get(preferred) if preferred else None
    if preferred and not value:
        # This platform's hook home is an environment-scoped standard
        # directory. Falling back to the process workspace would install the
        # managed hook next to an unrelated cwd; return None so the installer
        # applies its own documented standard-directory default instead.
        return None
    workspace=Path(agent['workspace']).expanduser().resolve()
    # A GUI process launched from the filesystem root has no meaningful project
    # workspace; return None so the installer applies its own standard-directory
    # defaults instead of writing next to the filesystem root.
    if str(workspace)==str(Path(workspace).anchor):return None
    return str(workspace)


def install(endpoint, agent, exe, execute=False):
    exe=Path(exe).resolve()
    catalog=endpoint.request(agent,'/api/asg/artifact/catalog',None,'GET')
    selected=next((x for x in catalog['items'] if compatible(x['constraints'],exe,agent['platform'],agent.get('agent_version'))),None)
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
    target=install_target(agent)
    if selected['transport']=='soc-native-v1':
        args=['bash',str(root/'install/install.sh'),'--platform',agent['platform'],'--backend-url',endpoint.url,'--agent-id',agent['agent_id'],'--agent-name',agent.get('name') or agent['platform'],'--token',key]
        if target is not None:args+=['--target',target]
    elif selected['transport']=='soc-direct-v1':
        if target is None:raise ValueError('learned package requires a concrete Agent workspace')
        args=[sys.executable,str(root/'install.py'),'--target',target,'--exe',str(exe),'--platform',agent['platform'],'--backend-url',endpoint.url,'--agent-id',agent['agent_id'],'--agent-name',agent.get('name') or agent['platform'],'--instance-id',agent.get('asg_instance_id',''),'--token-file',agent['key_file']]
    else:
        raise ValueError('unsupported SOC package transport')
    result=subprocess.run(args+([] if execute else ['--dry-run']),capture_output=True,text=True,timeout=45)
    output=(result.stdout or result.stderr).strip()
    try:body=json.loads(output)
    except json.JSONDecodeError:body={'status':'installed' if result.returncode==0 and execute else 'validated' if result.returncode==0 else 'failed','output':output[-4000:]}
    if result.returncode!=0:raise ValueError('SOC package installer failed: '+str(body.get('error') or body.get('output') or result.returncode))
    return {'status':body.get('status','installed' if result.returncode==0 else 'failed'),'artifact_id':selected['id'],'model_calls':0,'transport':selected['transport'],'result':body}
