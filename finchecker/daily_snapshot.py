"""Provenance and freshness for published, calendar-audited daily bars."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

MANIFEST = 'daily-manifest.json'
MARKETS = ('SH', 'SZ', 'BJ')


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def ready_day(now: datetime, hour=16):
    today = now.date()
    return today if now.hour >= hour else today - timedelta(days=1)


def verified_manifest(root):
    manifest = json.loads((root / MANIFEST).read_text(encoding='utf-8'))
    if (manifest['version'] != 1 or manifest['provider'] != 'tushare' or
            manifest['daily_sha256'] != file_digest(root / 'daily.csv')):
        raise ValueError('日线与验收清单不匹配')
    if 'attributes_sha256' in manifest:
        verify_attributes(root, manifest)
    return manifest


def verify_attributes(root, manifest=None):
    if manifest is None:
        path = root / MANIFEST
        if not path.is_file():
            return
        manifest = json.loads(path.read_text(encoding='utf-8'))
    if 'attributes_sha256' in manifest and manifest['attributes_sha256'] != file_digest(root / 'attributes.csv'):
        raise ValueError('估值与验收清单不匹配')


def snapshot_metadata(root, target, latest, now):
    path = root / MANIFEST
    if not path.is_file():
        return {'freshness': 'unverified_calendar', 'latest_trade_date': latest}
    manifest = verified_manifest(root)
    if manifest['latest_trade_date'] != latest:
        raise ValueError('日线与验收清单不匹配')
    cutoff = ready_day(now, 17 if 'attributes_sha256' in manifest else 16).isoformat()
    reference = manifest['freshness_calendar']
    expected = max((day for day in reference['open_days'] if day <= cutoff), default=None)
    if not reference['from'] <= cutoff <= reference['through']:
        expected = None
    freshness = ('historical_selection' if target < latest else 'unverified_calendar' if expected is None
                 else 'stale' if latest < expected else 'current' if latest == expected else 'ahead_of_cutoff')
    return {'provider': manifest['provider'], 'latest_trade_date': latest,
            'expected_latest_trade_date': expected, 'freshness': freshness,
            'imported_at': manifest['imported_at'], 'market_coverage_date': target,
            'market_coverage': manifest['coverage_by_date'][target],
            'minimum_market_bar_ratio': manifest['minimum_market_bar_ratio'],
            'valuation_coverage': manifest.get('valuation_by_date', {}).get(target),
            'valuation_basis': 'Tushare daily_basic: PE(TTM), total market cap in CNY 100 million'
            if 'attributes_sha256' in manifest else None}
