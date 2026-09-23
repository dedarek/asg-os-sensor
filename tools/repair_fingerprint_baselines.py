"""Verify and repair modify-baseline chains in a live fingerprints.json.

Semantics (learned_install.install): for each file in a revision's plan,
expected_sha256 is the digest of the *previous* bytes of that file, or None
when the plan creates it.  Across revisions of one fingerprint entry, the
chain must therefore satisfy: baseline(rev N, path) == content-digest of the
most recent earlier revision that wrote the same path; None if no earlier
revision wrote it (creation).

Manual content edits after learning break this chain (crazytest B3), which
makes exact reuse fail with a misleading "precondition changed" on healthy
machines.  This tool derives the correct baseline from the chain itself and
only writes when --write is passed.  First-ever modify baselines (file existed
before any learned revision) cannot be derived and are reported unverifiable.

Usage: python3 tools/repair_fingerprint_baselines.py [--path FILE] [--write]
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def check_entry(entry):
    """Return (fixes, unverifiable). fixes: list of (revision, path, old, new)."""
    fixes = []
    unverifiable = []
    last_written = {}  # path -> content digest of most recent writer revision
    revisions = entry.get('revisions') or []
    for rev in revisions:
        plan = (rev.get('recipe') or {}).get('install_plan') or {}
        for item in plan.get('files', []):
            path = item.get('path')
            content = item.get('content')
            if not isinstance(path, str) or not isinstance(content, str):
                continue
            expected = item.get('expected_sha256')
            correct = last_written.get(path)  # None for creation
            if path in last_written:
                if expected != correct:
                    fixes.append((rev.get('revision'), path, expected, correct))
                    item['expected_sha256'] = correct
            elif expected is not None:
                # First writer created-vs-modify state unknown from the chain:
                # a non-None baseline is plausible (target pre-existed); keep it.
                unverifiable.append((rev.get('revision'), path, expected))
            last_written[path] = digest(content)
    # The active top-level hook_recipe should agree with the newest revision.
    if revisions:
        newest = max(revisions, key=lambda r: r.get('revision', 0))
        active = entry.get('hook_recipe') or {}
        if active.get('install_plan') and active.get('install_plan') == newest.get('recipe', {}).get('install_plan'):
            pass  # same object or equal content; both verified above per-revision
    return fixes, unverifiable


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--path', default=str(Path(__file__).resolve().parents[1] / 'runtime' / 'fingerprints.json'))
    parser.add_argument('--write', action='store_true')
    args = parser.parse_args()
    path = Path(args.path)
    data = json.loads(path.read_text())
    all_fixes = {}
    all_unverified = {}
    for entry in data.get('fingerprints', []):
        fixes, unverifiable = check_entry(entry)
        if fixes:
            all_fixes[entry.get('id', '?')] = fixes
        if unverifiable:
            all_unverified[entry.get('id', '?')] = unverifiable
    if args.write and all_fixes:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    print(json.dumps({'mode': 'write' if args.write else 'dry-run',
                      'fixed': all_fixes, 'unverifiable_first_modify': all_unverified},
                     ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
