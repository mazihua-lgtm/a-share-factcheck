import asyncio
from datetime import date, datetime, timedelta

import pytest
from fastmcp import Client

from finchecker import market, market_import
from finchecker.daily_snapshot import MANIFEST
from finchecker.mcp_server import mcp


NOW = datetime.fromisoformat('2026-11-20T16:30:00+08:00')
SYMBOLS = ('600519.SH', '000001.SZ', '920001.BJ')
NON_TRADING = '20261118'


@pytest.fixture
def upstream(monkeypatch):
    calls = []
    changes = {}

    class Response:
        def __init__(self, fields, rows, api):
            self.fields, self.rows, self.api = fields, rows, api

        def raise_for_status(self):
            pass

        def json(self):
            return {'code': 0, 'data': {'fields': self.fields, 'items': self.rows,
                                        'has_more': changes.get('truncate') == self.api}}

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def post(self, url, json, **kwargs):
            assert url == market_import.TUSHARE_URL and json['token'] == 'fixture-only'
            assert not kwargs['allow_redirects']
            api, params = json['api_name'], json['params']
            calls.append((api, params))
            fields = json['fields'].split(',')
            if api == 'trade_cal':
                start = datetime.strptime(params['start_date'], '%Y%m%d').date()
                end = datetime.strptime(params['end_date'], '%Y%m%d').date()
                rows = []
                for offset in range((end - start).days + 1):
                    day = start + timedelta(days=offset)
                    raw = day.strftime('%Y%m%d')
                    rows.append([raw, int(day.weekday() < 5 and raw != NON_TRADING)])
                if changes.get('missing_calendar'):
                    rows.pop()
            elif api == 'stock_basic':
                rows = ([[symbol, '20200101', None] for symbol in SYMBOLS] +
                        [['920002.BJ', '20261201', None]]) if params['list_status'] == 'L' else []
                if params['list_status'] == 'D':
                    rows = [['600999.SH', '20200101', '20221231' if changes.get('long_history') else '20261119']]
            elif api == 'daily_basic':
                day = params['trade_date']
                rows = [[symbol, day, price, 800000 + index * 10_000]
                        for index, (symbol, price) in enumerate(zip(SYMBOLS, (25.0, None, 20.0)))]
                if changes.get('missing_valuation') == day:
                    rows.pop()
                if changes.get('wrong_valuation_date') == day:
                    rows[0][1] = '20261118'
            elif api == 'daily':
                day = params['trade_date']
                assert day != NON_TRADING
                rows = [[symbol, day, 12, 12, 11, 12, 100, gain]
                        for symbol, gain in zip(SYMBOLS, (1.0, 2.0, 3.0))]
                if changes.get('empty_day') == day:
                    rows = []
                if changes.get('missing_exchange') == day:
                    rows.pop()
                if changes.get('future_listing') == day:
                    rows.append(['920002.BJ', day, 12, 12, 11, 12, 100, 4.0])
            else:
                raise AssertionError(api)
            return Response(fields, rows, api)

    monkeypatch.setattr(market_import.requests, 'Session', Session)
    monkeypatch.setattr(market_import.time, 'sleep', lambda seconds: None)
    return calls, changes


def test_import_publishes_calendar_roster_and_exact_pct_change(upstream, tmp_path, monkeypatch):
    calls, _ = upstream
    frame, manifest = market_import.tushare_daily('fixture-only', date(2026, 11, 18), date(2026, 11, 20), now=NOW)
    assert [params['trade_date'] for api, params in calls if api == 'daily'] == ['20261119', '20261120']
    output = tmp_path / 'audited'
    assert len(market_import.write_snapshot(output, frame, manifest)) == 6
    assert 'fixture-only' not in (output / MANIFEST).read_text()
    with pytest.raises(ValueError, match='不覆盖'):
        market_import.write_snapshot(output, frame, manifest)
    monkeypatch.setenv('FINCHECKER_MARKET_DIR', str(output))

    class Clock(datetime):
        current = NOW

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(market, 'datetime', Clock)

    async def exercise():
        async with Client(mcp) as client:
            arguments = {'conditions': [{'field': 'pct_change', 'op': '>', 'value': 0}]}
            screened = (await client.call_tool('screen_stocks', arguments)).data
            assert screened['freshness'] == 'current' and screened['expected_latest_trade_date'] == '2026-11-20'
            assert screened['universe'] == 3 and screened['market_coverage']['BJ']['listed'] == 1
            assert [row['pct_change'] for row in screened['rows']] == [3.0, 2.0, 1.0]
            assert all(row['close'] == 12 for row in screened['rows'])
            assert 'ex-rights' in screened['pct_change_basis']
            Clock.current = NOW + timedelta(days=3)
            stale = (await client.call_tool('screen_stocks', arguments)).data
            assert stale['status'] == 'unavailable' and stale['freshness'] == 'stale' and 'rows' not in stale
            history = (await client.call_tool('screen_stocks', {**arguments, 'trade_date': '2026-11-19'})).data
            assert history['status'] == 'ok' and history['freshness'] == 'historical_selection'
            queried = (await client.call_tool('query_dataset', {'dataset': 'daily'})).data
            assert queried['market_coverage_date'] == '2026-11-20' and queried['freshness'] == 'stale'
            Clock.current = NOW + timedelta(days=15)
            expired = (await client.call_tool('screen_stocks', arguments)).data
            assert expired['status'] == 'unavailable' and expired['freshness'] == 'unverified_calendar'

    asyncio.run(exercise())
    _, weekend = market.market_view(output, now=NOW + timedelta(days=1))
    assert weekend['freshness'] == 'current'
    _, unverified = market.market_view(output, now=NOW + timedelta(days=15))
    assert unverified['freshness'] == 'unverified_calendar'
    daily = output / 'daily.csv'
    daily.write_text(daily.read_text().replace(',3.0', ',4.0'))
    with pytest.raises(market.MarketUnavailable, match='已变更'):
        market.market_view(output, now=NOW)


