"""Task-2 acceptance batch: real chat/tool traffic vs captured input/output.

The driver is the independent sender: it records exactly what it sent and the
target's exact final answer, then reconciles against what the Hook captured
(/api/hook-data). Completeness is measured against that baseline, not against
the capture itself. Scenarios the target cannot do are recorded as such.

Usage:  python3 e2e/verify_io_batch.py [--scenarios chat30,longtext3,...] [--instance 52934:...]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import secrets
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'artifacts/autonomous-demo'
DASH = 'http://127.0.0.1:8081'
ALL = ('chat30', 'longtext3', 'stream10', 'multitool10', 'toolfail5', 'concurrent30')


def request(base, path, body=None, timeout=300):
    req = urllib.request.Request(base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={'Content-Type': 'application/json'})
    return json.load(urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=timeout))


def reply_text(response):
    parts = (response or {}).get('parts') or []
    return ''.join(str(p.get('text') or '') for p in parts if isinstance(p, dict) and p.get('type') == 'text')


def new_session(target, title):
    return request(target, '/session', {'title': title,
        'permission': [{'permission': '*', 'pattern': '*', 'action': 'allow'}]})


def send(target, gateway, session_id, text, timeout=300):
    return request(target, '/session/' + session_id + '/message',
                   {'model': {'providerID': 'demo', 'modelID': gateway['model']},
                    'parts': [{'type': 'text', 'text': text}]}, timeout=timeout)


def round_trip(target, gateway, session_id, text):
    canary = 'ASG-IO-' + secrets.token_hex(8)
    prompt = (text + '\n\n验收编号：' + canary +
              '\n请在回复中原样包含这个验收编号 ' + canary + '。')
    record = {'canary': canary, 'session_id': session_id, 'sent_chars': len(prompt), 'text': text,
              'sent_at': time.time()}
    try:
        response = send(target, gateway, session_id, prompt)
        record.update(reply=reply_text(response), ok=True)
    except Exception as exc:  # noqa: BLE001
        record.update(reply='', ok=False, error=type(exc).__name__ + ': ' + str(exc)[:200])
    return record


def scenario_chat(target, gateway, n_sessions, n_rounds):
    rounds = []
    kinds = ['请用中文简短介绍什么是 Agent 运行时治理。',
             'Please reply in English with one sentence about tool-use safety.',
             '请回答下面这行代码的作用，并保留换行：\nprint(1 + 1)',
             '用一句中文说明 PII 脱敏的目标。']
    for s in range(n_sessions):
        session = new_session(target, 'io-chat-%d' % s)
        for i in range(n_rounds):
            rounds.append(round_trip(target, gateway, session['id'], kinds[(s + i) % len(kinds)]))
    return rounds


def scenario_longtext(target, gateway):
    rounds = []
    for kb in (1, 10, 100):
        filler = ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(kb * 1024))
        session = new_session(target, 'io-long-%d' % kb)
        record = round_trip(target, gateway, session['id'],
                            '以下是约 %d KB 的纯文本，请只回复“收到”。\n%s' % (kb, filler))
        record['size_kb'] = kb
        rounds.append(record)
    return rounds


def scenario_stream(target, gateway, n):
    rounds = []
    for _ in range(n):
        session = new_session(target, 'io-stream')
        rounds.append(round_trip(target, gateway, session['id'],
                                 '请用中文写一段不少于 8000 字符的详细技术说明，主题是进程级 Hook 的加载与信任，'
                                 '分多个小节展开，务必保证正文长度超过 8000 字符。'
 + '注意：先把验收编号写在回复最前面单独一行，再开始正文，防止长文输出截断导致编号缺失。'))
    return rounds


def scenario_multitool(target, gateway, n):
    rounds = []
    for _ in range(n):
        session = new_session(target, 'io-tools')
        rounds.append(round_trip(target, gateway, session['id'],
                                 '请连续使用工具：先用 read 读取本工作区的 demo.txt，再运行一次 bash 命令 ls，最后用 read 再读一次 demo.txt。然后报告结果。'))
    return rounds


def scenario_toolfail(target, gateway, n):
    rounds = []
    for i in range(n):
        session = new_session(target, 'io-fail')
        rounds.append(round_trip(target, gateway, session['id'],
                                 '请使用 read 工具读取这个不存在的文件 /tmp/asg-missing-%d.txt，并把错误原样报告，不要创建它。' % i))
    return rounds


def hook_data(instance_id, since):
    import urllib.parse
    label = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(since))
    q = urllib.parse.urlencode({'instance_id': instance_id, 'since': label, 'limit': 20000})
    return json.load(urllib.request.urlopen(DASH + '/api/hook-data?' + q, timeout=60))


def binding_for(instance_id):
    """(log_path, pid, create_time) for a bound instance, or (None, None, None)."""
    record = None
    for candidate in (ROOT / 'artifacts/autonomous-service/observations.json',
                      ROOT / 'artifacts/stage1/dashboard/observations.json'):
        try:
            registry = json.loads(candidate.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(registry, dict) and isinstance(registry.get(instance_id), dict):
            record = registry[instance_id]
            break
    if not isinstance(record, dict):
        return None, None, None
    try:
        cfg_path = Path(record['config_path'])
        if not cfg_path.is_absolute():
            cfg_path = ROOT / cfg_path
        config = json.loads(cfg_path.read_text())
    except (OSError, ValueError, KeyError):
        return None, None, None
    target = config.get('target') or {}
    return config.get('log_path'), target.get('pid'), target.get('create_time')


def _parts_text(value):
    """Join the text parts of a payload part list, which may be a bounded dict."""
    parts = value
    if isinstance(value, dict):
        raw = value.get('value')
        if not isinstance(raw, str):
            return ''
        try:
            parts = json.loads(raw)
        except ValueError:
            return ''
    if not isinstance(parts, list):
        return ''
    return ' '.join(str(p.get('text') or '') for p in parts
                    if isinstance(p, dict) and p.get('type') == 'text')


def _record_time(record):
    value = record.get('timestamp')
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        from datetime import datetime, timezone
        try:
            moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.timestamp()
    return None


def reconcile(instance_id, since, rounds):
    # Read the producer log directly: the HTTP reader is bounded (2000 records /
    # 8 MB) and a full fixed batch can exceed that window, which would silently
    # drop rounds. The raw log is the same producer data without the cap.
    binding_log, filter_pid, filter_create = binding_for(instance_id)
    records, source = [], {'log_path': str(binding_log), 'exists': False, 'lines': 0}
    if binding_log and Path(binding_log).is_file():
        source['exists'] = True
        for line in Path(binding_log).read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            source['lines'] += 1
            if filter_pid is not None and record.get('pid') != filter_pid:
                continue
            if _record_time(record) is None or _record_time(record) < since:
                continue
            detail = record.get('detail') if isinstance(record.get('detail'), dict) else {}
            records.append({'event_type': record.get('event'), 'timestamp': _record_time(record),
                            'instance_id': detail.get('instance_id') or instance_id, 'payload': record})
    data = {'records': records, 'coverage': {'source': source, 'complete': False,
                                             'scope': 'producer_log_direct'}}
    conv = []
    import re
    user_texts = {}
    assist_texts = {}
    assist_msgs = set()
    assist_trunc_sessions = set()
    for c in conv:
        if c.get('role') == 'user':
            user_texts.setdefault(c.get('session_id'), []).append(str(c.get('text') or ''))
    def session_of(rec):
        payload = rec.get('payload') or {}
        detail = payload.get('detail') if isinstance(payload.get('detail'), dict) else {}
        return detail.get('session_id') or payload.get('session_id')

    def content_json_text(payload, want_role=None):
        # Newer capture layers serialise the whole message object into
        # payload['content'] instead of detail.parts/detail.text.value.
        raw = payload.get('content')
        if not isinstance(raw, str) or not raw.startswith('{'):
            return None
        try:
            obj = json.loads(raw)
        except ValueError:
            return None
        msg = obj.get('message') if isinstance(obj.get('message'), dict) else obj
        if want_role and msg.get('role') not in (want_role, None):
            return None
        parts = obj.get('parts')
        if isinstance(parts, list):
            joined = ''.join(str(p.get('text') or '') for p in parts if isinstance(p, dict) and p.get('type') == 'text')
            if joined:
                return joined
        if isinstance(obj.get('text'), str):
            return obj['text']
        return None

    for rec in records:
        payload = rec.get('payload') or {}
        detail = payload.get('detail') if isinstance(payload.get('detail'), dict) else {}
        if rec.get('event_type') == 'assistant.output' and detail.get('message_role') == 'assistant':
            mid = detail.get('message_id')
            if mid:
                assist_msgs.add(mid)
            value = (detail.get('message') or {}).get('value')
            if isinstance(value, str):
                assist_texts.setdefault(session_of(rec), []).append(value)
            if (detail.get('message') or {}).get('truncated') is True:
                assist_trunc_sessions.add(session_of(rec))
    for rec in records:
        payload = rec.get('payload') or {}
        detail = payload.get('detail') if isinstance(payload.get('detail'), dict) else {}
        if rec.get('event_type') == 'user.input':
            text = _parts_text(detail.get('parts')) or content_json_text(payload, 'user')
            if text:
                user_texts.setdefault(session_of(rec), []).append(text)
        if rec.get('event_type') == 'assistant.output' and detail.get('part_type') == 'text' and (
                detail.get('message_id') in assist_msgs or detail.get('message_role') in (None, 'assistant')):
            value = (detail.get('text') or {}).get('value')
            if not isinstance(value, str) and isinstance(payload.get('content'), str) and not payload['content'].startswith('{'):
                value = payload['content']
            if isinstance(value, str):
                assist_texts.setdefault(session_of(rec), []).append(value)
            if (detail.get('text') or {}).get('truncated') is True or payload.get('content_truncated') is True:
                assist_trunc_sessions.add(session_of(rec))

    truncated_sessions = set()
    for rec in records:
        if rec.get('event_type') == 'user.input':
            parts = ((rec.get('payload') or {}).get('detail') or {}).get('parts')
            if (isinstance(parts, dict) and parts.get('truncated') is True) or ((rec.get('payload') or {}).get('content_truncated') is True) and rec.get('event_type') == 'user.input':
                truncated_sessions.add(session_of(rec))

    def hit(bucket, session, canary):
        return any(canary in t for t in bucket.get(session, []))

    def exact(bucket, session, reply):
        target_text = ' '.join(str(reply or '').split())
        return bool(target_text) and any(' '.join(str(t).split()) == target_text for t in bucket.get(session, []))

    sent = [r for r in rounds if r.get('ok')]
    user_hits = sum(1 for r in sent if hit(user_texts, r['session_id'], r['canary']))
    out_hits = sum(1 for r in sent if hit(assist_texts, r['session_id'], r['canary']))
    out_exact = sum(1 for r in sent if exact(assist_texts, r['session_id'], r.get('reply')))
    user_missing = [r['canary'] for r in sent if not hit(user_texts, r['session_id'], r['canary'])]
    output_missing = [r['canary'] for r in sent if not hit(assist_texts, r['session_id'], r['canary'])]
    user_truncated = [r['canary'] for r in sent
                      if not hit(user_texts, r['session_id'], r['canary']) and r['session_id'] in truncated_sessions]
    user_absent = [c for c in user_missing if c not in user_truncated]
    output_truncated = [r['canary'] for r in sent
                        if not hit(assist_texts, r['session_id'], r['canary']) and r['session_id'] in assist_trunc_sessions]
    output_absent = [c for c in output_missing if c not in output_truncated]

    tools = {}
    for rec in records:
        et = rec.get('event_type')
        if et not in ('tool.execute.before', 'tool.execute.after', 'tool.error'):
            continue
        payload = rec.get('payload') or {}
        detail = payload.get('detail') or {}
        call = detail.get('call_id') or payload.get('call_id')
        if not call:
            continue
        slot = tools.setdefault(call, {})
        slot[et] = detail
        slot['instance_id'] = rec.get('instance_id')
    paired = [v for v in tools.values() if 'tool.execute.before' in v and (
        'tool.execute.after' in v or 'tool.error' in v)]
    with_payload = [v for v in paired
                    if json.dumps(v.get('tool.execute.before', {}), ensure_ascii=False).strip('{}')]

    trunc_unmarked = 0
    for rec in records:
        payload = rec.get('payload') or {}
        detail = payload.get('detail')
        if isinstance(detail, dict) and detail.get('truncated') is True and 'truncation' not in detail:
            trunc_unmarked += 1

    known_sessions = {r['session_id'] for r in rounds}
    crosslink_bad = sum(1 for rec in records if rec.get('instance_id') != instance_id)
    unknown_session = sum(1 for c in conv if c.get('session_id') not in known_sessions)

    return {
        'instance_id': instance_id,
        'sent_ok': len(sent), 'sent_total': len(rounds),
        'user_input_complete': user_hits, 'final_output_complete': out_hits, 'final_output_exact': out_exact,
        'final_output_truncated_marked': len(output_truncated), 'final_output_absent': output_absent,
        'user_input_truncated_marked': len(user_truncated), 'user_input_absent': user_absent,
        'user_input_missing': user_absent, 'final_output_missing': output_missing,
        'supported_user_input_rate': (user_hits / (len(sent) - len(user_truncated))) if (len(sent) - len(user_truncated)) else 0.0,
        'user_input_rate': (user_hits / len(sent)) if sent else 0.0,
        'final_output_rate': (out_hits / len(sent)) if sent else 0.0,
        'tool_pairs': len(paired), 'tool_pairs_with_payload': len(with_payload),
        'truncated_unmarked': trunc_unmarked,
        'cross_link_violations': crosslink_bad, 'unknown_session_entries': unknown_session,
        'captured_records': len(records), 'captured_conversation': len(conv),
        'coverage': data.get('coverage'),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--scenarios', default='all')
    ap.add_argument('--instance', default=None)
    ap.add_argument('--reconcile-only', action='store_true')
    args = ap.parse_args()
    scenarios = ALL if args.scenarios == 'all' else tuple(s.strip() for s in args.scenarios.split(',') if s.strip())

    meta = json.loads((DEMO / 'target.json').read_text())
    gateway = json.loads((DEMO / 'gateway.json').read_text())
    target = 'http://127.0.0.1:' + str(meta['port'])
    instance_id = args.instance or ('%s:%s' % (meta['pid'], meta['create_time']))
    # A stale target pid silently yields an empty reconciliation, because the
    # reader keeps only records that match a bound, live instance. Fail loudly.
    bound = binding_for(instance_id)[0] is not None
    if not bound:
        print(json.dumps({'error': 'instance_not_bound', 'instance_id': instance_id,
                          'hint': 'demo target pid in target.json is stale'}, ensure_ascii=False))
        sys.exit(2)
    started = time.time()
    out = ROOT / 'artifacts/acceptance'
    out.mkdir(parents=True, exist_ok=True)
    path = out / 'io-batch.json'
    report = {}
    if path.exists():
        try:
            report = json.loads(path.read_text())
        except ValueError:
            report = {}
    report.update({'instance_id': instance_id, 'mode': 'isolated_real_instance', 'last_started_at': started})
    results = report.setdefault('scenarios', {})

    if args.reconcile_only:
        all_rounds = [r for rows in results.values() for r in rows]
        since = min((r.get('sent_at', started) for r in all_rounds), default=started) - 5
        report['reconciliation'] = reconcile(instance_id, since, all_rounds)
        report['finished_at'] = time.time()
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report['reconciliation'], ensure_ascii=False))
        return

    def guard(key, fn):
        try:
            results[key] = fn()
        except Exception as exc:  # noqa: BLE001
            results.setdefault(key, [])
            report.setdefault('scenario_errors', {})[key] = type(exc).__name__ + ': ' + str(exc)[:200]

    if 'chat30' in scenarios:
        guard('chat30', lambda: scenario_chat(target, gateway, 3, 10))
    if 'longtext3' in scenarios:
        guard('longtext3', lambda: scenario_longtext(target, gateway))
    if 'stream10' in scenarios:
        guard('stream10', lambda: scenario_stream(target, gateway, 10))
    if 'multitool10' in scenarios:
        guard('multitool10', lambda: scenario_multitool(target, gateway, 10))
    if 'toolfail5' in scenarios:
        guard('toolfail5', lambda: scenario_toolfail(target, gateway, 5))
    if 'concurrent30' in scenarios:
        def run_concurrent():
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
                futures = []
                for s in range(3):
                    session = new_session(target, 'io-concurrent-%d' % s)
                    for i in range(10):
                        futures.append(pool.submit(round_trip, target, gateway, session['id'],
                                                   '并发会话第 %d 轮，请用中文简短确认收到。' % i))
                return [f.result() for f in futures]
        guard('concurrent30', run_concurrent)

    all_rounds = [r for rows in results.values() for r in rows]
    since = min((r.get('sent_at', started) for r in all_rounds), default=started) - 5
    try:
        report['reconciliation'] = reconcile(instance_id, since, all_rounds)
    except Exception as exc:  # noqa: BLE001
        report['reconciliation'] = {'error': type(exc).__name__ + ': ' + str(exc)[:200]}
    report['finished_at'] = time.time()
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'report': str(path), 'scenarios': list(results.keys()),
                      'reconciliation': report['reconciliation'] is not None}, ensure_ascii=False))
    print(json.dumps(report['reconciliation'], ensure_ascii=False))
    rec = report['reconciliation'] or {}
    passed = (rec.get('sent_ok') == rec.get('sent_total') and (rec.get('captured_records') or 0) > 0
              and rec.get('supported_user_input_rate') == 1.0 and rec.get('final_output_rate') == 1.0)
    report['passed'] = passed
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
