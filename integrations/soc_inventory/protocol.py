"""SOC v1 static inventory. Runs on the endpoint, never on the SOC host.

Frozen P0 roots are contract compatibility, learned roots are explicit additional
ASG evidence. Neither file presence nor a heartbeat proves runtime invocation.
"""
import hashlib
import json
import os
import time
import uuid
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from .collector import manifest, read_bytes, safe_definition


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def config(path):
    if not path.exists(): return {}
    data=read_bytes(path).decode()
    if path.suffix=='.toml':
        import tomlkit
        return tomlkit.parse(data)
    if path.suffix in ('.yaml','.yml'):
        import yaml
        return yaml.safe_load(data) or {}
    import json5
    return json5.loads(data)


def project_chain(cwd):
    cwd=Path(cwd).resolve()
    for p in (cwd,*cwd.parents):
        if (p/'.git').exists():
            chain=[]; cursor=cwd
            while True:
                chain.append(cursor)
                if cursor==p: return chain
                cursor=cursor.parent
    return [cwd]


def roots(platform, cwd, home=None, env=None):
    env=os.environ if env is None else env
    home=Path(home or Path.home()).resolve(); cwd=Path(cwd).resolve()
    chain=project_chain(cwd); skill=[]; mcps=[]; settings=[]; errors=[]
    # Prompt files and model-config files are collected as real inventory
    # categories. Prompt roots use the cross-agent convention file names
    # (AGENTS.md and friends), never brand-specific guesses.
    prompt_names=('AGENTS.md','SOUL.md','CLAUDE.md','QWEN.md','GEMINI.md')
    prompt=[]; models=[]
    def read(p):
        try: value=config(p); settings.append(value); return value
        except Exception: errors.append(str(p)); return {}
    if platform=='codex':
        base=Path(env.get('CODEX_HOME',home/'.codex'))
        skill=[p/'.agents/skills' for p in chain]+[home/'.agents/skills',base/'skills']
        if os.name!='nt': skill.append(Path('/etc/codex/skills'))
        mcps=[(base/'config.toml','mcp_servers'),(cwd/'.codex/config.toml','mcp_servers'),(chain[-1]/'.codex/config.toml','mcp_servers')]
        for p,_ in mcps:read(p)
        models=[p for p,_ in mcps]
        prompt.extend([base/'AGENTS.md',home/'AGENTS.md',*[p/'AGENTS.md' for p in chain]])
    elif platform=='hermes':
        base=Path(env.get('HERMES_HOME',home/'.hermes')); data=read(base/'config.yaml')
        skill=[base/'skills',*[Path(p).expanduser() for p in data.get('skills',{}).get('external_dirs',[])]]
        if env.get('HERMES_OPTIONAL_SKILLS_DIR'):skill.append(Path(env['HERMES_OPTIONAL_SKILLS_DIR']))
        mcps=[(base/'config.yaml','mcp_servers')]
        models=[base/'config.yaml']
        prompt.append(base/'AGENTS.md')
    elif platform=='opencode':
        base=Path(env.get('OPENCODE_CONFIG_DIR',home/'.config/opencode'))
        skill=[base/'skills',home/'.agents/skills',home/'.claude/skills']
        for p in chain:skill.extend([p/'.opencode/skills',p/'.agents/skills',p/'.claude/skills'])
        mcps=[(base/'opencode.json','mcp'),(cwd/'opencode.json','mcp')]
        for p,_ in mcps:
            data=read(p)
            for extra in data.get('skills',[]) if isinstance(data.get('skills'),list) else []:
                if isinstance(extra,str) and '://' not in extra:
                    q=Path(extra).expanduser();skill.append(q if q.is_absolute() else p.parent/q)
        models=[p for p,_ in mcps]
        prompt.extend([base/'AGENTS.md',home/'AGENTS.md',*[p/'AGENTS.md' for p in chain]])
    elif platform=='openclaw':
        base=Path(env.get('OPENCLAW_STATE_DIR',home/'.openclaw')); p=base/'openclaw.json'; data=read(p)
        agents=data.get('agents',{}); default=(agents.get('defaults',{}) if isinstance(agents,dict) else {}).get('workspace')
        workspace=Path(default).expanduser() if default else home/'.openclaw/workspace'
        # Explicit instance workspace takes precedence; never bind other agents' roots.
        if cwd!=home:workspace=cwd
        skill=[base/'skills']
        if p.exists() and str(p) not in errors:skill.extend([workspace/'skills',workspace/'.agents/skills'])
        else: errors.append('workspace 未解析')
        if base.resolve()==(home/'.openclaw').resolve():skill.append(home/'.agents/skills')
        skill.extend(Path(p).expanduser() for p in data.get('skills',{}).get('load',{}).get('extraDirs',[]))
        mcps=[(p,'mcp.servers')]
        models=[p]
        prompt.extend([base/'AGENTS.md',workspace/'AGENTS.md',workspace/'SOUL.md'])
    else:
        # Unknown platform: generic evidence-based root discovery. Only dirs/files
        # the agent itself actually uses qualify - never a blind home sweep.
        # 1) Generic cross-agent conventions (shared dirs, not brand-specific).
        # 2) Explicit env vars declared by collection_environment (ZCODE_HOME etc.).
        # 3) Workspace project dirs: .agents/skills, .claude/skills under the chain.
        for p in chain:
            skill.extend([p/'.agents/skills', p/'.claude/skills'])
        skill.append(home/'.agents/skills')
        # Env-declared roots: any *_HOME/*_CONFIG_DIR/*_CONFIG_PATH the process
        # exported. We look for skills/config below each declared root.
        declared=[]
        for key,value in env.items():
            if not value or key=='PATH':continue
            if key in ('HOME','USERPROFILE') or key.endswith(('_HOME','_CONFIG_DIR','_CONFIG_PATH')):
                try:candidate=Path(value).expanduser()
                except (OSError,RuntimeError):continue
                if candidate.is_absolute() and candidate.is_dir():
                    declared.append(candidate)
        for base in declared[:8]:
            skill.append(base/'skills')
            for name in ('config.toml','config.json','config.jsonc','settings.json','settings.toml'):
                f=base/name
                if f.is_file():
                    # Discover the MCP servers block by structure, not by key name.
                    # Generic unknown platforms can't rely on a fixed field name.
                    data=read(f)
                    field=_first_mcp_field(data)
                    if field: mcps.append((f,field))
                    models.append(f)
            prompt.extend(base/name for name in prompt_names)
        # Workspace config files (generic names only, never brand-specific paths).
        for name in ('config.toml','config.json','settings.json','mcp.json'):
            f=cwd/name
            if f.is_file():
                # Same structural probe as the env-declared roots above: unknown
                # platforms spell the block mcpServers/mcp/servers, not only
                # mcp_servers; a fixed field name reported a false "0 MCP"
                # (crazytest B21). mcp.json puts servers at the document root.
                data=read(f)
                field=_first_mcp_field(data)
                if field: mcps.append((f,field))
                elif name=='mcp.json' and isinstance(data,dict) and any(
                        isinstance(v,dict) and any(k in v for k in ('command','url','transport','type'))
                        for v in data.values()):
                    mcps.append((f,''))
                models.append(f)
        for p in chain:prompt.extend(p/name for name in prompt_names)
        prompt.append(home/'AGENTS.md')

    def existing(paths):
        return list(dict.fromkeys(p.resolve() for p in paths if p.is_file()))
    return {'skill':list(dict.fromkeys(p.resolve() for p in skill)),
            'mcp':list(dict.fromkeys((p.resolve(),k) for p,k in mcps)),
            'settings':settings,'errors':errors,
            'prompt':existing(prompt),
            'model':existing(models)}


