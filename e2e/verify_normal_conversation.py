"""Read-only comparison of normal Codex session messages and captured Hook text.

Uses the target's transcript as independent baseline. Never injects messages,
replays callbacks, or treats text found in tool output as a user prompt.
"""
import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


HIDDEN_ASSISTANT_BLOCKS = (
    re.compile(r'\n?<oai-mem-citation>.*?</oai-mem-citation>\s*', re.DOTALL),
)


def visible_assistant_text(text):
    """Remove app metadata that is stored in the transcript but hidden in UI.

    The Codex Hook observes the answer presented to the user.  The rollout file
    can additionally contain renderer metadata such as memory citations; those
    bytes are not part of the visible model output and must not create a false
    mismatch.
    """
    for pattern in HIDDEN_ASSISTANT_BLOCKS:
        text = pattern.sub('', text)
    return text.rstrip()


def injected_user_context(text):
    return text.lstrip().startswith((
        '# AGENTS.md instructions', '<environment_context>',
        '<recommended_plugins>', '<permissions instructions>',
        '<collaboration_mode>', '<apps_instructions>', '<plugins_instructions>',
    ))


def reconcile(records, transcript):
    by_turn = {}
    turn = None
    with transcript.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            p = row.get('payload', {})
            if row.get('type') == 'turn_context':
                turn = p.get('turn_id')
            if row.get('type') != 'response_item' or p.get('type') != 'message' or not turn:
                continue
            role = p.get('role')
            if role not in ('user', 'assistant'):
                continue
            text = ''.join(c.get('text', '') for c in p.get('content', []) if isinstance(c, dict))
            # Session setup records are distinct from the submitted chat prompt.
            if role == 'user' and injected_user_context(text):
                continue
            if role == 'assistant':
                phase = p.get('phase')
                # Current transcripts mark the final answer explicitly.  Older
                # ones used phase=None, so retain those as candidates and match
                # them against the observed Hook text below.
                if phase not in (None, 'final', 'final_answer'):
                    continue
                text = visible_assistant_text(text)
            by_turn.setdefault((p.get('internal_chat_message_metadata_passthrough') or {}).get('turn_id') or turn, {}).setdefault(role, []).append(text)
    observed = {}
    for record in records:
        p = record.get('payload', {})
        kind = record.get('event_type')
        role, text = (('user', p.get('prompt')) if kind in ('user.prompt.submitted', 'user.input')
                      else ('assistant', p.get('assistant_output')) if kind == 'assistant.output' else (None, None))
        if role and isinstance(text, str):
            if role == 'assistant':
                text = visible_assistant_text(text)
            observed.setdefault(p.get('turn_id'), {}).setdefault(role, []).append(text)
    results = []
    for tid, values in observed.items():
        baseline = by_turn.get(tid, {})
        # Compare each observed user-visible message against the independent
        # transcript.  This avoids treating setup messages or internal renderer
        # metadata as text the Hook should have captured.
        for role in ('user', 'assistant'):
            if baseline.get(role) and not values.get(role):
                results.append({'turn_id': tid, 'role': role, 'chars': 0,
                                'exact': False, 'reason': 'missing_hook_message'})
        for role, texts in values.items():
            candidates = baseline.get(role, [])
            for text in texts:
                results.append({'turn_id': tid, 'role': role,
                                'sha256': hashlib.sha256(text.encode()).hexdigest(),
                                'chars': len(text), 'exact': text in candidates})
    complete = sum(1 for tid, values in observed.items()
                   if values.get('user') and values.get('assistant')
                   and all(r['exact'] for r in results if r['turn_id'] == tid)
                   and {'user','assistant'} <= {r['role'] for r in results if r['turn_id'] == tid})
    return {'mode':'normal_user_instance','scope':'turns_with_hook_records_in_available_window',
            'completed_turns_exact':complete,'compared_messages':len(results),
            'mismatches':sum(not r['exact'] for r in results),'rows':results,
            'passed':complete >= 10 and bool(results) and all(r['exact'] for r in results),
            'limitations':['未上报的整轮仍需按安装生效时间建立完整基准范围；本报告不证明模型网络和控制']}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--pid', type=int, required=True);args=ap.parse_args()
    data=json.load(urllib.request.urlopen('http://127.0.0.1:8081/api/hook-data?pid=%s&limit=4000'%args.pid,timeout=30))
    records=data.get('records', [])
    if not records:
        # Short-lived normal instances (codex exec turns) exit before the
        # registry retains a binding, so the canonical API has no rows.
        # Fall back to the Hook log itself; content matching still uses the
        # target's own rollout transcript as the independent baseline.
        records=[]
        log=Path.home()/'.codex/asg-observer/events.jsonl'
        want={'user.input':'user.input','assistant.output':'assistant.output'}
        for line in log.read_text().splitlines():
            try: row=json.loads(line)
            except ValueError: continue
            if row.get('pid')!=args.pid or row.get('event') not in want: continue
            body=row.get('content')
            payload={'prompt':body} if row['event']=='user.input' else {'assistant_output':body}
            payload.update({'turn_id':row.get('turn_id'),'session_id':row.get('session_id'),'transcript_path':row.get('transcript_path')})
            records.append({'event_type':want[row['event']],'payload':payload})
    # Codex transcripts are named with the thread/session id; when the hook
    # payload lacks an explicit transcript_path, locate the rollout file by
    # session id so live desktop and exec instances reconcile identically.
    sessions={r.get('payload',{}).get('session_id') for r in records if r.get('payload',{}).get('session_id')}
    sessions|={r.get('payload',{}).get('turn_id') for r in records if r.get('payload',{}).get('turn_id')}
    paths={r.get('payload',{}).get('transcript_path') for r in records}
    for sid in sorted(s for s in sessions if s):
        if any(p and sid in p for p in paths):
            continue
        import subprocess
        found=subprocess.run(['find',str(Path.home()/'.codex/sessions'),'-name','*'+sid+'*.jsonl'],capture_output=True,text=True).stdout.split()
        for f in found:
            paths.add(f)
    reports=[]
    for path in sorted(p for p in paths if p):
        relevant=[r for r in records if r.get('payload',{}).get('transcript_path')==path or any(s and s in path for s in (r.get('payload',{}).get('session_id'), r.get('payload',{}).get('turn_id')))]
        report=reconcile(relevant,Path(path));report['transcript_path']=path;reports.append(report)
    completed_total=sum(r['completed_turns_exact'] for r in reports)
    mismatch_total=sum(r['mismatches'] for r in reports)
    output={'pid':args.pid,'reports':reports,
            'completed_turns_exact_total':completed_total,
            'mismatches_total':mismatch_total,
            'passed':completed_total >= 10 and mismatch_total == 0}
    (ROOT/f'artifacts/acceptance/normal-conversation-{args.pid}.json').write_text(
        json.dumps(output,ensure_ascii=False,indent=2))
    print(json.dumps({**output,'reports':[{k:v for k,v in r.items() if k not in ('rows','transcript_path')} for r in reports]},ensure_ascii=False))

if __name__=='__main__':main()
