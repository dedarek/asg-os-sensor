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

# The desktop app appends rendered image placeholders to the transcript copy of
# a user message; the Hook observes the submitted body without them.  Stripped
# variants only serve as extra verbatim candidates, so hook text must still
# byte-match transcript bytes minus app-injected placeholders.
CODEX_IMAGE_PLACEHOLDER = re.compile(r'\n?<image name=\[Image #\d+\] path="[^"]*"></image>\s*')


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


def user_candidate_variants(text):
    """Yield the transcript text plus app-placeholder-stripped variants."""
    yield text
    stripped = CODEX_IMAGE_PLACEHOLDER.sub('\n', text)
    if stripped != text:
        yield stripped


def injected_user_context(text):
    return text.lstrip().startswith((
        '# AGENTS.md instructions', '<environment_context>',
        '<recommended_plugins>', '<permissions instructions>',
        '<collaboration_mode>', '<apps_instructions>', '<plugins_instructions>',
        '<codex_internal_context',
    ))


def transcript_baseline(paths):
    """Merge every rollout holding hook-observed turns into one baseline.

    A resumed thread rewrites history into several rollout files; partial
    copies must not create false missing/mismatch rows, so the union of
    texts per (turn, role) is the independent baseline.
    """
    baseline = {}
    for path in paths:
        turn = None
        with Path(path).open(errors='ignore') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                p = row.get('payload', {})
                if row.get('type') == 'turn_context':
                    turn = p.get('turn_id')
                if p.get('type') != 'message' or not turn:
                    continue
                role = p.get('role')
                if role not in ('user', 'assistant'):
                    continue
                text = ''.join(c.get('text', '') for c in p.get('content', []) if isinstance(c, dict))
                if role == 'user' and injected_user_context(text):
                    continue
                if role == 'assistant':
                    phase = p.get('phase')
                    if phase not in (None, 'final', 'final_answer'):
                        continue
                    text = visible_assistant_text(text)
                tid = (p.get('internal_chat_message_metadata_passthrough') or {}).get('turn_id') or turn
                entry = baseline.setdefault(tid, {'roles': {}, 'first_ts': None})
                stamp = row.get('timestamp')
                if isinstance(stamp, str) and (entry['first_ts'] is None or stamp < entry['first_ts']):
                    entry['first_ts'] = stamp
                entry['roles'].setdefault(role, set()).add(text)
    return baseline


