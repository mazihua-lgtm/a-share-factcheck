"""Read-only A-share research and filing verification tools."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

import pandas as pd
from fastmcp import FastMCP
from pydantic import Field

from finchecker import documents, market
from finchecker.daily_snapshot import MANIFEST
from finchecker.extractor import extract_claims
from finchecker.fetcher import get_source
from finchecker.verifier import verify_claims

mcp = FastMCP('a-share-factcheck')
Dataset = Literal['daily', 'attributes', 'quotes', 'limit_events']
DocumentKind = Literal['announcement', 'report', 'news', 'minutes', 'dongmi']
Limit = Annotated[int, Field(ge=1, le=100)]


def guarded(func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except market.MarketUnavailable as error:
        return {'status': 'unavailable', 'reason': str(error)}
    except ValueError as error:
        return {'status': 'invalid_input', 'reason': str(error)}


@mcp.tool()
def discover_data() -> dict:
    """List available datasets, exact field names, screening presets and feed requirements."""
    root = market.data_root()
    datasets = {name: {'fields': [*fields, *market.OPTIONAL_COLUMNS.get(name, ())],
                       'configured': root is not None and (root / f'{name}.csv').is_file()}
                for name, fields in market.DATASETS.items()}
    datasets['daily']['manifest_present'] = root is not None and (root / MANIFEST).is_file()
    return {'datasets': datasets, 'screen_fields': list(market.DAILY_FIELDS), 'presets': market.PRESETS,
            'realtime_derived_fields': [*market.INDICATORS, 'day_cross_above_ma20'],
            'document_kinds': list(documents.KINDS),
            'local_documents_configured': root is not None and (root / 'documents.jsonl').is_file(),
            'automaticOrder': False,
            'public_sources': {'daily_single_symbol': 'Tencent', 'announcement_index': 'CNINFO'}}


def _query(dataset, symbol, start_date, end_date, fields, limit, offset):
    root = market.data_root()
    frame = market.load(dataset, root)
    meta = market.daily_metadata(root, frame['trade_date'].max(), frame) if dataset == 'daily' and not frame.empty else {}
    day = frame['as_of'].dt.tz_convert(market.SHANGHAI).dt.strftime('%Y-%m-%d') if dataset == 'quotes' else frame['trade_date']
    if start_date:
        frame = frame[day.ge(market.iso_day(start_date))]
    if end_date:
        frame = frame[day.loc[frame.index].le(market.iso_day(end_date))]
        if start_date and market.iso_day(start_date) > market.iso_day(end_date):
            raise ValueError('开始日期晚于结束日期')
    if symbol:
        frame = frame[frame['symbol'].eq(market.normalize_symbol(symbol))]
    chosen = fields or list(frame)
    if set(chosen) - set(frame) or len(set(chosen)) != len(chosen):
        raise ValueError('fields 含未知或重复列；请先查 discover_data')
    if dataset == 'quotes':
        frame = frame.assign(as_of=frame['as_of'].dt.tz_convert(market.SHANGHAI).map(datetime.isoformat))
    return {'status': 'ok', 'source': f'local_{dataset}', 'total': len(frame), **meta,
            'next_offset': offset + limit if offset + limit < len(frame) else None,
            'rows': market.records(frame[chosen].iloc[offset:offset + limit])}


@mcp.tool()
def query_dataset(dataset: Dataset, symbol: str | None = None, start_date: str | None = None,
                  end_date: str | None = None, fields: list[str] | None = None, limit: Limit = 50,
                  offset: Annotated[int, Field(ge=0)] = 0) -> dict:
    """Get rows from a configured dataset, filter by ISO date and stock code, and page with next_offset."""
    return guarded(_query, dataset, symbol, start_date, end_date, fields, limit, offset)


@mcp.tool()
def search_documents(keyword: Annotated[str, Field(max_length=100)] = '', kind: DocumentKind = 'announcement',
                     symbol: str | None = None, start_date: str | None = None,
                     end_date: str | None = None, limit: Limit = 20) -> dict:
    """Search up to 90 calendar days. SSE/SZSE announcements by stock code use CNINFO; other items use licensed local files."""
    return guarded(documents.search, market.data_root(), keyword, kind, symbol, start_date, end_date, limit)


@mcp.tool()
def get_document(ref: Annotated[str, Field(max_length=256)]) -> dict:
    """Return an announcement's original-publication link or the authorized summary of a local research document."""
    return guarded(documents.get, market.data_root(), ref)


def _screen(conditions, preset, trade_date, sort_by, descending, limit):
    rules = list(conditions or [])
    if preset is not None:
        if preset not in market.PRESETS:
            raise ValueError('未知策略预设；请先查 discover_data')
        rules = [market.Condition(**rule) for rule in market.PRESETS[preset]] + rules
    if not rules or len(rules) > 12:
        raise ValueError('需要 1–12 项筛选条件')
    frame, meta = market.market_view(market.data_root(), trade_date)
    if trade_date is None and meta.get('provider') == 'tushare' and meta['freshness'] != 'current':
        return {'status': 'unavailable', **meta, 'reason': '日线未对齐最近已到发布时间的交易日；更新数据或明确指定历史交易日'}
    return {'status': 'ok', **meta, **market.apply_screen(frame, rules, sort_by, descending, limit)}


@mcp.tool()
def screen_stocks(conditions: list[market.Condition] | None = None, preset: str | None = None,
                  trade_date: str | None = None, sort_by: str = 'pct_change',
                  descending: bool = True, limit: Limit = 30) -> dict:
    """Screen imported A-share daily bars with AND conditions and presets; return the exact executed rules and coverage."""
    return guarded(_screen, conditions, preset, trade_date, sort_by, descending, limit)


