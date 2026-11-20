"""Read-only market snapshots and deterministic, date-bounded screening."""
from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, timedelta
from operator import eq, ge, gt, le, lt, ne
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finchecker.daily_snapshot import snapshot_metadata, verify_attributes

SHANGHAI = ZoneInfo('Asia/Shanghai')
DATASETS = {
    'daily': ('trade_date', 'symbol', 'open', 'high', 'low', 'close', 'volume'),
    'attributes': ('trade_date', 'symbol', 'pe_dynamic', 'market_cap', 'industry'),
    'quotes': ('as_of', 'symbol', 'price', 'pct_change', 'volume_ratio', 'limit_status', 'status'),
    'limit_events': ('trade_date', 'symbol', 'board_height', 'outcome_date', 'promoted'),
}
OPTIONAL_COLUMNS = {'daily': ('pct_change',), 'attributes': ('pe_ttm',),
                    'quotes': ('board_height', 'limit_down_status', 'open', 'high', 'low', 'volume')}
ATTRIBUTE_FIELDS = (*DATASETS['attributes'][2:], *OPTIONAL_COLUMNS['attributes'])
NUMERIC = {'open', 'high', 'low', 'close', 'volume', 'pe_dynamic', 'pe_ttm', 'market_cap', 'price',
           'pct_change', 'volume_ratio', 'board_height', 'promoted'}
PERIOD_FIELDS = ('ma5', 'ma10', 'ma20', 'ma60', 'macd_dif', 'macd_dea', 'macd_hist',
                 'kdj_k', 'kdj_d', 'kdj_j', 'ma_bull', 'macd_bull', 'kdj_bull',
                 'rsi6', 'rsi12', 'rsi24', 'boll_upper', 'boll_middle', 'boll_lower')
INDICATORS = tuple(f'{period}_{field}' for period in ('day', 'week', 'month') for field in PERIOD_FIELDS)
DAILY_FIELDS = ('symbol', 'trade_date', 'open', 'high', 'low', 'close', 'volume', 'pct_change',
                'volume_ratio', 'day_cross_above_ma20', *ATTRIBUTE_FIELDS, *INDICATORS)
OPS = {'>': gt, '>=': ge, '<': lt, '<=': le, '=': eq, '!=': ne}
STATES = {'ok', 'suspended', 'no_trade'}
LIMIT_UP = {'untouched', 'touched', 'opened', 'sealed'}
LIMIT_DOWN = {'untouched', 'touched', 'sealed'}


class Condition(BaseModel):
    model_config = ConfigDict(extra='forbid')

    field: str
    op: Literal['>', '>=', '<', '<=', '=', '!=']
    value: float | str | None = None
    ref: str | None = None
    scale: float = Field(default=1, gt=0, allow_inf_nan=False)

    @model_validator(mode='after')
    def check_operand(self):
        if (self.value is None) == (self.ref is None):
            raise ValueError('provide exactly one of value or ref')
        if self.ref is None and self.scale != 1:
            raise ValueError('scale requires ref')
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError('value must be finite')
        return self


PRESETS = {
    'ma_macd_trend': [{'field': 'day_ma_bull', 'op': '=', 'value': 1},
                      {'field': 'week_macd_bull', 'op': '=', 'value': 1}],
    'above_ma20_with_volume': [{'field': 'close', 'op': '>', 'ref': 'day_ma20'},
                               {'field': 'volume_ratio', 'op': '>=', 'value': 2}],
    'cross_ma20_with_volume': [{'field': 'day_cross_above_ma20', 'op': '=', 'value': 1},
                               {'field': 'volume_ratio', 'op': '>=', 'value': 2}],
    'multi_period_value': [{'field': 'week_kdj_bull', 'op': '=', 'value': 1},
                           {'field': 'month_macd_bull', 'op': '=', 'value': 1},
                           {'field': 'pe_dynamic', 'op': '>', 'value': 0},
                           {'field': 'pe_dynamic', 'op': '<', 'value': 30}],
    'multi_period_value_ttm': [{'field': 'week_kdj_bull', 'op': '=', 'value': 1},
                               {'field': 'month_macd_bull', 'op': '=', 'value': 1},
                               {'field': 'pe_ttm', 'op': '>', 'value': 0},
                               {'field': 'pe_ttm', 'op': '<', 'value': 30}],
}


class MarketUnavailable(RuntimeError):
    pass