def _first_mcp_field(data, prefix=()):
    """Locate the MCP servers block by declaration structure, not brand paths."""
    if not isinstance(data, dict):
        return None
    server_keys = ('mcpServers', 'mcp_servers', 'mcp', 'servers')
    for key, value in data.items():
        if key in server_keys:
            if isinstance(value, dict) and any(
                isinstance(d, dict) and any(k in d for k in ('command', 'url', 'transport', 'type'))
                for d in value.values()
            ):
                return '.'.join((*prefix, key))
            if isinstance(value, dict) and isinstance(value.get('servers'), dict) and any(
                isinstance(d, dict) and any(k in d for k in ('command', 'url', 'transport', 'type'))
                for d in value['servers'].values()
            ):
                return '.'.join((*prefix, key, 'servers'))
        found = _first_mcp_field(value, (*prefix, key))
        if found:
            return found
    return None


def skill_scope(root, platform, settings, deadline):
    root=Path(root).resolve(); key='skill:'+str(root)
    result={'scope_key':key,'status':'success','expected_count':0,'items':[],'skipped_symlink':0}
    visited=set()
    def walk(folder,depth):
        if time.monotonic()>deadline:raise TimeoutError()
        actual=folder.resolve()
        if not actual.is_relative_to(root):result['skipped_symlink']+=1;return
        if actual in visited:return
        visited.add(actual)
        entry=folder/'SKILL.md'
        if entry.is_file():
            if not entry.resolve().is_relative_to(root):result['skipped_symlink']+=1;return
            if len(result['items'])>=2000:
                result.update(status='partial',error_code='INVENTORY_SCOPE_TRUNCATED');return
            h=hashlib.sha256(); size=0; data=b''
            with entry.open('rb') as f:
                while True:
                    if time.monotonic()>deadline:raise TimeoutError()
                    chunk=f.read(65536)
                    if not chunk:break
                    h.update(chunk);size+=len(chunk)
                    if size<=1024*1024:data+=chunk
            try: info=manifest(data,folder.name) if size<=1024*1024 else {}
            except Exception:info={}
            relative=folder.relative_to(root).as_posix(); installation=key+'/'+relative
            enabled=True
            for setting in settings:
                if platform=='codex':
                    for item in setting.get('skills',{}).get('config',[]):
                        q=item.get('path')
                        if q and Path(q).expanduser().resolve() in (folder.resolve(),entry.resolve()) and item.get('enabled') is False:enabled=False
                if platform=='openclaw' and setting.get('skills',{}).get('entries',{}).get(info.get('name',folder.name),{}).get('enabled') is False:enabled=False
            result['items'].append({**info,'name':info.get('name',folder.name),'validation_status':info.get('validation_status','invalid'),
                'relative_path':relative,'installation_key':installation,'identity_key':'path:'+installation,
                'identity_status':'unverified','manifest_digest':h.hexdigest(),'fingerprint_version':'skill-md-v1',
                'enabled':enabled,'content_status':'metadata_only'})
            return
        if depth>=6:return
        for child in sorted(folder.iterdir()):
            if child.is_dir():walk(child,depth+1)
    try:
        root.stat()
    except FileNotFoundError:return result
    except OSError:
        result.update(status='failed',error_code='INVENTORY_SCOPE_READ_FAILED');return result
    try:walk(root,0)
    except TimeoutError:result.update(status='failed',error_code='INVENTORY_SCOPE_TIMEOUT')
    except OSError:result.update(status='failed',error_code='INVENTORY_SCOPE_READ_FAILED')
    result['expected_count']=len(result['items']);return result