def _diagnose(symbol, trade_date):
    code = market.normalize_symbol(symbol)
    root = market.data_root()
    if root is not None and (root / 'daily.csv').is_file():
        frame, meta = market.market_view(root, trade_date, [code])
        if frame.empty:
            raise market.MarketUnavailable('指定交易日无该股票的有效成交记录')
    else:
        bars = market.public_daily(symbol)
        if trade_date:
            target = market.iso_day(trade_date)
            if not bars['trade_date'].eq(target).any():
                raise market.MarketUnavailable('公开日线中的指定交易日无记录或尚未纳入稳妥口径')
            bars = bars[bars['trade_date'].le(target)]
        frame = pd.DataFrame([market.summarize(bars)])
        meta = {'source': 'tencent_unadjusted_daily', 'trade_date': bars['trade_date'].max(),
                'coverage': 'single_symbol', 'period_basis': 'day/week/month through trade_date'}
    return {'status': 'ok', **meta, 'symbol': code, 'evidence': market.records(frame)[0],
            'scope': '行情及可计算技术指标；不推断资金流向或题材归属'}


@mcp.tool()
def diagnose_stock(symbol: str, trade_date: str | None = None) -> dict:
    """Return one stock's verifiable technical evidence; no feed falls back to prior-day public Tencent bars."""
    return guarded(_diagnose, symbol, trade_date)


def _realtime(view, symbols, conditions, sort_by, descending, limit):
    root = market.data_root()
    frame, meta = market.realtime_view(root)
    if view == 'market':
        pools = {state: market.records(frame[frame['limit_status'].eq(state)].head(limit))
                 for state in ('sealed', 'opened', 'touched')}
        if 'limit_down_status' in frame:
            pools['limit_down'] = market.records(frame[frame['limit_down_status'].eq('sealed')].head(limit))
        return {'status': 'ok', **meta, 'breadth': market.market_breadth(frame), 'pools': pools, 'pool_limit': limit,
                'truncated': any(frame['limit_status'].eq(state).sum() > limit for state in ('sealed', 'opened', 'touched'))
                or ('limit_down_status' in frame and int(frame['limit_down_status'].eq('sealed').sum()) > limit)}
    if view == 'quotes':
        if not symbols or len(symbols) > 50:
            raise ValueError('quotes 模式须指定 1–50 只股票')
        codes = {market.normalize_symbol(symbol) for symbol in symbols}
        selected = frame[frame['symbol'].isin(codes)]
        selected, derived = market.with_intraday_indicators(selected, root, meta['trade_date'])
        selected, attribute_date = market.with_attributes(selected, root, meta['trade_date'])
        return {'status': 'ok', **meta, 'missing_symbols': sorted(codes - set(selected['symbol'])),
                'rows': market.records(selected.head(limit)), 'truncated': len(selected) > limit,
                'indicative_open_bars': derived, 'attributes_date': attribute_date}
    if not conditions or len(conditions) > 12:
        raise ValueError('screen 模式需要 1–12 项条件')
    if meta['freshness'] != 'recent_snapshot':
        raise market.MarketUnavailable(f"当前无新鲜盘中帧（最近 {meta['as_of']}），不生成实时筛选结果")
    frame, derived = market.with_intraday_indicators(frame, root, meta['trade_date'])
    frame, attribute_date = market.with_attributes(frame, root, meta['trade_date'])
    if sort_by in market.ATTRIBUTE_FIELDS:
        raise ValueError('盘后属性仅用于盘中筛选，不用于盘中排序')
    return {'status': 'ok', **meta, **market.apply_screen(frame, conditions, sort_by, descending, limit),
            'indicative_open_bars': derived, 'attributes_date': attribute_date}


@mcp.tool()
def get_realtime(view: Literal['market', 'quotes', 'screen'] = 'market', symbols: list[str] | None = None,
                 conditions: list[market.Condition] | None = None, sort_by: str = 'pct_change',
                 descending: bool = True, limit: Limit = 30) -> dict:
    """Read the latest authorized quote frame, market breadth and limit pools. A stale frame is explicitly labeled."""
    return guarded(_realtime, view, symbols, conditions, sort_by, descending, limit)


@mcp.tool()
def predict_lianban_promotion(board_height: Annotated[int, Field(ge=1)], as_of: str | None = None,
                             lookback_days: Annotated[int, Field(ge=1, le=730)] = 180) -> dict:
    """Return only a historical promotion frequency from finalized imported outcomes; not a future probability."""
    return guarded(market.promotion_baseline, market.data_root(), board_height, as_of, lookback_days)


@mcp.tool()
def check_financial_claims(text: str, source: Literal['disclosure', 'akshare'] = 'disclosure') -> dict:
    """Compare extracted Chinese A-share financial claims against a selected source.

    Args:
        text: Research or news text to check.
        source: 'disclosure' (limited official filing snapshot) or 'akshare' (third-party summary).
    Returns source, overall status, and per-claim results with available evidence.
    """
    if source not in ('disclosure', 'akshare'):
        raise ValueError('Use demo_financial_claims for simulated data')
    report = verify_claims(extract_claims(text), get_source(source))
    return report.model_dump(mode='json')


@mcp.tool()
def demo_financial_claims() -> dict:
    """Run a fixed fictional example. Results are simulated and must not be used as verification."""
    text = '600519 2025年三季报：营业收入130亿元，同比增长8.3%。'
    return verify_claims(extract_claims(text), get_source('mock')).model_dump(mode='json')


def main() -> None:
    mcp.run()


if __name__ == '__main__':
    main()
