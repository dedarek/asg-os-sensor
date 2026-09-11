"""Read-only acceptance: source bytes, target identity and real paired events."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT/'artifacts/autonomous-demo'
SERVICE = ROOT/'artifacts/autonomous-service'

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['first','reuse','restored']);args=parser.parse_args()
    meta=json.loads((DEMO/'target.json').read_text())
    state=json.load(urllib.request.urlopen('http://127.0.0.1:8081/api/state',timeout=10))
    iid='%s:%s'%(meta['pid'],meta['create_time'])
    agent=next(a for a in state['agents'] if a['instance_id']==iid)
    observation=state['observation_instances'][iid]
    assert observation['target_alive'] is True
    assert observation['health']['loaded_observed'] is True
    assert observation['paired_calls'], 'No completed real tool callback pair'
    assert any(x['tool_name']=='read' for x in observation['paired_calls']), 'No real read tool pair'
    # Check the actual bounded tool result, not only an event label or model prose.
    log=Path(observation['binding']['log_path'])
    raw_events=[json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    expected_text=(DEMO/'workspace/demo.txt').read_text().strip()
    assert any(e.get('pid')==meta['pid'] and e.get('event')=='tool.execute.after'
        and e.get('tool')=='read' and expected_text in str(e.get('detail', {}).get('output',''))
        for e in raw_events), 'Paired call did not return the test file contents'
    adapter=agent['adapter']
    plan=adapter['onboarding']['plan']
    if args.phase!='first':
        assert adapter['match_status']=='exact', 'Reopened instance did not reuse exact compatibility'
    runs=[]
    for p in SERVICE.glob('pid_%s_*/investigation_lifecycle.json'%meta['pid']):
        life=json.loads(p.read_text())
        if life['target']=={'pid':meta['pid'],'create_time':meta['create_time']}:
            runs.append({'phase':life.get('phase'),'status':life['status'],'run_dir':str(p.parent)})
    if args.phase!='first':
        assert not any(r['phase']=='hook' for r in runs), 'Reopened instance unexpectedly relearned the Hook'
    # Compare installed bytes with the actual model-produced candidate, never a template.
    fp=json.loads((SERVICE/'fingerprints.json').read_text())
    entry=next(x for x in fp['fingerprints'] if x['id']==plan['fingerprint_id'])
    recipe=entry['hook_recipe']
    files=[]
    for f in recipe['install_plan']['files']:
        installed=Path(recipe['hook']['workspace'])/f['path']
        expected=f['content'].encode()
        assert installed.read_bytes()==expected, 'Installed Hook differs from Goose candidate'
        files.append({'path':str(installed),'sha256':hashlib.sha256(expected).hexdigest()})
    result={'phase':args.phase,'target':{'pid':meta['pid'],'create_time':meta['create_time']},
        'instance_id':iid,'fingerprint_id':entry['id'],'match_status':adapter['match_status'],
        'loaded':True,'actual_file_contents_verified':True,'valid_events':observation['events']['valid'],
        'discarded_events':observation['events']['invalid'],'paired_tools':[x['tool_name'] for x in observation['paired_calls']],
        'assets':{k:v['status'] for k,v in adapter['assets'].items()},'investigations':runs,
        'installed_files':files,'active_investigations':list(state['active_investigations'])}
    out=DEMO/(args.phase+'-verification.json');out.write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
