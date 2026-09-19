#!/usr/bin/env python3
"""Install/remove the endpoint collector for this user; credentials stay in config/key files."""
import argparse
import json
import os
import plistlib
import shlex
import subprocess
import sys
from pathlib import Path

NAME='com.asg.soc-collector'
ROOT=Path(__file__).resolve().parents[1]

def run(*args):subprocess.run(args,check=True)
def status_report():
    if sys.platform=='darwin':
        probe=subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{NAME}'],capture_output=True,text=True)
        loaded=probe.returncode==0
        pid=next((line.split('=',1)[1].strip().strip('();') for line in probe.stdout.splitlines() if 'pid =' in line),None)
    elif os.name=='nt':
        probe=subprocess.run(['schtasks','/Query','/TN',NAME,'/FO','LIST'],capture_output=True,text=True)
        loaded=probe.returncode==0
        pid=next((line.split(':',1)[1].strip() for line in probe.stdout.splitlines() if line.lower().startswith('task to run')),None)
    else:
        probe=subprocess.run(['systemctl','--user','show',NAME,'-p','ActiveState','-p','MainPID'],capture_output=True,text=True)
        fields=dict(line.split('=',1) for line in probe.stdout.strip().splitlines() if '=' in line)
        loaded=fields.get('ActiveState')=='active';pid=fields.get('MainPID')
    return {'service':NAME,'loaded':loaded,'pid':pid or None}
def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['install','remove','status']);parser.add_argument('--config');args=parser.parse_args()
    if args.action=='status':
        print(json.dumps(status_report(),ensure_ascii=False));return
    command=[sys.executable,'-m','integrations.soc_inventory.endpoint','--config',str(Path(args.config).expanduser().resolve())] if args.config else []
    if args.action=='install':
        if not args.config:parser.error('--config required')
        config=json.loads(Path(args.config).expanduser().read_text())
        for field in ('backend_url','state_dir','agents'):
            if field not in config:parser.error('missing config field '+field)
        if os.name!='nt':Path(args.config).expanduser().chmod(0o600)
    if sys.platform=='darwin':
        path=Path.home()/'Library/LaunchAgents'/f'{NAME}.plist';domain=f'gui/{os.getuid()}'
        if args.action=='remove':
            subprocess.run(['launchctl','bootout',domain,str(path)],check=False)
            path.unlink(missing_ok=True);return
        path.parent.mkdir(parents=True,exist_ok=True)
        log=Path(config['state_dir']).expanduser();log.mkdir(parents=True,exist_ok=True)
        path.write_bytes(plistlib.dumps({'Label':NAME,'ProgramArguments':command,'WorkingDirectory':str(ROOT),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'StandardOutPath':str(log/'service.log'),'StandardErrorPath':str(log/'service.log')}))
        subprocess.run(['launchctl','bootout',domain,str(path)],check=False)
        run('launchctl','bootstrap',domain,str(path))
    elif os.name=='nt':
        if args.action=='remove':run('schtasks','/Delete','/TN',NAME,'/F');return
        # A launcher gives Task Scheduler an explicit working directory.
        launcher=Path(config['state_dir']).expanduser()/'start-collector.cmd';launcher.parent.mkdir(parents=True,exist_ok=True)
        launcher.write_text('@echo off\ncd /d '+subprocess.list2cmdline([str(ROOT)])+'\n'+subprocess.list2cmdline(command)+'\n')
        run('schtasks','/Create','/TN',NAME,'/SC','ONLOGON','/TR',subprocess.list2cmdline([str(launcher)]),'/F')
        run('schtasks','/Run','/TN',NAME)
    else:
        path=Path.home()/'.config/systemd/user'/f'{NAME}.service'
        if args.action=='remove':
            subprocess.run(['systemctl','--user','disable','--now',NAME],check=False);path.unlink(missing_ok=True);run('systemctl','--user','daemon-reload');return
        path.parent.mkdir(parents=True,exist_ok=True)
        quote=lambda s:'"'+s.replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
        path.write_text('[Unit]\nDescription=ASG SOC endpoint collector\nAfter=network-online.target\n[Service]\nWorkingDirectory='+quote(str(ROOT))+'\nExecStart='+' '.join(quote(x) for x in command)+'\nRestart=always\nRestartSec=30\n[Install]\nWantedBy=default.target\n')
        run('systemctl','--user','daemon-reload');run('systemctl','--user','enable','--now',NAME)
if __name__=='__main__':main()
