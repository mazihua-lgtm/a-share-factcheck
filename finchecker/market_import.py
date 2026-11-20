"""Create a local research snapshot from an explicitly authorized historical feed."""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import pandas as pd
import requests

from finchecker.daily_snapshot import MANIFEST, MARKETS, file_digest, ready_day, verified_manifest
from finchecker.market import DATASETS, SHANGHAI, MarketUnavailable, load, normalize_symbol, numeric, public_daily

TUSHARE_URL = 'https://api.tushare.pro/'
TUSHARE_FIELDS = ('ts_code', 'trade_date', 'open', 'high', 'low', 'close', 'vol', 'pct_chg')
ROSTER_FIELDS = ('ts_code', 'list_date', 'delist_date')
CALENDAR_FIELDS = ('cal_date', 'is_open')
VALUATION_FIELDS = ('ts_code', 'trade_date', 'pe_ttm', 'total_mv')
TUSHARE_ROW_LIMIT = 6000
MIN_COVERAGE = 0.95
SYNC_MAX_DAYS = 6 * 366


def _tushare_table(session, token, api, params, fields):
    try:
        response = session.post(TUSHARE_URL, json={
            'api_name': api, 'token': token, 'params': params, 'fields': ','.join(fields),
        }, timeout=20, allow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if payload.get('code') != 0:
            raise MarketUnavailable(f'Tushare {api} 请求被拒绝，请核对接口权限和频率限制')
        table = payload['data']
        columns, rows = table['fields'], table['items']
        if table.get('has_more') or len(rows) >= TUSHARE_ROW_LIMIT:
            raise MarketUnavailable(f'Tushare {api} 结果可能截断，不发布')
        if not rows and not columns:
            return pd.DataFrame(columns=list(fields))
        if len(columns) != len(fields) or set(columns) != set(fields):
            raise ValueError('unexpected fields')
        return pd.DataFrame(rows, columns=columns)
    except (requests.RequestException, AttributeError, KeyError, TypeError, ValueError):
        raise MarketUnavailable(f'Tushare {api} 请求失败或返回格式无效') from None


def _calendar(fetch, start, end):
    calendar = fetch('trade_cal', {'exchange': 'SSE', 'start_date': start.strftime('%Y%m%d'),
                                   'end_date': end.strftime('%Y%m%d')}, CALENDAR_FIELDS)
    calendar_days = calendar['cal_date'].tolist()
    expected = {(start + timedelta(days=offset)).strftime('%Y%m%d') for offset in range((end - start).days + 1)}
    states = calendar['is_open'].astype(str)
    if len(calendar_days) != len(expected) or set(calendar_days) != expected or not states.isin(['0', '1']).all():
        raise MarketUnavailable('Tushare 交易日历不完整或状态无效')
    return sorted(calendar.loc[states.eq('1'), 'cal_date'])


def _roster(fetch):
    result = {}
    for status in ('L', 'D', 'P'):
        frame = fetch('stock_basic', {'list_status': status}, ROSTER_FIELDS)
        for code, listed, delisted in frame[list(ROSTER_FIELDS)].itertuples(index=False, name=None):
            if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{6}\.(SH|SZ|BJ)', code) or
                    code in result or not isinstance(listed, str) or not re.fullmatch(r'[0-9]{8}', listed) or
                    (delisted and (not isinstance(delisted, str) or not re.fullmatch(r'[0-9]{8}', delisted)
                                   or delisted <= listed)) or (status == 'D' and not delisted)):
                raise MarketUnavailable('Tushare 股票名录代码、上市日期或退市日期无效或重复')
            result[code] = (listed, delisted)
    if {code[-2:] for code in result} != set(MARKETS):
        raise MarketUnavailable('Tushare 股票名录未包含沪、深、北三个市场')
    return result


