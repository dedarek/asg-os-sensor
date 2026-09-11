"""Accept a read-only asset checkpoint independently of Hook recipes."""
from __future__ import annotations
from pathlib import Path
import json
import re

from runtime import investigation_findings


def outcome(run_dir: Path, target: dict, returncode: int | None) -> dict | None:
    """A zero exit is necessary, never sufficient. Recheck persisted provenance."""
    if returncode != 0:
        return None
    saved = investigation_findings.load(run_dir / 'investigation_findings.json', target)
    if not saved:
        return None
    findings = saved.get('findings', {})
    identity = findings.get('identity')
    assets = findings.get('assets', {})
    if not isinstance(assets, dict):
        return None
    records = ([identity] if isinstance(identity, dict) else []) + list(assets.values())
    if not records:
        return None
    for record in records:
        investigation_findings.validate_finding(record)
        for ref in record['evidence_refs']:
            if not re.fullmatch(r'ev-[A-Za-z0-9_-]+', ref):
                raise ValueError('Invalid asset evidence reference')
            try:
                evidence = json.loads((run_dir / 'evidence' / (ref + '.json')).read_text())
            except FileNotFoundError as exc:
                raise FileNotFoundError('Asset evidence missing: ' + ref) from exc
            if evidence.get('target') != target or evidence.get('evidence_id') != ref:
                raise ValueError('Asset evidence belongs to another instance')
    useful = [name for name, record in assets.items()
              if record.get('status') == 'collected' and record.get('value') not in (None, {}, [], '')]
    complete = isinstance(identity, dict) and all(name in assets for name in investigation_findings.ASSET_NAMES)
    status = 'assets_collected' if useful and complete else 'partial'
    return {
        'status': status,
        'message': (f'资产初查已保存：{len(useful)} 类有实际数据，'
                    f'{len(assets)} 类已报告；未知项见各项说明。本轮未生成新 Hook 配方、未安装新 Hook；既有挂接状态单独展示。'),
        'asset_checkpoint': {'reported': list(assets), 'collected': useful,
                             'scope': 'this_investigation_pass',
                             'hook_proposed': False, 'installed': False},
        'partial_findings': saved,
    }
