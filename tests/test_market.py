import asyncio
from datetime import date, datetime

import pandas as pd
import pytest
from fastmcp import Client

from finchecker import documents, market, market_import
from finchecker.mcp_server import mcp


@pytest.fixture
def dataset(tmp_path, monkeypatch):
    monkeypatch.setenv('FINCHECKER_MARKET_DIR', str(tmp_path))
    days = pd.bdate_range(end='2026-11-19', periods=65).strftime('%Y-%m-%d')
    bars = []
    for symbol, prices, volumes in [
        ('600519', [*([10] * 64), 12], [*([100] * 64), 220]),
        ('600900', list(range(10, 75)), [100] * 65),
        ('000001', [None] * 63 + [11, 11.5], [None] * 63 + [100, 100]),
    ]:
        bars.extend((day, symbol, close, close + 1, close - 1, close, volume)
                    for day, close, volume in zip(days, prices, volumes) if close is not None)
    bars += [('2026-11-20', '600519', 90, 91, 89, 90, 100)]
    pd.DataFrame(bars, columns=market.DATASETS['daily']).to_csv(tmp_path / 'daily.csv', index=False)
    (tmp_path / 'attributes.csv').write_text(
        'trade_date,symbol,pe_dynamic,market_cap,industry\n'
        '2026-11-19,600519,20,100,industry_a\n'
        '2026-11-20,600519,99,100,industry_b\n', encoding='utf-8')
    (tmp_path / 'documents.jsonl').write_text(
        '{"ref":"local:report-1","kind":"report","date":"2026-11-18","symbol":"600519",'
        '"title":"示例研究","url":"https://example.com/report","summary":"授权摘要"}\n', encoding='utf-8')
    return tmp_path


def test_screen_uses_requested_prefix_and_reports_missing(dataset):
    frame, metadata = market.market_view(dataset, '2026-11-19')
    assert metadata['attributes_date'] == '2026-11-19'
    assert frame.set_index('symbol').loc['600519', 'pe_dynamic'] == 20
    rules = [market.Condition(**rule) for rule in market.PRESETS['cross_ma20_with_volume']]
    result = market.apply_screen(frame, rules)
    assert (result['universe'], result['matched'], result['missing_inputs']) == (3, 1, 1)
    row = result['rows'][0]
    assert row['symbol'] == '600519' and row['day_ma20'] == pytest.approx(10.1)
    assert row['volume_ratio'] == pytest.approx(2.2)
    assert row['month_macd_bull'] is None
    empty_fields = market.apply_screen(frame, [market.Condition(field='month_macd_bull', op='!=', value=0)])
    assert empty_fields['matched'] == 0 and empty_fields['missing_inputs'] == 3
    sorted_rows = market.apply_screen(frame, [market.Condition(field='close', op='>', ref='day_ma20')], limit=1)
    assert sorted_rows['truncated'] and sorted_rows['matched'] == 2
    assert sorted_rows['rows'][0]['symbol'] == '600519'
    with pytest.raises(market.MarketUnavailable):
        market.market_view(dataset, '2026-11-21')


def test_realtime_uses_only_latest_frame_and_previous_attributes(dataset):
    (dataset / 'quotes.csv').write_text(
        'as_of,symbol,price,pct_change,volume_ratio,limit_status,status,board_height,limit_down_status,open,high,low,volume\n'
        '2026-11-20T10:00:00+08:00,600900,74,0,1,sealed,ok,1,untouched,74,75,73,100\n'
        '2026-11-20T10:01:00+08:00,600519,13,8,3,sealed,ok,2,untouched,12,13,12,50\n'
        '2026-11-20T10:01:00+08:00,000001,10,-10,1,opened,ok,,sealed,11,12,10,60\n'
        '2026-11-20T10:01:00+08:00,000002,,,,,suspended,,,,,,\n', encoding='utf-8')
    recent = datetime.fromisoformat('2026-11-20T10:02:00+08:00')
    frame, meta = market.realtime_view(dataset, recent)
    assert meta['freshness'] == 'recent_snapshot'
    breadth = market.market_breadth(frame)
    assert (breadth['sample'], breadth['limit_up'], breadth['limit_down'], breadth['broken_rate_pct']) == (2, 1, 1, 50)
    frame, derived = market.with_intraday_indicators(frame, dataset, meta['trade_date'])
    assert derived and frame.set_index('symbol').loc['600519', 'day_ma20'] == pytest.approx(10.25)
    frame, previous = market.with_attributes(frame, dataset, meta['trade_date'])
    assert previous == '2026-11-19' and frame.set_index('symbol').loc['600519', 'pe_dynamic'] == 20
    _, old = market.realtime_view(dataset, datetime.fromisoformat('2026-11-20T10:05:00+08:00'))
    assert old['freshness'] == 'stale_snapshot'