def mcp_scope(path, field):
    key='mcp:'+str(path);result={'scope_key':key,'status':'success','items':[],'expected_count':0}
    try:
        value=config(path)
        # An empty field means the servers block is the document root (mcp.json).
        for part in field.split('.'):
            if part:
                value=value.get(part,{})
        if not isinstance(value,dict):raise ValueError('invalid MCP block')
        for name,definition in sorted(value.items()):
            if not isinstance(definition,dict):raise ValueError('invalid MCP definition')
            safe=safe_definition(definition)
            safe['auth_type']='configured' if safe['credential_ref'] else 'unknown'
            safe['description']=str(definition.get('description',''))[:1200]
            # Arguments can embed arbitrary credentials: report explicit redaction.
            safe['args']=[];safe['args_redacted']=bool(definition.get('args') or isinstance(definition.get('command'),list))
            installation=key+'/'+name
            result['items'].append({**safe,'name':name,'installation_key':installation,'identity_key':'path:'+installation,
                'definition_digest':sha(canonical(safe)),'fingerprint_version':'mcp-config-v1'})
    except Exception:result.update(status='failed',error_code='INVENTORY_SCOPE_PARSE_FAILED')
    result['expected_count']=len(result['items']);return result


def prompt_scope(path, deadline=None):
    """Prompt-file inventory: identity by raw digest, never the content body."""
    path=Path(path);key='prompt:'+str(path.parent);name=path.name
    result={'scope_key':key,'status':'success','items':[],'expected_count':0}
    try:
        if deadline is not None and time.monotonic()>deadline:raise TimeoutError()
        h=hashlib.sha256();size=0
        with path.open('rb') as f:
            while True:
                if deadline is not None and time.monotonic()>deadline:raise TimeoutError()
                chunk=f.read(65536)
                if not chunk:break
                h.update(chunk);size+=len(chunk)
        installation=key+'/'+name
        result['items'].append({'name':name,'relative_path':name,'installation_key':installation,
            'identity_key':'path:'+installation,'identity_status':'unverified',
            'manifest_digest':h.hexdigest(),'fingerprint_version':'prompt-file-v1',
            'size_bytes':size,'content_status':'metadata_only'})
    except TimeoutError:result.update(status='failed',error_code='INVENTORY_SCOPE_TIMEOUT')
    except OSError:result.update(status='failed',error_code='INVENTORY_SCOPE_READ_FAILED')
    result['expected_count']=len(result['items']);return result


