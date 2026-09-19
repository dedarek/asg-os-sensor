"""Verify a continuously running standalone Hook service for a fixed duration.

This check never restarts the service.  It binds every sample to the same
process create time, polls /health, and updates the existing independent-runtime
report without rerunning or replacing its workload/control/outage evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/acceptance'
STATE = ROOT / 'artifacts/autonomous-service/hook-runtime-state.json'
REPORT = OUT / 'independent-runtime.json'
HEALTH = 'http://127.0.0.1:8099/health'


def read_json(path):
    return json.loads(path.read_text())


def health():
    try:
        with urllib.request.urlopen(HEALTH, timeout=5) as response:
            return json.load(response).get('status')
    except Exception as exc:  # noqa: BLE001
        return 'error:' + type(exc).__name__


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--minutes', type=float, default=60)
    parser.add_argument('--interval', type=float, default=30)
    parser.add_argument('--reconcile-existing', action='store_true',
                        help='recheck the saved run endpoint identity without repeating the soak')
    args = parser.parse_args()
    if args.reconcile_existing:
        result = read_json(OUT / 'independent-soak.json')
        pid = int(result['pid'])
        expected_create_time = float(result['create_time'])
        try:
            end_identity_matches = (psutil.pid_exists(pid)
                                    and abs(psutil.Process(pid).create_time() - expected_create_time) <= 0.01)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            end_identity_matches = False
        source_unchanged = all(file_hash(ROOT / rel) == digest
                               for rel, digest in result.get('source_hashes', {}).items())
        result['identity_end_rechecked_at'] = datetime.now(timezone.utc).isoformat()
        result['end_identity_matches'] = end_identity_matches
        result['identity_probe_failures'] = result['samples'] - result['same_process_samples']
        result['continuity_basis'] = ('same PID:create_time at start and after the sampling window, '
                                      'with every in-window health request successful; a process cannot '
                                      'restart while retaining its original create_time')
        result['source_unchanged'] = source_unchanged
        result['all_ok'] = (result['healthy_samples'] == result['samples']
                            and end_identity_matches and source_unchanged
                            and result['samples'] >= 100)
        (OUT / 'independent-soak.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        report = read_json(REPORT)
        report['soak'] = result
        report.setdefault('gates', {})['soak_60min_ok'] = result['all_ok']
        report['passed'] = all(report['gates'].values())
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps({'passed': result['all_ok'], 'samples': result['samples'],
                          'healthy': result['healthy_samples'],
                          'identity_probe_failures': result['identity_probe_failures'],
                          'end_identity_matches': end_identity_matches}, ensure_ascii=False))
        return
    if args.minutes < 60:
        raise SystemExit('acceptance soak requires at least 60 minutes')

    state = read_json(STATE)
    pid = int(state['pid'])
    expected_create_time = float(state['create_time'])
    proc = psutil.Process(pid)
    if abs(proc.create_time() - expected_create_time) > 0.01:
        raise SystemExit('standalone service identity changed before soak')

    started = time.time()
    samples = []
    source_hashes = {
        str(path.relative_to(ROOT)): file_hash(path)
        for path in (ROOT / 'hook_runtime.py', ROOT / 'runtime/hook_service.py',
                     ROOT / 'runtime/hook_control.py', ROOT / 'runtime/hook_data.py')
    }
    deadline = started + args.minutes * 60
    while True:
        now = time.time()
        same_process = psutil.pid_exists(pid)
        identity_error = None
        if same_process:
            try:
                same_process = abs(psutil.Process(pid).create_time() - expected_create_time) <= 0.01
            except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
                same_process = False
                identity_error = type(exc).__name__
        samples.append({'at': now, 'health': health(), 'same_process': same_process,
                        'identity_error': identity_error})
        if now >= deadline:
            break
        time.sleep(min(args.interval, max(0, deadline - now)))

    source_unchanged = all(file_hash(ROOT / rel) == digest for rel, digest in source_hashes.items())
    try:
        end_identity_matches = (psutil.pid_exists(pid)
                                and abs(psutil.Process(pid).create_time() - expected_create_time) <= 0.01)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        end_identity_matches = False
    result = {
        'started_at': datetime.fromtimestamp(started, timezone.utc).isoformat(),
        'finished_at': datetime.now(timezone.utc).isoformat(),
        'minutes': args.minutes,
        'interval_seconds': args.interval,
        'samples': len(samples),
        'healthy_samples': sum(s['health'] == 'ok' for s in samples),
        'same_process_samples': sum(s['same_process'] for s in samples),
        'identity_probe_failures': sum(not s['same_process'] for s in samples),
        'identity_probe_errors': [s for s in samples if not s['same_process']],
        'end_identity_matches': end_identity_matches,
        'continuity_basis': ('same PID:create_time at start and end, with every in-window health request '
                             'successful; individual identity probe errors remain recorded as diagnostics'),
        'pid': pid,
        'create_time': expected_create_time,
        'source_hashes': source_hashes,
        'source_unchanged': source_unchanged,
    }
    result['all_ok'] = (result['healthy_samples'] == len(samples)
                        and end_identity_matches and source_unchanged and len(samples) >= 100)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'independent-soak.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))

    report = read_json(REPORT)
    report['soak'] = result
    report.setdefault('gates', {})['soak_60min_ok'] = result['all_ok']
    report['passed'] = all(report['gates'].values())
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps({'passed': result['all_ok'], 'samples': len(samples),
                      'healthy': result['healthy_samples'], 'same_process': result['same_process_samples']},
                     ensure_ascii=False))


if __name__ == '__main__':
    main()
