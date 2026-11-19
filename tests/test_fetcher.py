import pandas as pd
import pytest

from finchecker.fetcher import AkshareSource, DataSourceError


class FakeAk:
    """Stands in for the akshare module; counts calls and can simulate outages."""

    def __init__(self):
        self.calls = {'names': 0, 'abstract': 0}
        self.fail_names = 0  # fail this many times, then succeed
        self.broken = False  # always fail

    def stock_info_a_code_name(self):
        self.calls['names'] += 1
        if self.broken or self.calls['names'] <= self.fail_names:
            raise ConnectionError('simulated outage')
        return pd.DataFrame({'code': ['600519'], 'name': ['贵州茅台']})

    def stock_financial_abstract(self, symbol):
        self.calls['abstract'] += 1
        if self.broken:
            raise ConnectionError('simulated outage')
        return pd.DataFrame({
            '指标': ['营业总收入'],
            '20250930': [1.3e10],
            '20240930': [1.2e10],
            '20250630': [8.7e9],
        })


def _source(ak, tmp_path, **kw):
    kw.setdefault('sleep', lambda _s: None)
    kw.setdefault('backoff', 0.0)
    return AkshareSource(ak_module=ak, cache_path=tmp_path / 'c.sqlite', **kw)


def test_get_returns_truth_with_prior_periods(tmp_path):
    truth = _source(FakeAk(), tmp_path).get('600519', '2025Q3', '营业收入')
    assert truth is not None
    assert truth.value == pytest.approx(1.3e10)
    assert truth.prior_year_value == pytest.approx(1.2e10)
    assert truth.prior_quarter_value == pytest.approx(8.7e9)


def test_sqlite_cache_avoids_repeat_network_calls(tmp_path):
    ak = FakeAk()
    _source(ak, tmp_path).get('600519', '2025Q3', '营业收入')
    # a fresh instance has no in-memory memo, so only the sqlite cache can serve it
    _source(ak, tmp_path).get('600519', '2025Q3', '营业收入')
    assert ak.calls == {'names': 1, 'abstract': 1}


def test_retry_then_success(tmp_path):
    ak = FakeAk()
    ak.fail_names = 2
    src = _source(ak, tmp_path, retries=3)
    assert src.code_exists('600519') is True
    assert ak.calls['names'] == 3


def test_total_outage_degrades_to_none_not_exception(tmp_path):
    ak = FakeAk()
    ak.broken = True
    src = _source(ak, tmp_path, retries=2)
    assert src.get('600519', '2025Q3', '营业收入') is None
    # cannot confirm the code either way, so it must not be reported as wrong_code
    assert src.code_exists('600519') is True


def test_stale_cache_is_used_when_source_is_down(tmp_path):
    ak = FakeAk()
    # ttl=-1 makes every entry already expired for normal reads
    assert _source(ak, tmp_path, ttl=-1).get('600519', '2025Q3', '营业收入') is not None
    ak.broken = True
    truth = _source(ak, tmp_path, ttl=-1, retries=1).get('600519', '2025Q3', '营业收入')
    assert truth is not None and truth.value == pytest.approx(1.3e10)


def test_fetch_error_type_is_datasourceerror(tmp_path):
    ak = FakeAk()
    ak.broken = True
    src = _source(ak, tmp_path, retries=1, cache=False)
    with pytest.raises(DataSourceError):
        src._call('probe', lambda: ak.stock_info_a_code_name())