def _audit_day(frame, roster, day, minimum):
    if frame.empty or not frame['trade_date'].eq(day).all() or frame['ts_code'].duplicated().any():
        raise MarketUnavailable(f'{day} 是交易日，但日线为空、日期不符或代码重复')
    eligible = {code for code, (listed, delisted) in roster.items() if listed <= day and (not delisted or day < delisted)}
    received = set(frame['ts_code'])
    if received - eligible:
        raise MarketUnavailable(f'{day} 日线出现名录上市区间之外的代码（{len(received - eligible)} 只）')
    coverage = {}
    for market in MARKETS:
        listed = {code for code in eligible if code.endswith(f'.{market}')}
        bars = len(received & listed)
        ratio = bars / len(listed) if listed else None
        if ratio is not None and ratio < minimum:
            raise MarketUnavailable(f'{day} {market} 有日线 {bars}/{len(listed)}，低于 {minimum:.1%}；不发布')
        coverage[market] = {'listed': len(listed), 'with_bar': bars, 'bar_ratio': ratio}
    return coverage


def _valuation_day(table, matching_codes, day):
    if table.empty or not table['trade_date'].eq(day).all() or table['ts_code'].duplicated().any():
        raise MarketUnavailable(f'{day} 估值为空、日期不符或代码重复')
    table = table[table['ts_code'].isin(matching_codes)].copy()
    cap = numeric(table['total_mv'].replace('', None), 'daily_basic.total_mv')
    ttm = numeric(table['pe_ttm'].replace('', None), 'daily_basic.pe_ttm')
    if cap.le(0).any() or ttm.le(0).any():
        raise MarketUnavailable(f'{day} 估值含非正的总市值或 PE(TTM)')
    coverage = {}
    for market_code in MARKETS:
        expected = sum(code.endswith(f'.{market_code}') for code in matching_codes)
        in_market = table['ts_code'].str.endswith(f'.{market_code}').fillna(False)
        with_cap = int((in_market & cap.notna()).sum())
        if expected and with_cap / expected < MIN_COVERAGE:
            raise MarketUnavailable(f'{day} {market_code} 有市值 {with_cap}/{expected}，低于 {MIN_COVERAGE:.1%}；不发布')
        coverage[market_code] = {'daily_bars': expected, 'with_market_cap': with_cap,
                                 'with_pe_ttm': int((in_market & ttm.notna()).sum())}
    return pd.DataFrame({'symbol': table['ts_code'], 'trade_date': _iso_basic(day), 'market_cap': cap / 10_000,
                         'pe_ttm': ttm, 'pe_dynamic': None, 'industry': None}), coverage


def _iso_basic(value):
    return datetime.strptime(value, '%Y%m%d').date().isoformat()


def _pull_tushare(token, start, end, now, *, valuation=False, allow_empty=False):
    if not token:
        raise MarketUnavailable('TUSHARE_TOKEN 未设置；不回退到未授权数据')
    hour = 17 if valuation else 16
    cutoff = ready_day(now, hour)
    max_days = SYNC_MAX_DAYS if valuation else 401
    if not 0 <= (end - start).days < max_days or end > cutoff:
        raise ValueError(f'导入范围最多 {max_days} 个自然日；北京时间 {hour}:00 前不纳入当天')
    reference_from = cutoff - timedelta(days=45)
    reference_through = cutoff + timedelta(days=14)
    frames = []
    coverage = {}
    attributes = []
    valuation_coverage = {}
    with requests.Session() as session:
        last_call = None

        def fetch(api, params, fields):
            nonlocal last_call
            if last_call is not None:
                time.sleep(max(0, 1.3 - (time.monotonic() - last_call)))
            last_call = time.monotonic()
            return _tushare_table(session, token, api, params, fields)

        open_days = []
        for offset in range(0, (end - start).days + 1, 401):
            first = start + timedelta(days=offset)
            open_days.extend(_calendar(fetch, first, min(end, first + timedelta(days=400))))
        if not open_days and allow_empty:
            return None, None, None
        if not open_days:
            raise MarketUnavailable('所选区间没有交易日')
        reference_days = _calendar(fetch, reference_from, reference_through)
        if not any(day <= cutoff.strftime('%Y%m%d') for day in reference_days):
            raise MarketUnavailable('近期交易日历没有已到日期的交易日')
        overlap_start = max(start, reference_from).strftime('%Y%m%d')
        overlap_end = end.strftime('%Y%m%d')
        if ({day for day in open_days if overlap_start <= day <= overlap_end} !=
                {day for day in reference_days if overlap_start <= day <= overlap_end}):
            raise MarketUnavailable('两次交易日历结果在重叠区间不一致')
        roster = _roster(fetch)
        for raw_day in open_days:
            chunk = fetch('daily', {'trade_date': raw_day}, TUSHARE_FIELDS)
            day = _iso_basic(raw_day)
            coverage[day] = _audit_day(chunk, roster, raw_day, MIN_COVERAGE)
            if chunk['pct_chg'].isna().any():
                raise MarketUnavailable(f'{day} 缺少上游涨跌幅，不发布')
            chunk['trade_date'] = day
            frames.append(chunk.rename(columns={'ts_code': 'symbol', 'vol': 'volume', 'pct_chg': 'pct_change'}))
            if valuation:
                table = fetch('daily_basic', {'trade_date': raw_day}, VALUATION_FIELDS)
                values, valuation_coverage[day] = _valuation_day(table, set(chunk['ts_code']), raw_day)
                attributes.append(values)
    manifest = {
        'version': 1, 'provider': 'tushare', 'imported_at': datetime.now(SHANGHAI).isoformat(),
        'latest_trade_date': max(coverage), 'coverage_by_date': coverage, 'minimum_market_bar_ratio': MIN_COVERAGE,
        'freshness_calendar': {'from': reference_from.isoformat(), 'through': reference_through.isoformat(),
                               'open_days': [_iso_basic(day) for day in reference_days]},
    }
    if valuation:
        manifest['valuation_by_date'] = valuation_coverage
    return pd.concat(frames, ignore_index=True), manifest, pd.concat(attributes, ignore_index=True) if valuation else None