@pytest.mark.parametrize(('problem', 'value', 'reason'), [
    ('truncate', 'daily', '截断'),
    ('missing_calendar', True, '日历不完整'),
    ('empty_day', '20261119', '日线为空'),
    ('missing_exchange', '20261119', 'BJ 有日线 0/1'),
    ('future_listing', '20261119', '上市区间之外'),
])
def test_incomplete_snapshots_fail_closed(upstream, problem, value, reason):
    _, changes = upstream
    changes[problem] = value
    with pytest.raises(market.MarketUnavailable, match=reason):
        market_import.tushare_daily('fixture-only', date(2026, 11, 19), date(2026, 11, 20), now=NOW)


def test_before_ready_cutoff_does_not_query_today(upstream):
    calls, _ = upstream
    with pytest.raises(ValueError, match='16:00'):
        market_import.tushare_daily('fixture-only', date(2026, 11, 19), date(2026, 11, 20),
                                    now=NOW.replace(hour=15))
    assert not calls


def test_sync_only_pulls_new_days_and_keeps_previous_on_failure(upstream, tmp_path, monkeypatch):
    calls, changes = upstream
    evening = NOW.replace(hour=18)
    root = tmp_path / 'managed'
    initial = market_import.sync_tushare(root, 'fixture-only', date(2026, 11, 19), now=evening)
    assert (root / 'current').resolve() == initial
    assert not market_import.sync_tushare(root, 'fixture-only', now=evening)
    assert not market_import.sync_tushare(root, 'fixture-only', now=evening + timedelta(days=2))
    calls.clear()
    monkeypatch.setenv('FINCHECKER_MARKET_DIR', str(root / 'current'))
    pinned = market.data_root()
    monday = evening + timedelta(days=3)
    changes['missing_valuation'] = '20261123'
    with pytest.raises(market.MarketUnavailable, match='BJ 有市值 0/1'):
        market_import.sync_tushare(root, 'fixture-only', now=monday)
    assert (root / 'current').resolve() == initial
    changes.clear()
    calls.clear()
    updated = market_import.sync_tushare(root, 'fixture-only', now=monday)
    assert (root / 'current').resolve() == updated != pinned
    assert [params['trade_date'] for api, params in calls if api == 'daily'] == ['20261123']
    assert market.load('daily', pinned)['trade_date'].max() == '2026-11-20'
    frame, meta = market.market_view(updated, now=monday)
    assert meta['freshness'] == 'current' and meta['attributes_date'] == '2026-11-23'
    assert meta['valuation_coverage']['SZ']['with_pe_ttm'] == 0
    assert frame['pe_dynamic'].isna().all()
    assert frame.set_index('symbol').loc['600519', 'market_cap'] == 80
    result = market.apply_screen(frame, [market.Condition(field='pe_ttm', op='<', value=30)])
    assert result['matched'] == 2 and result['missing_inputs'] == 1

    async def exercise():
        async with Client(mcp) as client:
            screened = (await client.call_tool('screen_stocks', {
                'trade_date': '2026-11-23', 'conditions': [{'field': 'pe_ttm', 'op': '<', 'value': 30}],
            })).data
            assert screened['matched'] == 2 and screened['attributes_date'] == '2026-11-23'
            assert screened['valuation_coverage']['SZ']['with_pe_ttm'] == 0

    asyncio.run(exercise())
    _, before_release = market.market_view(updated, now=monday.replace(hour=16))
    assert before_release['freshness'] == 'ahead_of_cutoff'
    earlier, earlier_meta = market.market_view(updated, '2026-11-19', now=monday)
    assert earlier_meta['attributes_date'] == '2026-11-19' and len(earlier) == 3
    attributes = updated / 'attributes.csv'
    attributes.write_text(attributes.read_text().replace(',80.0,', ',79.0,'))
    with pytest.raises(market.MarketUnavailable, match='已变更'):
        market.load('attributes', updated)
    with pytest.raises(market.MarketUnavailable, match='上次行情快照校验失败'):
        market_import.sync_tushare(root, 'fixture-only', now=monday + timedelta(days=1))


def test_sync_long_history_populates_monthly_macd(upstream, tmp_path):
    calls, changes = upstream
    changes['long_history'] = True
    evening = NOW.replace(hour=18)
    start = date(2023, 1, 1)
    root = market_import.sync_tushare(tmp_path / 'managed', 'fixture-only', start, now=evening)
    frame, meta = market.market_view(root, now=evening)
    assert meta['freshness'] == 'current'
    assert frame['month_macd_dea'].notna().all()
    assert frame['month_macd_dea'].eq(0).all()
    calendar_calls = [params for api, params in calls if api == 'trade_cal']
    assert len(calendar_calls) > 4
    assert len({params['trade_date'] for api, params in calls if api == 'daily'}) > 900
