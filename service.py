"""Start/stop the local ASG service using an explicit deployment settings file."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import psutil

ROOT = Path(__file__).resolve().parent

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start','stop','status'))
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    settings = json.loads(Path(args.config).read_text())
    run = Path(settings['ASG_RUN_DIR']); run.mkdir(parents=True,exist_ok=True)
    record = run/'deployment.json'
    process = None
    if record.exists():
        meta = json.loads(record.read_text())
        try:
            candidate = psutil.Process(meta['pid'])
            if abs(candidate.create_time()-meta['create_time']) < .001:
                if candidate.cwd() != str(ROOT) or 'monitor_dashboard.py' not in candidate.cmdline():
                    raise RuntimeError('Deployment process identity does not match this service')
                process = candidate
        except psutil.NoSuchProcess: pass
    if args.action == 'status':
        print('running PID %s' % process.pid if process else 'stopped'); return
    if args.action == 'stop':
        if process:
            # Stop only this service's own active investigators before the service.
            children = process.children(recursive=True)
            for child in reversed(children):
                try: child.terminate()
                except psutil.NoSuchProcess: pass
            process.terminate(); process.wait(timeout=10)
        print('stopped'); return
    if process:
        print('already running PID %s' % process.pid); return
    env = os.environ.copy()
    for key in list(env):
        if key.startswith('ASG_'): env.pop(key)
    env.update(settings)
    with (run/'service.log').open('ab') as log:
        child = subprocess.Popen([sys.executable,'-u','-B','monitor_dashboard.py'],cwd=ROOT,
            env=env,stdout=log,stderr=log,start_new_session=True)
    record.write_text(json.dumps({'pid':child.pid,'create_time':psutil.Process(child.pid).create_time(),
                                  'port':int(settings.get('ASG_PORT',8080))}))
    print('started http://%s:%s/' % (settings.get('ASG_HOST','127.0.0.1'),settings.get('ASG_PORT','8080')))

if __name__ == '__main__': main()
