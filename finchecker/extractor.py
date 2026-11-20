"""Rule-based extractor for Chinese financial sentences (regex + rules, no LLM).

Metric terms are matched with a single alternation ordered longest-first, so compound
terms such as 扣非归母净利润 are never mis-read as the shorter 归母净利润 or 净利润.
"""
from __future__ import annotations

import re

from finchecker.models import ClaimKind, FinancialClaim

CODE_RE = re.compile(r'(?<!\d)(\d{6})(?:\.(?:SH|SZ|BJ))?(?!\d)')
PERIOD_RE = re.compile(
    r'(20\d{2})\s*年?\s*(?:第?([一二三四1-4])季度|Q([1-4])|(年报|年度报告|年度|半年报|中报|三季报|一季报))',
    re.IGNORECASE,
)
QUARTER_CN = {'一': 1, '二': 2, '三': 3, '四': 4, '1': 1, '2': 2, '3': 3, '4': 4}
REPORT_KEYWORD = {'半年报': 2, '中报': 2, '三季报': 3, '一季报': 1}

# Surface term -> canonical metric name (canonical names match fetcher.AK_METRIC_ROW keys).
METRIC_ALIASES = {
    '归母净利润': '归属于上市公司股东的净利润',
    '归属于母公司股东的净利润': '归属于上市公司股东的净利润',
    '营收': '营业收入',
    '扣非归母净利润': '扣除非经常性损益后的归属于上市公司股东的净利润',
    '归属于上市公司股东的扣除非经常性损益的净利润': '扣除非经常性损益后的归属于上市公司股东的净利润',
    '扣非净利润': '扣除非经常性损益后的净利润',
    '扣除非经常性损益的净利润': '扣除非经常性损益后的净利润',
    '每股收益': '基本每股收益',
}
CANONICAL_METRICS = [
    '归属于上市公司股东的净利润', '营业总收入', '营业收入', '净利润', '营业利润', '营业成本', '毛利率',
    '基本每股收益', '稀释每股收益', '总资产', '净资产',
]
# Longest first: at any position the longest matching term wins.
METRIC_TERMS = sorted(set(METRIC_ALIASES) | set(CANONICAL_METRICS), key=lambda t: (-len(t), t))
METRIC_RE = re.compile('|'.join(re.escape(t) for t in METRIC_TERMS))

# Amounts: plain or with thousands separators, e.g. 130亿元, 1,300.5亿元, 0.85元, 1.2万亿元.
VALUE_RE = re.compile(
    r'(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)\s*(万亿|亿元|亿|万元|万|元)'
)
UNIT = {'万亿': 1e12, '亿元': 1e8, '亿': 1e8, '万元': 1e4, '万': 1e4, '元': 1.0}
GROWTH_RE = re.compile(
    r'(同比|环比)\s*(增长|增加|上涨|上升|提升|下降|减少|下滑|降低)?\s*(-?\d+(?:\.\d+)?)\s*%'
)
NEG_WORDS = {'下降', '减少', '下滑', '降低'}
SPLIT_RE = re.compile(r'[。；;\n]+')


def _period(m: re.Match) -> str:
    year = m.group(1)
    if m.group(2):
        return f'{year}Q{QUARTER_CN[m.group(2)]}'
    if m.group(3):
        return f'{year}Q{m.group(3)}'
    kw = m.group(4)
    if kw in REPORT_KEYWORD:
        return f'{year}Q{REPORT_KEYWORD[kw]}'
    return f'{year}FY'


def _metric(sentence: str) -> str | None:
    metrics = {METRIC_ALIASES.get(term, term) for term in METRIC_RE.findall(sentence)}
    return next(iter(metrics)) if len(metrics) == 1 else None


def _amount(number: str, unit: str) -> float:
    return float(number.replace(',', '')) * UNIT[unit]


def extract_claims(text: str) -> list[FinancialClaim]:
    """Extract claims sentence by sentence. A sentence without its own stock code
    inherits the most recent code seen in the text."""
    claims: list[FinancialClaim] = []
    last_code: str | None = None
    for sentence in SPLIT_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        codes = set(CODE_RE.findall(sentence))
        if codes:
            last_code = next(iter(codes)) if len(codes) == 1 else None
        code = last_code
        periods = {_period(match) for match in PERIOD_RE.finditer(sentence)}
        period = next(iter(periods)) if len(periods) == 1 else None
        metric = _metric(sentence)
        amounts = list(VALUE_RE.finditer(sentence))
        growths = list(GROWTH_RE.finditer(sentence))
        if not amounts and not growths:
            continue
        base = {'raw_text': sentence, 'stock_code': code, 'period': period, 'metric': metric}
        for vm in amounts:
            claims.append(FinancialClaim(
                kind=ClaimKind.VALUE,
                declared_value=_amount(vm.group(1), vm.group(2)) if len(amounts) == 1 else None,
                **base,
            ))
        for gm in growths:
            kind = ClaimKind.YOY if gm.group(1) == '同比' else ClaimKind.QOQ
            pct = float(gm.group(3))
            if gm.group(2) in NEG_WORDS:
                pct = -abs(pct)
            claims.append(FinancialClaim(kind=kind, declared_growth_pct=pct, **base))
    return claims
