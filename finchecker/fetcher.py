"""Ground-truth data source abstraction.

MockSource: fixed fixtures for tests and offline demos (NOT real market data).
AkshareSource: real A-share data via akshare (https://github.com/akfamily/akshare).
  Field names follow akshare's current output and should be re-verified against live data.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel


class GroundTruth(BaseModel):
    stock_code: str
    name: str
    period: str
    metric: str
    value: float  # yuan
    prior_year_value: Optional[float] = None
    prior_quarter_value: Optional[float] = None


class GroundTruthSource(ABC):
    @abstractmethod
    def code_exists(self, code: str) -> bool: ...

    @abstractmethod
    def get(self, code: str, period: str, metric: str) -> Optional[GroundTruth]: ...


MOCK_NAMES = {'600519': '贵州茅台'}
MOCK_DATA = {
    ('600519', '2025Q3', '营业收入'): dict(
        value=1.30e10, prior_year_value=1.20e10, prior_quarter_value=1.25e10),
    ('600519', '2025Q3', '归属于上市公司股东的净利润'): dict(
        value=6.0e9, prior_year_value=5.5e9, prior_quarter_value=5.8e9),
}


class MockSource(GroundTruthSource):
    def code_exists(self, code: str) -> bool:
        return code in MOCK_NAMES

    def get(self, code, period, metric):
        row = MOCK_DATA.get((code, period, metric))
        if row is None:
            return None
        return GroundTruth(stock_code=code, name=MOCK_NAMES[code], period=period, metric=metric, **row)


PERIOD_TO_MMDD = {'1Q': '0331', '2Q': '0630', '3Q': '0930', 'FY': '1231'}
# (year offset, mmdd) of the previous quarter-end
PREV_QUARTER = {'1Q': (-1, '1231'), '2Q': (0, '0331'), '3Q': (0, '0630'), 'FY': (0, '0930')}
AK_METRIC_ROW = {'营业收入': '营业总收入', '归属于上市公司股东的净利润': '归母净利润'}


class AkshareSource(GroundTruthSource):
    def __init__(self):
        import akshare as ak  # lazy import
        self._ak = ak
        self._names: dict[str, str] | None = None
        self._cache: dict[str, object] = {}

    def _name_map(self) -> dict[str, str]:
        if self._names is None:
            df = self._ak.stock_info_a_code_name()
            self._names = dict(zip(df['code'].astype(str).str.zfill(6), df['name']))
        return self._names

    def code_exists(self, code: str) -> bool:
        return code in self._name_map()

    def get(self, code, period, metric):
        if not self.code_exists(code):
            return None
        year, q = period[:4], period[4:]
        mmdd = PERIOD_TO_MMDD.get(q)
        if mmdd is None:
            return None
        if code not in self._cache:
            self._cache[code] = self._ak.stock_financial_abstract(symbol=code)
        df = self._cache[code]
        rows = df[df['指标'] == AK_METRIC_ROW.get(metric, metric)]
        if rows.empty:
            return None
        row = rows.iloc[0]

        def col(y: str, md: str) -> Optional[float]:
            v = row.get(f'{y}{md}')
            return float(v) if v is not None and v == v else None  # v == v filters NaN

        value = col(year, mmdd)
        if value is None:
            return None
        off, pmd = PREV_QUARTER[q]
        return GroundTruth(
            stock_code=code,
            name=self._name_map()[code],
            period=period,
            metric=metric,
            value=value,
            prior_year_value=col(str(int(year) - 1), mmdd),
            prior_quarter_value=col(str(int(year) + off), pmd),
        )


def get_source(name: str) -> GroundTruthSource:
    if name == 'akshare':
        return AkshareSource()
    if name == 'mock':
        return MockSource()
    raise ValueError(f'unknown source: {name}')