def _epoch(stamp):
    if isinstance(stamp, (int, float)):
        return float(stamp)
    if isinstance(stamp, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(stamp.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


def reconcile(records, baseline):
    # The evaluation window starts at the first user.input this instance's hook
    # actually captured; turns fully predating it cannot have been observed and
    # are excluded, not counted as failures.  Hook records with no baseline turn
    # are transcript_turn_absent: unverified against an independent source, so
    # they never count as exact and never count as fabricated mismatches.
    window_start = None
    for record in records:
        p = record.get('payload', {})
        if record.get('event_type') in ('user.prompt.submitted', 'user.input'):
            stamp = _epoch(p.get('timestamp'))
            if stamp is not None and (window_start is None or stamp < window_start):
                window_start = stamp
    observed = {}
    pre_activation = 0
    declared_partial = set()
    for record in records:
        p = record.get('payload', {})
        kind = record.get('event_type')
        # asg-io-v3 records carry the body under content; legacy bridges used
        # prompt/assistant_output.  Accept both without reinterpreting roles.
        role, text = (('user', p.get('prompt') or p.get('content')) if kind in ('user.prompt.submitted', 'user.input')
                      else ('assistant', p.get('assistant_output') or p.get('content')) if kind == 'assistant.output' else (None, None))
        if not (role and isinstance(text, str)):
            continue
        stamp = _epoch(p.get('timestamp'))
        if window_start is not None and stamp is not None and stamp < window_start - 1:
            pre_activation += 1
            continue
        if role == 'assistant':
            text = visible_assistant_text(text)
        sha = hashlib.sha256(text.encode()).hexdigest()
        # A record only earns prefix credit when it explicitly declares that
        # this very field was truncated or redacted (truncation.truncated set,
        # or content_complete=false).  The generic redaction envelope that
        # every hook row carries proves nothing about this field.
        trunc = p.get('truncation') or {}
        declared = bool(trunc.get('truncated')) or p.get('content_complete') is False
        if declared:
            declared_partial.add((p.get('turn_id'), role, sha))
        observed.setdefault(p.get('turn_id'), {}).setdefault(role, {})[text] = sha
    results = []
    for tid in set(baseline) - set(observed):
        entry = baseline[tid]
        turn_ts = _epoch(entry.get('first_ts'))
        if turn_ts is None or (window_start is not None and turn_ts < window_start - 1):
            pre_activation += 1
            continue
        for role in ('user', 'assistant'):
            if entry['roles'].get(role):
                results.append({'turn_id': tid, 'role': role, 'chars': 0,
                                'exact': False, 'reason': 'missing_hook_message'})
    for tid, values in observed.items():
        entry = baseline.get(tid)
        if entry is None:
            for role, texts in values.items():
                for text, sha in texts.items():
                    results.append({'turn_id': tid, 'role': role, 'sha256': sha,
                                    'chars': len(text), 'exact': False,
                                    'reason': 'transcript_turn_absent'})
            continue
        base = entry['roles']
        for role in ('user', 'assistant'):
            if base.get(role) and not values.get(role):
                results.append({'turn_id': tid, 'role': role, 'chars': 0,
                                'exact': False, 'reason': 'missing_hook_message'})
        for role, texts in values.items():
            candidates = base.get(role, set())
            if role == 'user':
                candidates = {v for c in candidates for v in user_candidate_variants(c)}
            stripped = {t.rstrip() for t in candidates}
            for text, sha in texts.items():
                row = {'turn_id': tid, 'role': role, 'sha256': sha, 'chars': len(text),
                       'exact': text in candidates or text.rstrip() in stripped}
                if not row['exact'] and (tid, role, sha) in declared_partial and any(t.startswith(text.rstrip()) for t in stripped):
                    # A declared truncation can never be byte-identical by
                    # design.  The visible prefix is verified but is NOT a
                    # verbatim match: exact stays False, the turn can never
                    # count toward completed_turns_exact, and the row is
                    # reported separately as a capture-completeness gap.
                    row['exact'] = False
                    row['reason'] = 'partial_declared'
                results.append(row)
    verified = [r for r in results if r.get('reason') != 'transcript_turn_absent']
    complete = sum(1 for tid, values in observed.items()
                   if values.get('user') and values.get('assistant')
                   and all(r['exact'] for r in results if r['turn_id'] == tid))
    redacted_count = sum(1 for r in results if r.get('reason') == 'partial_declared')
    unverified = sum(1 for r in results if r.get('reason') == 'transcript_turn_absent')
    return {'mode': 'normal_user_instance', 'scope': 'turns_after_first_captured_user_input',
            'window_start_epoch': window_start, 'pre_window_messages': pre_activation,
            'completed_turns_exact': complete, 'compared_messages': len(results),
            'unverified_no_baseline': unverified, 'redacted_messages': redacted_count,
            'partial_declared_messages': redacted_count,
            'mismatches': sum(not r['exact'] for r in verified), 'rows': results,
            'passed': complete >= 10 and bool(verified) and all(r['exact'] for r in verified),
            'limitations': ['评估窗口从本实例首次采集到用户输入起算；窗口前轮次不计入；本报告不证明模型网络和控制',
                            '显式声明截断/脱敏的正文只验证可见前缀，记为 partial_declared：不计入逐字一致回合，也不能通过验收',
                            '目标转录缺失的轮次记为 transcript_turn_absent，不参与精确计数']}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--pid', type=int, required=True);args=ap.parse_args()
    data=json.load(urllib.request.urlopen('http://127.0.0.1:8081/api/hook-data?pid=%s&limit=4000'%args.pid,timeout=30))
    records=data.get('records', [])
    transport='api'
    if not records:
        transport='hook_log'
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
    turn_ids={r.get('payload',{}).get('turn_id') for r in records if r.get('payload',{}).get('turn_id')}
    if turn_ids:
        # One session id spans many rollouts across days; keep only files that
        # actually contain the hook-observed turn ids so a stale rollout cannot
        # fabricate missing_hook_message failures.
        matched=set()
        for candidate in sorted(p for p in paths if p):
            try:
                body=Path(candidate).read_text(errors='ignore')
            except OSError:
                continue
            if any(tid in body for tid in turn_ids):matched.add(candidate)
        paths=matched
    reports=[]
    if records and any(paths):
        baseline=transcript_baseline(sorted(p for p in paths if p))
        report=reconcile(records,baseline)
        report['transcripts']=sorted(p for p in paths if p)
        reports=[report]
    completed_total=sum(r['completed_turns_exact'] for r in reports)
    mismatch_total=sum(r['mismatches'] for r in reports)
    output={'pid':args.pid,'reports':reports,
            'completed_turns_exact_total':completed_total,
            'mismatches_total':mismatch_total,
            'passed':completed_total >= 10 and mismatch_total == 0}
    (ROOT/f'artifacts/acceptance/normal-conversation-{args.pid}.json').write_text(
        json.dumps(output,ensure_ascii=False,indent=2))
    # The sign-off table reads this single row; bind it to the exact instance,
    # transport and evidence file so a pass can never float free of its target.
    from datetime import datetime, timezone
    instance_id=None
    try:
        st=json.load(urllib.request.urlopen('http://127.0.0.1:8081/api/state',timeout=15))
        for row in (st.get('agents') or []):
            if args.pid in (row.get('instances') or []):
                instance_id=row.get('instance_id');break
    except OSError:
        pass
    summary={'pid':args.pid,'transport':transport,
             'instance_id':instance_id,
             'completed_turns_exact':completed_total,
             'compared_messages':sum(r['compared_messages'] for r in reports),
             'mismatches':mismatch_total,'passed':output['passed'],
             'hook_sha256':hashlib.sha256((Path.home()/'.codex/asg-observer/hook.cjs').read_bytes()).hexdigest() if (Path.home()/'.codex/asg-observer/hook.cjs').exists() else None,
             'source':'verify_normal_conversation.py --pid %d (hook records vs target rollout transcripts)'%args.pid,
             'checked_at':datetime.now(timezone.utc).isoformat(),
             'evidence':'artifacts/acceptance/normal-conversation-%d.json'%args.pid,
             'transcripts':sorted(p for p in paths if p)}
    (ROOT/'artifacts/acceptance/current-normal-conversation.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
    print(json.dumps({**output,'reports':[{k:v for k,v in r.items() if k not in ('rows','transcript_path')} for r in reports]},ensure_ascii=False))

if __name__=='__main__':main()
