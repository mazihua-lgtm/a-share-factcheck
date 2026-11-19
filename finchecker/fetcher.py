"""Ground-truth data source abstraction.

MockSource: fixed fixtures for tests and offline demos (NOT real market data).
AkshareSource: real A-share data via akshare (https://github.com/akfamily/akshare), with a
    SQLite TTL cache, bounded retries with exponential backoff, and stale-cache fallback.
    Field names must be re-verified against live akshare output before relying on them.

Cache location: $FINCHECKER_CACHE_DIR/akshare_cache.sqlite (default ~/.cache/finchecker).
Cache TTL in seconds: $FINCHECKER_CACHE_TTL (default 86400).
"""
from __future__ import annotations

import importlib
import logging
import os
import pickle
import sqlite3
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Optional

from pydantic import BaseModel

log = logging.getLogger(__name__)

DEFAULT_TTL = 24 * 3600


class GroundTruth(BaseModel):
    stock_code: str
    name: str
    period: str
    metric: str
    value: float  # yuan
    prior_year_value: Optional[float] = None
    prior_quarter_value: Optional[float] = None


class DataSourceError(RuntimeError):
    """Raised when a remote data source fails after all retries."""


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


# Period suffixes match extractor output: 2025Q3, 2025FY.
PERIOD_TO_MMDD = {'Q1': '0331', 'Q2': '0630', 'Q3': '0930', 'FY': '1231'}
# (year offset, mmdd) of the previous quarter-end
PREV_QUARTER = {'Q1': (-1, '1231'), 'Q2': (0, '0331'), 'Q3': (0, '0630'), 'FY': (0, '0930')}
# canonical metric -> row label in akshare stock_financial_abstract output
AK_METRIC_ROW = {
    '营业收入': '营业总收入',
    '归属于上市公司股东的净利润': '归母净利润',
    '扣除非经常性损益后的净利润': '扣非净利润',
    '扣除非经常性损益后的归属于上市公司股东的净利润': '扣非归母净利润',
}


def default_cache_path() -> Path:
    base = os.environ.get('FINCHECKER_CACHE_DIR') or (Path.home() / '.cache' / 'finchecker')
    return Path(base) / 'akshare_cache.sqlite'


def default_ttl() -> float:
    raw = os.environ.get('FINCHECKER_CACHE_TTL')
    try:
        return float(raw) if raw else float(DEFAULT_TTL)
    except ValueError:
        log.warning('invalid FINCHECKER_CACHE_TTL=%r, using default', raw)
        return float(DEFAULT_TTL)


class _SqliteCache:
    """Key-value cache with per-entry timestamps. Cache errors never break a query."""

    def __init__(self, path: Path, ttl: float):
        self.ttl = ttl
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), timeout=10)
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, ts REAL NOT NULL, payload BLOB NOT NULL)'
        )
        self._conn.commit()

    def get(self, key: str, allow_stale: bool = False) -> Any:
        try:
            row = self._conn.execute('SELECT ts, payload FROM kv WHERE k = ?', (key,)).fetchone()
        except sqlite3.Error as e:
            log.warning('cache read failed for %s: %s', key, e)
            return None
        if row is None:
            return None
        ts, payload = row
        if not allow_stale and time.time() - ts > self.ttl:
            return None
        try:
            return pickle.loads(payload)
        except Exception as e:
            log.warning('corrupt cache entry %s: %s', key, e)
            return None

    def put(self, key: str, value: Any) -> None:
        try:
            self._conn.execute(
                'INSERT OR REPLACE INTO kv (k, ts, payload) VALUES (?, ?, ?)',
                (key, time.time(), pickle.dumps(value)),
            )
            self._conn.commit()
        except Exception as e:
            log.warning('cache write failed for %s: %s', key, e)


class AkshareSource(GroundTruthSource):
    def __init__(
        self,
        ak_module: Any = None,
        cache: bool = True,
        cache_path: Optional[Path] = None,
        ttl: Optional[float] = None,
        retries: int = 3,
        backoff: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._ak = ak_module if ak_module is not None else importlib.import_module('akshare')
        self._retries = max(1, retries)
        self._backoff = backoff
        self._sleep = sleep
        self._cache: Optional[_SqliteCache] = None
        if cache:
            try:
                self._cache = _SqliteCache(
                    cache_path or default_cache_path(), default_ttl() if ttl is None else ttl)
            except (OSError, sqlite3.Error) as e:
                log.warning('disk cache disabled: %s', e)
        self._names: Optional[dict[str, str]] = None
        self._abstracts: dict[str, Any] = {}

    def _call(self, label: str, fn: Callable[[], Any]) -> Any:
        last: Optional[Exception] = None
        for attempt in range(1, self._retries + 1):
            try:
                return fn()
            except Exception as e:  # akshare surfaces many exception types
                last = e
                log.warning('%s attempt %d/%d failed: %s', label, attempt, self._retries, e)
                if attempt < self._retries:
                    self._sleep(self._backoff * 2 ** (attempt - 1))
        raise DataSourceError(f'{label} failed after {self._retries} attempts: {last}') from last

    def _cached(self, key: str, label: str, fn: Callable[[], Any]) -> Any:
        if self._cache is not None:
            hit = self._cache.get(key)
            if hit is not None:
                return hit
        try:
            value = self._call(label, fn)
        except DataSourceError:
            if self._cache is not None:
                stale = self._cache.get(key, allow_stale=True)
                if stale is not None:
                    log.warning('using stale cache for %s', label)
                    return stale
            raise
        if self._cache is not None:
            self._cache.put(key, value)
        return value

    def _fetch_names(self) -> dict[str, str]:
        df = self._ak.stock_info_a_code_name()
        if df is None or df.empty:
            raise ValueError('empty stock list')
        return dict(zip(df['code'].astype(str).str.zfill(6), df['name'].astype(str)))

    def _name_map(self) -> dict[str, str]:
        if self._names is None:
            self._names = self._cached('stock_list', 'stock_info_a_code_name', self._fetch_names)
        return self._names

    def _abstract(self, code: str) -> Any:
        if code not in self._abstracts:
            def fetch() -> Any:
                df = self._ak.stock_financial_abstract(symbol=code)
                if df is None or df.empty:
                    raise ValueError(f'empty financial abstract for {code}')
                return df
            self._abstracts[code] = self._cached(
                f'abstract:{code}', f'stock_financial_abstract({code})', fetch)
        return self._abstracts[code]

    def code_exists(self, code: str) -> bool:
        try:
            return code in self._name_map()
        except DataSourceError as e:
            # Cannot confirm either way; returning True lets get() report 'unverifiable'
            # instead of a false 'wrong_code' failure.
            log.warning('cannot check code %s: %s', code, e)
            return True

    def get(self, code, period, metric):
        try:
            names = self._name_map()
        except DataSourceError:
            return None
        if code not in names:
            return None
        year, q = period[:4], period[4:]
        mmdd = PERIOD_TO_MMDD.get(q)
        if mmdd is None:
            return None
        try:
            df = self._abstract(code)
        except DataSourceError:
            return None
        rows = df[df['指标'] == AK_METRIC_ROW.get(metric, metric)]
        if rows.empty:
            return None
        row = rows.iloc[0]

        def col(y: str, md: str) -> Optional[float]:
            v = row.get(f'{y}{md}')
            if v is None:
                return None
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return None if f != f else f  # f != f filters NaN

        value = col(year, mmdd)
        if value is None:
            return None
        off, pmd = PREV_QUARTER[q]
        return GroundTruth(
            stock_code=code,
            name=names[code],
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