MODEL_REDACT=('token','key','secret','password','auth','credential','cookie','header')


def _model_redacted_keys(value):
    found=set()
    if isinstance(value,dict):
        for k,v in value.items():
            if any(t in str(k).lower() for t in MODEL_REDACT):found.add(str(k))
            else:found|=_model_redacted_keys(v)
    elif isinstance(value,list):
        for item in value:found|=_model_redacted_keys(item)
    return found


def _safe_model_value(value,out=None):
    """Copy model config structure with values that can carry credentials removed."""
    out={} if out is None else out
    if isinstance(value,dict):
        for k,v in value.items():
            lk=str(k).lower()
            if any(t in lk for t in MODEL_REDACT):continue
            if isinstance(v,dict):
                nested={}
                _safe_model_value(v,nested)
                if nested:out[str(k)]=nested
            elif isinstance(v,str):
                if lk in ('url','baseurl','base_url','endpoint') and '://' in v:
                    parts=urlsplit(v)
                    out[str(k)]=urlunsplit((parts.scheme,parts.hostname or '','','',''))
                else:out[str(k)]=v[:200]
            elif isinstance(v,(int,float,bool)):out[str(k)]=v
            elif isinstance(v,list):out[str(k)]=[x for x in v if isinstance(x,(str,int,float,bool))][:20]
    return out


def model_scope(path):
    """Model inventory from static config: default model plus provider blocks.

    Only declarations count here; a config entry never proves the model is
    currently being used - that evidence belongs to runtime observation.
    """
    key='model:'+str(path);result={'scope_key':key,'status':'success','items':[],'expected_count':0}
    try:
        data=config(path)
        if not isinstance(data,dict):raise ValueError('invalid model config')
        entries={}
        default=data.get('model')
        if isinstance(default,str) and default.strip():entries['default']={'default_model':default[:200]}
        elif isinstance(default,dict):entries['default']=_safe_model_value(default)
        for field in ('model_providers','providers','models'):
            block=data.get(field)
            if isinstance(block,dict):
                for name,definition in block.items():
                    if isinstance(definition,dict):
                        safe=_safe_model_value(definition)
                        refs=sorted(_model_redacted_keys(definition))
                        if refs:safe['credential_ref']=refs;safe['auth_type']='configured'
                        entries[str(name)]=safe
        # Nested layouts (agents.defaults.model etc.) qualify only through
        # providers; anything else stays metadata-only under 'default'.
        for name,safe in sorted(entries.items()):
            installation=key+'/'+name
            result['items'].append({**safe,'name':name,'installation_key':installation,
                'identity_key':'path:'+installation,'identity_status':'unverified',
                'definition_digest':sha(canonical(safe)),'fingerprint_version':'model-config-v1'})
    except Exception:result.update(status='failed',error_code='INVENTORY_SCOPE_PARSE_FAILED')
    result['expected_count']=len(result['items']);return result


def bounded_reader(operation, args, deadline):
    remaining=min(15,deadline-time.monotonic())
    if remaining<=0:raise TimeoutError('collection deadline exceeded')
    try:
        result=subprocess.run([sys.executable,'-m','integrations.soc_inventory.collection_worker'],
            input=json.dumps({'operation':operation,'args':args},default=str),text=True,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=remaining,
            cwd=str(Path(__file__).resolve().parents[2]),check=True)
    except subprocess.TimeoutExpired as exc:raise TimeoutError('collection deadline exceeded') from exc
    return json.loads(result.stdout)


def workspace_identity(agent, home=None, env=None):
    if agent.get('workspace_id'):return agent['workspace_id']
    environment=os.environ if env is None else env
    cwd=Path(agent['workspace']).resolve();platform=agent['platform']
    if platform in ('codex','opencode'):
        top=project_chain(cwd)[-1]
        return str(top) if (top/'.git').exists() else 'nogit:'+str(cwd)
    if platform=='hermes':
        base=Path(environment.get('HERMES_HOME',Path(home or Path.home())/'.hermes')).expanduser().resolve()
        return 'hermes:'+str(base)
    return str(cwd)


def scope_present(path, key, previous):
    # Never interpret inaccessible directories as an empty successful scan.
    try:path.stat();return True
    except FileNotFoundError:return key in previous
    except OSError:return True