def test_realtime_mcp_pools_and_stale_screen(monkeypatch, dataset):
    frame = pd.DataFrame([
        {'symbol': '600519', 'price': 12, 'pct_change': 10, 'volume_ratio': 3,
         'limit_status': 'sealed', 'limit_down_status': 'untouched', 'status': 'ok'},
        {'symbol': '600900', 'price': 12, 'pct_change': -10, 'volume_ratio': 2,
         'limit_status': 'opened', 'limit_down_status': 'sealed', 'status': 'ok'},
    ])
    meta = {'as_of': '2026-11-20T10:02:00+08:00', 'trade_date': '2026-11-20', 'freshness': 'recent_snapshot'}
    monkeypatch.setattr(market, 'realtime_view', lambda root: (frame, meta))

    async def exercise():
        async with Client(mcp) as client:
            async def call(**arguments):
                return (await client.call_tool('get_realtime', arguments)).data

            pools = await call()
            assert pools['breadth']['broken_rate_pct'] == 50 and not pools['truncated']
            assert pools['pools']['limit_down'][0]['symbol'] == '600900'
            quotes = await call(view='quotes', symbols=['600519', '000001'])
            assert quotes['missing_symbols'] == ['000001'] and not quotes['indicative_open_bars']
            condition = {'field': 'volume_ratio', 'op': '>', 'value': 2}
            result = await call(view='screen', conditions=[condition])
            assert result['matched'] == 1
            meta['freshness'] = 'stale_snapshot'
            assert (await call(view='screen', conditions=[condition]))['status'] == 'unavailable'

    asyncio.run(exercise())


def test_promotion_excludes_unknown_future_outcomes(dataset):
    (dataset / 'limit_events.csv').write_text(
        'trade_date,symbol,board_height,outcome_date,promoted\n' +
        ''.join(f'2026-11-18,{600000 + index},2,2026-11-19,{int(index < 15)}\n' for index in range(30)) +
        '2026-11-19,600519,2,2026-11-20,1\n', encoding='utf-8')
    result = market.promotion_baseline(dataset, 2, '2026-11-19', 30)
    assert result['sample'] == 30 and result['historical_rate_pct'] == 50
    assert market.promotion_baseline(dataset, 3, '2026-11-19', 30)['historical_rate_pct'] is None


def test_mcp_tools_and_result_contract(dataset):
    async def exercise():
        async with Client(mcp) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {'discover_data', 'query_dataset', 'search_documents', 'get_document', 'screen_stocks',
                    'diagnose_stock', 'get_realtime', 'predict_lianban_promotion', 'check_financial_claims'} <= names

            async def call(name, **arguments):
                return (await client.call_tool(name, arguments)).data

            meta = await call('discover_data')
            assert not meta['automaticOrder'] and meta['datasets']['daily']['configured']
            result = await call('screen_stocks', preset='cross_ma20_with_volume', trade_date='2026-11-19')
            assert result['rows'][0]['symbol'] == '600519' and result['matched'] == 1
            rows = await call('query_dataset', dataset='daily', symbol='600519.SH', fields=['trade_date'], limit=1)
            assert rows['next_offset'] == 1 and rows['total'] == 66
            quotes = await call('get_realtime')
            assert quotes['status'] == 'unavailable'
            items = await call('search_documents', kind='report', keyword='示例', end_date='2026-11-20')
            item = await call('get_document', ref=items['rows'][0]['ref'])
            assert item['summary'] == '授权摘要'
            evidence = await call('diagnose_stock', symbol='600519', trade_date='2026-11-19')
            assert evidence['evidence']['close'] == 12
            invalid = await call('screen_stocks', conditions=[{'field': 'bogus', 'op': '=', 'value': 1}])
            assert invalid['status'] == 'invalid_input'
            official = await call('check_financial_claims', text='600519 2025年报：营业收入同比下降1.21%。')
            assert official['status'] == 'verified'

    asyncio.run(exercise())


def test_word_filter_does_not_change_the_cninfo_symbol_query(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {'hasMore': True, 'announcements': [
                {'secCode': '600519', 'announcementTitle': '年度报告',
                 'adjunctUrl': 'finalpage/2026-04-17/1234.PDF', 'announcementTime': 1_776_355_200_000},
                {'secCode': '600519', 'announcementTitle': '分红公告',
                 'adjunctUrl': 'finalpage/2026-04-17/5678.PDF', 'announcementTime': 1_776_355_200_000},
            ]}

    def post(url, data, **kwargs):
        assert url == documents.CNINFO and data['searchkey'] == '600519'
        return Response()

    monkeypatch.setattr(documents.requests, 'post', post)
    result = documents.search(None, '年度报告', 'announcement', '600519', '2026-04-16', '2026-04-18')
    assert result['truncated'] and len(result['rows']) == 1
    assert documents.get(None, result['rows'][0]['ref'])['status'] == 'source_only'


def test_importer_validates_before_publish_and_never_overwrites(dataset, tmp_path):
    bars = market.load('daily', dataset)
    output = tmp_path / 'imported'
    assert len(market_import.write_daily(output, bars)) == len(bars)
    with pytest.raises(ValueError, match='已存在'):
        market_import.write_daily(output, bars)
    with pytest.raises(market.MarketUnavailable, match='重复'):
        market_import.write_daily(tmp_path / 'duplicates', pd.concat([bars, bars]))
    assert not (tmp_path / 'duplicates' / 'daily.csv').exists()
    with pytest.raises(market.MarketUnavailable, match='TUSHARE_TOKEN'):
        market_import.tushare_daily('', date(2026, 11, 18), date(2026, 11, 19))
