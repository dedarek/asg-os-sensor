"""Short-lived filesystem reader. Parent enforces its wall-clock deadline."""
import json
import sys
import time
from .protocol import roots, skill_scope, mcp_scope, scope_present

def main():
    request=json.load(sys.stdin);op=request['operation'];args=request['args']
    if op=='roots':
        skill,mcp,settings,errors=roots(**args)
        result={'skill':[str(p) for p in skill],'mcp':[(str(p),field) for p,field in mcp],'settings':settings,'errors':errors}
    elif op=='skill':
        from pathlib import Path
        previous=args.pop('previous_scopes',[])
        result=skill_scope(**args,deadline=time.monotonic()+15) if scope_present(Path(args['root']),'skill:'+args['root'],previous) else None
    elif op=='mcp':
        from pathlib import Path
        previous=args.pop('previous_scopes',[])
        result=mcp_scope(Path(args['path']),args['field']) if scope_present(Path(args['path']),'mcp:'+args['path'],previous) else None
    else:raise ValueError('unsupported collection operation')
    json.dump(result,sys.stdout,ensure_ascii=False,default=str)

if __name__=='__main__':main()