def normalize_symbol(symbol):
    match = re.fullmatch(r'(?:(SH|SZ|BJ))?(\d{6})(?:\.(SH|SZ|BJ))?', symbol.strip().upper())
    if not match:
        raise ValueError('股票代码须为 6 位数字，可带 SH/SZ/BJ 市场标记')
    prefix, code, suffix = match.groups()
    if prefix and suffix and prefix != suffix:
        raise ValueError('股票代码的市场标记不一致')
    return code


def iso_day(value):
    return date.fromisoformat(value).isoformat()


def numeric(series, label):
    result = pd.to_numeric(series, errors='coerce')
    if (series.notna() & (result.isna() | result.isin([float('inf'), float('-inf')]))).any():
        raise MarketUnavailable(f'{label} 含无效数字')
    return result


def records(frame):
    safe = frame.replace([float('inf'), float('-inf')], float('nan'))
    return safe.astype(object).where(safe.notna(), None).to_dict(orient='records')


def data_root():
    value = os.getenv('FINCHECKER_MARKET_DIR')
    return Path(value).expanduser().resolve() if value else None


def load(dataset, root):
    columns = DATASETS[dataset]
    if root is None or not (root / f'{dataset}.csv').is_file():
        raise MarketUnavailable(f'未配置 {dataset}.csv；请用 FINCHECKER_MARKET_DIR 指定有权使用的数据目录')
    if dataset == 'attributes':
        try:
            verify_attributes(root)
        except (OSError, ValueError, TypeError):
            raise MarketUnavailable('估值验收清单无效或行情文件已变更；请重新导入') from None
    optional = OPTIONAL_COLUMNS.get(dataset, ())
    try:
        frame = pd.read_csv(root / f'{dataset}.csv', usecols=lambda field: field in (*columns, *optional),
                            dtype=str, keep_default_na=False, na_values=[''])
    except (OSError, UnicodeError, ValueError) as error:
        raise MarketUnavailable(f'{dataset}.csv 无法读取') from error
    missing = set(columns) - set(frame)
    if missing:
        raise MarketUnavailable(f'{dataset}.csv 缺少字段：{", ".join(sorted(missing))}')
    time_field = columns[0]
    try:
        frame['symbol'] = frame['symbol'].fillna('').map(normalize_symbol)
        for field in ('trade_date', 'outcome_date'):
            if field in frame:
                frame[field] = frame[field].fillna('').map(iso_day)
        if time_field == 'as_of':
            frame[time_field] = pd.to_datetime(frame[time_field].fillna('').map(parse_timestamp), utc=True)
    except (ValueError, OverflowError) as error:
        raise MarketUnavailable(f'{dataset}.csv 日期或股票代码无效') from error
    for field in set(frame) & NUMERIC:
        frame[field] = numeric(frame[field], f'{dataset}.{field}')
    if frame.duplicated([time_field, 'symbol']).any():
        raise MarketUnavailable(f'{dataset}.csv 存在重复时间与代码')
    if dataset == 'daily':
        prices = frame[['open', 'high', 'low', 'close']]
        if (prices.isna().any(axis=1) | prices.le(0).any(axis=1) |
                (frame['high'] < prices[['open', 'low', 'close']].max(axis=1)) |
                (frame['low'] > prices[['open', 'high', 'close']].min(axis=1)) |
                frame['volume'].isna() | frame['volume'].lt(0)).any():
            raise MarketUnavailable('daily.csv 含无效 OHLCV')
    if dataset == 'quotes':
        active = frame['status'].eq('ok')
        if ((~frame['status'].isin(STATES)) | (active & ~frame['limit_status'].isin(LIMIT_UP)) |
                (active & (frame['price'].isna() | frame['price'].le(0) |
                           frame['pct_change'].isna()))).any():
            raise MarketUnavailable('quotes.csv 含无效交易状态或行情')
        if 'limit_down_status' in frame and (active & ~frame['limit_down_status'].isin(LIMIT_DOWN)).any():
            raise MarketUnavailable('quotes.csv 含无效跌停状态')
    if dataset == 'limit_events' and (
        (frame['outcome_date'] <= frame['trade_date']) | ~frame['promoted'].isin([0, 1]) |
        frame['board_height'].isna() | frame['board_height'].lt(1) | (frame['board_height'] % 1).ne(0)
    ).any():
        raise MarketUnavailable('limit_events.csv 含无效板数、结果或结果日期')
    return frame.sort_values([time_field, 'symbol'], ignore_index=True)


def parse_timestamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('timestamp requires a UTC offset')
    return stamp