def tushare_daily(token, start, end, *, now=None):
    frame, manifest, _ = _pull_tushare(token, start, end, now or datetime.now(SHANGHAI))
    return frame, manifest


def write_daily(output, frame):
    output.mkdir(parents=True, exist_ok=True)
    target = output / 'daily.csv'
    if target.exists():
        raise ValueError('daily.csv 已存在；请选择新目录，不覆盖原始行情')
    with TemporaryDirectory(dir=output) as staging:
        staging = Path(staging)
        temp = staging / 'daily.csv'
        columns = [*DATASETS['daily'], *(['pct_change'] if 'pct_change' in frame else [])]
        frame[columns].to_csv(temp, index=False)
        checked = load('daily', staging)
        if checked.empty:
            raise MarketUnavailable('不能发布空日线')
        os.link(temp, target)
    return checked


def write_snapshot(output, frame, manifest, attributes=None):
    if output.exists():
        raise ValueError('审计快照需使用新的输出目录，不覆盖原始行情')
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as staging:
        snapshot = Path(staging) / 'snapshot'
        checked = write_daily(snapshot, frame)
        if 'pct_change' not in checked or checked['pct_change'].isna().any():
            raise MarketUnavailable('审计快照缺少上游涨跌幅，不发布')
        manifest = {**manifest, 'daily_sha256': file_digest(snapshot / 'daily.csv')}
        if attributes is not None:
            attribute_path = snapshot / 'attributes.csv'
            attributes.to_csv(attribute_path, index=False)
            validated = load('attributes', snapshot)
            if set(validated['trade_date']) != set(checked['trade_date']):
                raise MarketUnavailable('日线与估值交易日不一致')
            manifest['attributes_sha256'] = file_digest(attribute_path)
        (snapshot / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        snapshot.rename(output)
    return checked


def sync_tushare(output, token, start=None, end=None, *, now=None):
    import fcntl

    if not token:
        raise MarketUnavailable('TUSHARE_TOKEN 未设置；不回退到未授权数据')
    now = now or datetime.now(SHANGHAI)
    cutoff = ready_day(now, 17)
    end = end or cutoff
    if end > cutoff:
        raise ValueError('北京时间 17:00 前不纳入当天估值')
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / '.sync.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MarketUnavailable('已有盘后导入正在运行') from None
        return _sync_locked(output, token, start, end, now)


def _sync_locked(output, token, start, end, now):
    current = output / 'current'
    previous = None
    if current.is_symlink():
        previous = current.resolve(strict=True)
        if previous.parent != output or not previous.name.startswith('snapshot-'):
            raise ValueError('current 未指向本目录的受管快照')
        try:
            prior = verified_manifest(previous)
            latest = date.fromisoformat(prior['latest_trade_date'])
            if 'attributes_sha256' not in prior or prior['publication'] != 'tushare-sync':
                raise ValueError
        except (OSError, KeyError, TypeError, ValueError):
            raise MarketUnavailable('上次行情快照校验失败；请检查 current，不覆盖') from None
        if start is not None:
            raise ValueError('续更无需指定 --start；不会改写历史')
        if end <= latest:
            return None
        start = latest + timedelta(days=1)
    elif current.exists():
        raise ValueError('current 已存在且不是受管快照链接')
    elif start is None:
        raise ValueError('首次同步须指定 --start；月线 MACD 建议至少四年历史')

    frame, manifest, attributes = _pull_tushare(token, start, end, now, valuation=True, allow_empty=previous is not None)
    if frame is None:
        return None
    if previous is not None:
        frame = pd.concat([load('daily', previous), frame], ignore_index=True)
        attributes = pd.concat([load('attributes', previous), attributes], ignore_index=True)
        for key in ('coverage_by_date', 'valuation_by_date'):
            manifest[key] = {**prior[key], **manifest[key]}
    manifest['publication'] = 'tushare-sync'
    snapshot = output / f"snapshot-{manifest['latest_trade_date']}-{uuid4().hex[:12]}"
    write_snapshot(snapshot, frame, manifest, attributes)
    with TemporaryDirectory(dir=output) as staging:
        link = Path(staging) / 'current'
        link.symlink_to(snapshot.name, target_is_directory=True)
        link.replace(current)
    return snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description='导入未复权日线；只读行情，不提供交易功能')
    parser.add_argument('--output', required=True, type=Path)
    providers = parser.add_subparsers(dest='provider', required=True)
    public = providers.add_parser('public-sample', help='腾讯已收历史日线；仅用于指定样本，不是全市场')
    public.add_argument('symbols', nargs='+')
    tushare = providers.add_parser('tushare', help='按交易日批量获取 Tushare 日线；需要已授权 Token')
    tushare.add_argument('--start', required=True, type=date.fromisoformat)
    tushare.add_argument('--end', required=True, type=date.fromisoformat)
    sync = providers.add_parser('tushare-sync', help='建立或增量更新受管日线和估值快照（macOS / Linux）')
    sync.add_argument('--start', type=date.fromisoformat, help='仅首次导入必填')
    sync.add_argument('--end', type=date.fromisoformat, help='默认最近已到发布时间的自然日')
    args = parser.parse_args(argv)
    try:
        if args.provider == 'public-sample':
            codes = {normalize_symbol(symbol) for symbol in args.symbols}
            if len(codes) != len(args.symbols) or not 1 <= len(codes) <= 20:
                raise ValueError('公开样本须提供 1–20 个不同的股票代码')
            frame = pd.concat([public_daily(symbol) for symbol in args.symbols], ignore_index=True)
            checked = write_daily(args.output, frame)
        elif args.provider == 'tushare':
            if args.output.exists():
                raise ValueError('审计快照需使用新的输出目录，不覆盖原始行情')
            frame, manifest = tushare_daily(os.getenv('TUSHARE_TOKEN'), args.start, args.end)
            checked = write_snapshot(args.output, frame, manifest)
            print(f'验收清单：{args.output / MANIFEST}')
        else:
            snapshot = sync_tushare(args.output, os.getenv('TUSHARE_TOKEN'), args.start, args.end)
            print(f'已发布：{snapshot}；MCP 数据目录：{args.output / "current"}' if snapshot else '没有新增交易日；保留现有快照')
            return 0
    except (MarketUnavailable, ValueError, OSError) as error:
        parser.exit(2, f'导入失败：{error}\n')
    print(f"{args.provider}：{len(checked)} 行，{checked['symbol'].nunique()} 只，"
          f"{checked['trade_date'].min()} 至 {checked['trade_date'].max()}；覆盖范围以导入文件为准")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