def collect_contract(agent, epoch, revision, home=None, env=None, previous_scopes=(), bounded=False):
    platform=agent['platform'];cwd=agent['workspace'];deadline=time.monotonic()+60
    if bounded:
        try:
            plan=bounded_reader('roots',{'platform':platform,'cwd':cwd,'home':home,'env':env},deadline)
            skill=[Path(p) for p in plan['skill']];mcps=[(Path(p),k) for p,k in plan['mcp']]
            prompts=[Path(p) for p in plan.get('prompt',[])];models=[Path(p) for p in plan.get('model',[])]
            settings=plan['settings'];errors=plan['errors']
        except (TimeoutError,subprocess.SubprocessError,ValueError):
            skill=[];mcps=[];prompts=[];models=[];settings=[];errors=['INVENTORY_ROOT_DISCOVERY_FAILED']
    else:
        plan=roots(platform,cwd,home,env)
        skill=plan['skill'];mcps=plan['mcp'];prompts=plan['prompt'];models=plan['model'];settings=plan['settings'];errors=plan['errors']
    # Roots learned by ASG are explicit evidence, not guessed brand-specific paths.
    skill.extend(Path(p).resolve() for p in agent.get('learned_skill_roots',[]))
    mcps.extend((Path(p['path']).resolve(),p['field']) for p in agent.get('learned_mcp_configs',[]))
    prompts.extend(Path(p).resolve() for p in agent.get('learned_prompt_files',[]))
    models.extend(Path(p).resolve() for p in agent.get('learned_model_configs',[]))
    def scope_key(kind,path):
        return ('skill:' if kind=='skill' else 'mcp:' if kind=='mcp' else 'prompt:' if kind=='prompt' else 'model:')+str(path)
    def read_scope(kind,path,field=None):
        if not bounded:
            if kind=='skill':return skill_scope(path,platform,settings,min(deadline,time.monotonic()+15))
            if kind=='mcp':return mcp_scope(path,field)
            if kind=='prompt':return prompt_scope(path,min(deadline,time.monotonic()+15))
            return model_scope(path)
        args={'root':str(path),'platform':platform,'settings':settings} if kind=='skill' else {'path':str(path),'field':field} if kind=='mcp' else {'path':str(path)}
        args['previous_scopes']=list(previous_scopes)
        try:return bounded_reader(kind,args,deadline)
        except (TimeoutError,subprocess.SubprocessError,ValueError) as exc:
            return {'scope_key':scope_key(kind,path),'status':'failed','items':[],'expected_count':0,'error_code':'INVENTORY_SCOPE_TIMEOUT' if isinstance(exc,TimeoutError) else 'INVENTORY_SCOPE_READ_FAILED'}
    scopes={'skill':[read_scope('skill',p) for p in dict.fromkeys(skill) if bounded or scope_present(p,"skill:"+str(p),previous_scopes)],
            'mcp_server':[read_scope('mcp',p,k) for p,k in dict.fromkeys(mcps) if bounded or scope_present(p,"mcp:"+str(p),previous_scopes)],
            'prompt':[read_scope('prompt',p) for p in dict.fromkeys(prompts) if bounded or scope_present(p,"prompt:"+str(p.parent),previous_scopes)],
            'model':[read_scope('model',p) for p in dict.fromkeys(models) if bounded or scope_present(p,"model:"+str(p),previous_scopes)]}
    scopes={key:[scope for scope in values if scope is not None] for key,values in scopes.items()}
    categories={key:{'status':'unsupported'} for key in ('tool','mcp_tool')}
    for key,values in scopes.items():
        statuses={s['status'] for s in values}
        status='failed' if statuses=={'failed'} else 'partial' if statuses-{'success'} else 'success'
        if 'INVENTORY_ROOT_DISCOVERY_FAILED' in errors:status='failed'
        elif errors and key=='skill':status='partial'
        if not values and platform not in ('codex','hermes','opencode','openclaw') and key in ('skill','mcp_server'):status='failed'
        categories[key]={'status':status,'scopes':values}
        if errors and key=='skill':categories[key]['errors']=errors
    return {'schema_version':1,'agent_id':agent['agent_id'],'platform':platform,'channel':'desktop',
        'collector_id':'soc-inventory-'+platform,'collector_version':'1.0.0','workspace_id':workspace_identity(agent,home,env),
        'collector_epoch':epoch,'revision':revision,'snapshot_id':str(uuid.uuid4()),'mode':'full',
        'collected_at':datetime.now(timezone.utc).isoformat(),'part_index':0,'part_count':1,'categories':categories}