def indicators(bars):
    closing = bars['close']
    result = {f'ma{window}': closing.rolling(window).mean().iloc[-1] for window in (5, 10, 20, 60)}
    dif = closing.ewm(span=12, adjust=False, min_periods=12).mean() - closing.ewm(
        span=26, adjust=False, min_periods=26).mean()
    dea = dif.ewm(span=9, adjust=False, min_periods=9).mean()
    low = bars['low'].rolling(9).min()
    price_range = bars['high'].rolling(9).max() - low
    rsv = (closing - low) * 100 / price_range.where(price_range > 0)
    kval = rsv.ewm(alpha=1 / 3, adjust=False).mean().where(rsv.notna())
    dval = kval.ewm(alpha=1 / 3, adjust=False).mean().where(kval.notna())
    result.update(macd_dif=dif.iloc[-1], macd_dea=dea.iloc[-1], macd_hist=2 * (dif - dea).iloc[-1],
                  kdj_k=kval.iloc[-1], kdj_d=dval.iloc[-1], kdj_j=(3 * kval - 2 * dval).iloc[-1])
    changes = closing.diff()
    for window in (6, 12, 24):
        gain = changes.clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean().iloc[-1]
        loss = (-changes).clip(lower=0).ewm(alpha=1 / window, adjust=False, min_periods=window).mean().iloc[-1]
        result[f'rsi{window}'] = 100 * gain / (gain + loss) if gain + loss > 0 else None
    deviation = closing.rolling(20).std(ddof=0).iloc[-1]
    result.update(boll_middle=result['ma20'], boll_upper=result['ma20'] + 2 * deviation,
                  boll_lower=result['ma20'] - 2 * deviation)
    for field, values in {'ma_bull': [result[f'ma{n}'] for n in (5, 10, 20, 60)],
                          'macd_bull': [result['macd_dif'], result['macd_dea']],
                          'kdj_bull': [result['kdj_k'], result['kdj_d']]}.items():
        result[field] = None if any(pd.isna(number) for number in values) else int(
            all(left > right for left, right in zip(values, values[1:])))
    return result


def summarize(bars):
    bars = bars.sort_values('trade_date')
    latest = {key: bars.iloc[-1][key] for key in DATASETS['daily']}
    latest['pct_change'] = bars['pct_change'].iloc[-1] if 'pct_change' in bars else (
        None if len(bars) < 2 else (latest['close'] / bars['close'].iloc[-2] - 1) * 100)
    base = bars['volume'].iloc[-6:-1]
    latest['volume_ratio'] = latest['volume'] / base.mean() if len(base) == 5 and base.mean() > 0 else None
    closing = bars['close']
    latest['day_cross_above_ma20'] = None if len(bars) < 21 else int(
        closing.iloc[-2] <= closing.iloc[-21:-1].mean() and closing.iloc[-1] > closing.iloc[-20:].mean())
    series = bars.set_index(pd.to_datetime(bars['trade_date']))
    for period, frequency in (('day', None), ('week', 'W-FRI'), ('month', 'M')):
        aggregated = bars if frequency is None else series.groupby(series.index.to_period(frequency)).agg(
            {'close': 'last', 'high': 'max', 'low': 'min'})
        latest.update({f'{period}_{field}': value for field, value in indicators(aggregated).items()})
    return latest


def daily_metadata(root, target, bars, now=None):
    latest = bars['trade_date'].max()
    try:
        meta = snapshot_metadata(root, target, latest, now or datetime.now(SHANGHAI))
    except (OSError, ValueError, KeyError, TypeError):
        raise MarketUnavailable('日线验收清单无效或行情文件已变更；请重新导入') from None
    if 'pct_change' in bars:
        basis = 'Tushare pct_chg (ex-rights previous close)' if meta.get('provider') == 'tushare' else 'supplied pct_change'
    else:
        basis = 'loaded close / previous valid close - 1'
    return {'coverage': 'loaded_symbols_only', 'pct_change_basis': basis, **meta}


def market_view(root, trade_date=None, symbols=None, *, now=None):
    bars = load('daily', root)
    if bars.empty:
        raise MarketUnavailable('日线数据为空')
    target = iso_day(trade_date) if trade_date else bars['trade_date'].max()
    eligible = bars.loc[bars['trade_date'].eq(target) & bars['volume'].gt(0), 'symbol']
    if symbols is not None:
        eligible = eligible[eligible.isin(symbols)]
    if not bars['trade_date'].eq(target).any():
        raise MarketUnavailable(f'没有 {target} 的日线，不用其他日期替代')
    meta = daily_metadata(root, target, bars, now)
    sample = bars[bars['trade_date'].le(target) & bars['symbol'].isin(eligible) & bars['volume'].gt(0)]
    frame = pd.DataFrame([summarize(group) for _, group in sample.groupby('symbol')], columns=DAILY_FIELDS)
    frame, attribute_date = with_attributes(frame, root, target, include_target=True)
    return frame, {'source': 'local_daily', 'trade_date': target, 'attributes_date': attribute_date,
                   'period_basis': 'day/week/month through trade_date', **meta}


