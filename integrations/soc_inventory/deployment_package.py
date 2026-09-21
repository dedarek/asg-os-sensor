"""Build deterministic standalone archives from learned full-file plans."""
import gzip
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path
from runtime import learned_install,recipe_bundle
from .protocol import canonical
ROOT=Path(__file__).resolve().parents[2]

def supports_direct_events(bundle):
    files=((bundle.get('recipe') or {}).get('install_plan') or {}).get('files') or []
    for item in files:
        content=item.get('content','') if isinstance(item,dict) else ''
        client_calls = all(re.search(r"client\s*\(\s*['\"]"+action+r"['\"]", content)
                           for action in ('event','decision','ack'))
        denied_ack = bool(re.search(r"outcome\s*:\s*['\"]blocked['\"]", content))
        learned_control_client = (client_calls and 'tools/pre-execute' in content
                                  and bool(re.search(r"kind\s*:\s*['\"]deny['\"]", content))
                                  and 'hook_control_client.py' in content and denied_ack)
        if ('ASG_SOC_DIRECT_PROTOCOL = True' in content
                or 'runControlClient("event"' in content
                or learned_control_client
                or ('function record(event, meta)' in content and 'appendLine(LOG_PATH, JSON.stringify(row));' in content and 'CONTROL_CONFIG' in content)):
            return True
    return False

def build(bundle):
    recipe_bundle.validate_bundle(bundle)
    learned_install.validate_plan(bundle['recipe']['install_plan'])
    portability=recipe_bundle.scan_portability(bundle)
    if portability['credential_hits'] or portability['chat_hits']:
        raise ValueError('installation package contains credentials or chat content')
    if portability['machine_paths'] or portability['target_env_paths']:
        raise ValueError('installation package contains unresolved machine paths')
    if not supports_direct_events(bundle):raise ValueError('installation package requires direct normalized event delivery')
    files={'transport.json':canonical({'transport':'soc-direct-v1','control':'before_tool_call','events':'user_model_tool_session','requires_asg_service':False,'installer_revision':installer_revision()}), 'soc_client.py':(Path(__file__).parent/'package_runtime/soc_client.py').read_bytes(),'recipe-bundle.json':canonical(bundle),'install.py':(Path(__file__).parent/'package_runtime/install.py').read_bytes(),
           'runtime/__init__.py':b'',
           'install/install.sh':b'#!/usr/bin/env sh\nset -eu\nexec python3 "$(dirname "$0")/../install.py" "$@"\n',
           'README.txt':b'ASG learned file-plan installation package. Install: bash install.sh --target WORKSPACE --exe AGENT_EXECUTABLE --platform TYPE --backend-url SOC_URL --agent-id ID --token-file KEY_FILE. The installed control client calls SOC directly and does not need a running ASG service. Use --dry-run to validate and uninstall.sh with the same arguments to roll back. Target-native approval may still be required. Installation is not activation proof.\n',
           'install.sh':b'#!/usr/bin/env sh\nset -eu\nexec python3 "$(dirname "$0")/install.py" "$@"\n',
           'uninstall.sh':b'#!/usr/bin/env sh\nset -eu\nexec python3 "$(dirname "$0")/install.py" --uninstall "$@"\n'}
    for name in ('learned_install.py','file_lock.py','recipe_bundle.py'):
        files['runtime/'+name]=(ROOT/'runtime'/name).read_bytes()
    out=io.BytesIO()
    with gzip.GzipFile(fileobj=out,mode='wb',mtime=0) as gz:
        with tarfile.open(fileobj=gz,mode='w') as tar:
            for name,data in sorted(files.items()):
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o700 if name.endswith('.sh') else 0o600
                tar.addfile(info,io.BytesIO(data))
    return out.getvalue()

def installer_revision():
    paths=[Path(__file__),Path(__file__).parent/'package_runtime/install.py',Path(__file__).parent/'package_runtime/soc_client.py']+[ROOT/'runtime'/name for name in ('learned_install.py','file_lock.py','recipe_bundle.py')]
    return hashlib.sha256(b''.join(p.read_bytes() for p in paths)).hexdigest()
