"""Generic stdin/stdout client for generated synchronous Hooks."""
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import urllib.request
import psutil

def main():
    config=json.loads(Path(sys.argv[1]).read_text())
    action=sys.argv[2] if len(sys.argv)>2 else 'decision'
    if action not in ('decision','ack'):raise ValueError('unsupported client action')
    data=json.load(sys.stdin)
    if action=='decision':data['create_time']=psutil.Process(int(data['pid'])).create_time()
    token=Path(config['token_file']).read_text().strip()
    req=urllib.request.Request(config['base_url']+'/'+action,data=json.dumps(data).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    result=json.load(opener.open(req,timeout=65))
    if action=='decision' and result.get('decision') not in ('allow','deny'):raise ValueError('invalid decision')
    print(json.dumps(result))

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'decision':'deny','reason':'control_unavailable','error':type(exc).__name__,'enforcement_verified':False}))
        sys.exit(1)