def with_attributes(frame, root, target, include_target=False):
    if root is None or not (root / 'attributes.csv').is_file():
        return frame, None
    attributes = load('attributes', root)
    days = attributes['trade_date']
    attributes = attributes[days.le(target) if include_target else days.lt(target)]
    if attributes.empty:
        return frame, None
    attribute_date = attributes['trade_date'].max()
    chosen = attributes.loc[attributes['trade_date'].eq(attribute_date)].drop(columns='trade_date')
    return frame.drop(columns=list(chosen.columns.difference(['symbol'])), errors='ignore').merge(
        chosen, on='symbol', how='left'), attribute_date


def apply_screen(frame, conditions, sort_by='pct_change', descending=True, limit=30):
    if sort_by not in frame:
        raise ValueError(f'不可排序字段：{sort_by}')
    present = pd.Series(True, index=frame.index)
    passed = present.copy()
    for condition in conditions:
        if condition.field not in frame or (condition.ref is not None and condition.ref not in frame):
            raise ValueError(f'未知或不可用的筛选字段：{condition.field} / {condition.ref}')
        left = frame[condition.field]
        is_text = condition.field in {'symbol', 'trade_date', 'industry', 'limit_status', 'limit_down_status', 'status'}
        if is_text:
            if condition.ref is not None or not isinstance(condition.value, str) or condition.op not in {'=', '!='}:
                raise ValueError(f'{condition.field} 只支持与文本作等于或不等于比较')
            right = condition.value
            valid = left.notna()
        else:
            left = pd.to_numeric(left, errors='coerce')
            finite_left = left.notna() & ~left.isin([float('inf'), float('-inf')])
            if condition.ref is not None:
                if condition.ref not in set(DAILY_FIELDS[2:]) | NUMERIC or condition.ref == 'industry':
                    raise ValueError('ref 必须是数值字段')
                right = pd.to_numeric(frame[condition.ref], errors='coerce') * condition.scale
                valid = finite_left & right.notna() & ~right.isin([float('inf'), float('-inf')])
            else:
                if not isinstance(condition.value, float):
                    raise ValueError(f'{condition.field} 必须与数值比较')
                right = condition.value
                valid = finite_left
        present &= valid
        passed &= valid & OPS[condition.op](left, right)
    matched = frame[passed].sort_values([sort_by, 'symbol'], ascending=[not descending, True], na_position='last')
    return {'universe': len(frame), 'matched': len(matched), 'missing_inputs': int((~present).sum()),
            'truncated': len(matched) > limit, 'conditions': [item.model_dump(exclude_none=True) for item in conditions],
            'sort_by': sort_by, 'descending': descending, 'rows': records(matched.head(limit))}


def realtime_view(root, now=None):
    now = now or datetime.now(SHANGHAI)
    frames = load('quotes', root)
    if frames.empty:
        raise MarketUnavailable('没有盘中快照')
    stamp = frames['as_of'].max().to_pydatetime().astimezone(SHANGHAI)
    age = (now - stamp).total_seconds()
    if age < -5:
        raise MarketUnavailable('快照时间晚于本机时间')
    market_open = now.weekday() < 5 and ('09:30' <= now.strftime('%H:%M') < '11:30' or
                                         '13:00' <= now.strftime('%H:%M') < '15:00')
    fresh = market_open and 0 <= age <= 180 and now.date() == stamp.date()
    frame = frames[frames['as_of'].eq(stamp) & frames['status'].eq('ok')].drop(columns='as_of')
    return frame, {'source': 'local_quotes', 'as_of': stamp.isoformat(), 'trade_date': stamp.date().isoformat(),
                   'coverage': 'loaded_symbols_only', 'freshness': 'recent_snapshot' if fresh else 'stale_snapshot',
                   'age_seconds': round(age, 1)}


