"""Instance- and installed-byte-bound records from independent acceptance runners."""
import hashlib
import json
import os
import time
from pathlib import Path
import psutil
from runtime.learned_install import _atomic

def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def root():return Path(os.environ.get('ASG_RUN_DIR','artifacts/stage1/dashboard')).resolve()
def record(target, report_path, hook_files, checks):
    if abs(psutil.Process(target['pid']).create_time()-target['create_time'])>=.001:raise ValueError('target changed')
    value={'target':target,'report':str(Path(report_path).resolve()),'report_sha256':digest(report_path),
           'files':[{'path':str(Path(p).resolve()),'sha256':digest(p)} for p in hook_files],
           'checks':checks,'recorded_at':time.time(),'scope':'独立验收脚本的实际操作测试，仅限该实例与该 Hook 版本'}
    if not value['files']:raise ValueError('installed Hook files required')
    if not {'allow_effect','deny_effect'}.issubset(set(checks)):raise ValueError('independent allow and deny effect checks required')
    (root()/'hook-acceptance').mkdir(parents=True,exist_ok=True)
    _atomic(root()/'hook-acceptance'/f"{target['pid']}:{target['create_time']}.json",json.dumps(value,ensure_ascii=False).encode())
    return value

def snapshots():
    result=[]
    for p in (root()/'hook-acceptance').glob('*.json'):
        try:
            v=json.loads(p.read_text());t=v['target']
            current=abs(psutil.Process(t['pid']).create_time()-t['create_time'])<.001
            current=current and digest(v['report'])==v['report_sha256'] and all(digest(f['path'])==f['sha256'] for f in v['files'])
            result.append({**v,'current':current})
        except (ValueError,OSError,KeyError,psutil.Error):continue
    return result