def with_intraday_indicators(frame, root, target):
    required = ['open', 'high', 'low', 'price', 'volume']
    fields = (*INDICATORS, 'day_cross_above_ma20')
    if not set(required) <= set(frame) or root is None or not (root / 'daily.csv').is_file():
        return frame, False
    if frame.empty:
        return frame.assign(**{field: None for field in fields}), True
    historical = load('daily', root)
    historical = historical[historical['trade_date'].lt(target) & historical['symbol'].isin(frame['symbol']) &
                            historical['volume'].gt(0)]
    current = frame.dropna(subset=required).rename(columns={'price': 'close'}).assign(trade_date=target)
    if current.empty:
        return frame.assign(**{field: None for field in fields}), False
    if ((current[required[:3] + ['close']].le(0).any(axis=1)) | current['volume'].le(0) |
        current['high'].lt(current[['open', 'close', 'low']].max(axis=1)) |
        current['low'].gt(current[['open', 'close', 'high']].min(axis=1))).any():
        raise MarketUnavailable('盘中 OHLCV 不合法，无法重算指标')
    bars = pd.concat([historical, current[list(DATASETS['daily'])]], ignore_index=True)
    rows = {symbol: summarize(group) for symbol, group in bars.groupby('symbol')
            if group['trade_date'].eq(target).any()}
    values = pd.DataFrame.from_dict(rows, orient='index').reindex(columns=fields)
    values.index.name = 'symbol'
    return frame.merge(values, on='symbol', how='left'), True


def market_breadth(frame):
    changes = frame['pct_change']
    sealed = int(frame['limit_status'].eq('sealed').sum())
    broken = int(frame['limit_status'].eq('opened').sum())
    return {'sample': len(frame), 'up': int(changes.gt(0).sum()), 'down': int(changes.lt(0).sum()),
            'flat': int(changes.eq(0).sum()), 'limit_up': sealed, 'broken': broken,
            'limit_down': int(frame['limit_down_status'].eq('sealed').sum()) if 'limit_down_status' in frame else None,
            'broken_rate_pct': 100 * broken / (sealed + broken) if sealed + broken else None,
            'basis': '仅正常成交且已导入股票；炸板为 opened，分母为 opened + sealed'}


def promotion_baseline(root, board_height, as_of, lookback_days):
    events = load('limit_events', root)
    end = date.fromisoformat(as_of) if as_of else datetime.now(SHANGHAI).date()
    start = end - timedelta(days=lookback_days)
    sample = events[events['board_height'].eq(board_height) & events['trade_date'].ge(start.isoformat()) &
                    events['outcome_date'].le(end.isoformat())]
    observations = len(sample)
    promoted = int(sample['promoted'].sum())
    return {'source': 'local_limit_events', 'as_of': end.isoformat(), 'board_height': board_height,
            'lookback_days': lookback_days, 'sample': observations, 'promoted': promoted,
            'status': 'historical_baseline' if observations >= 30 else 'insufficient_sample',
            'historical_rate_pct': 100 * promoted / observations if observations >= 30 else None,
            'basis': '只统计截止日已知结果且已导入的样本；历史频率不是个股预测'}


def public_daily(symbol):
    original = symbol.strip().upper()
    code = normalize_symbol(original)
    exchange = 'sh' if code.startswith('6') else 'sz' if code.startswith(('00', '30')) else None
    if not exchange or ('.' in original and not original.endswith('.' + exchange.upper())) or (
        original[:2] in {'SH', 'SZ', 'BJ'} and original[:2].lower() != exchange
    ):
        raise MarketUnavailable('公共行情回退仅支持可确认的沪深 A 股；其他代码请导入日线')
    ticker = exchange + code
    ticker_date = datetime.now(SHANGHAI).date().isoformat()
    try:
        response = requests.get('https://web.ifzq.gtimg.cn/appstock/app/fqkline/get',
                                params={'param': f'{ticker},day,,,320,'}, timeout=8, allow_redirects=False)
        response.raise_for_status()
        rows = response.json()['data'][ticker]['day']
        frame = pd.DataFrame((row[:6] for row in rows), columns=['trade_date', 'open', 'close', 'high', 'low', 'volume'])
        frame['trade_date'] = frame['trade_date'].map(iso_day)
        if frame['trade_date'].duplicated().any():
            raise ValueError('duplicate dates')
        for field in ('open', 'close', 'high', 'low', 'volume'):
            frame[field] = numeric(frame[field], field)
        frame['symbol'] = code
        frame = frame[frame['trade_date'].lt(ticker_date) & frame['volume'].gt(0)]
        prices = frame[['open', 'close', 'high', 'low']]
        if (frame.empty or prices.isna().any().any() or prices.le(0).any().any() or
                frame['high'].lt(prices.max(axis=1)).any() or frame['low'].gt(prices.min(axis=1)).any()):
            raise ValueError('missing previous-session bars')
        return frame
    except (requests.RequestException, KeyError, TypeError, ValueError) as error:
        raise MarketUnavailable('腾讯公开日线暂不可用或数据结构有变') from error
