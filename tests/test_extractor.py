import pytest

from finchecker.extractor import extract_claims
from finchecker.models import ClaimKind


def _claims(text, kind=None):
    return [c for c in extract_claims(text) if kind is None or c.kind == kind]


def test_deducted_net_profit_is_not_mistaken_for_net_profit():
    (claim,) = _claims('600519 2025年三季报：扣非净利润同比增长12.0%。', ClaimKind.YOY)
    assert claim.metric == '扣除非经常性损益后的净利润'
    assert claim.declared_growth_pct == 12.0


def test_plain_net_profit_keeps_its_metric():
    (claim,) = _claims('600519 2025年三季报：净利润同比增长9.1%。', ClaimKind.YOY)
    assert claim.metric == '净利润'


def test_deducted_parent_net_profit_longest_match():
    (claim,) = _claims('600519 2025年三季报：扣非归母净利润60亿元。', ClaimKind.VALUE)
    assert claim.metric == '扣除非经常性损益后的归属于上市公司股东的净利润'
    assert claim.declared_value == pytest.approx(6.0e9)


def test_parent_net_profit_alias():
    (claim,) = _claims('600519 2025年三季报：归母净利润同比增长9.1%。', ClaimKind.YOY)
    assert claim.metric == '归属于上市公司股东的净利润'


def test_basic_eps_value_and_growth():
    claims = _claims('600519 2025年三季报：基本每股收益38.5元，同比增长6.2%。')
    value = [c for c in claims if c.kind == ClaimKind.VALUE]
    yoy = [c for c in claims if c.kind == ClaimKind.YOY]
    assert len(value) == 1 and len(yoy) == 1
    assert value[0].metric == '基本每股收益'
    assert value[0].declared_value == pytest.approx(38.5)
    assert yoy[0].declared_growth_pct == 6.2


def test_eps_alias_and_diluted_eps_stay_separate():
    (basic,) = _claims('600519 2025年三季报：每股收益38.5元', ClaimKind.VALUE)
    (diluted,) = _claims('600519 2025年三季报：稀释每股收益38.4元', ClaimKind.VALUE)
    assert basic.metric == '基本每股收益'
    assert diluted.metric == '稀释每股收益'


def test_shishi_yingshou_format():
    (claim,) = _claims('600519 2025年三季报：实现营收130亿元。', ClaimKind.VALUE)
    assert claim.metric == '营业收入'
    assert claim.period == '2025Q3'
    assert claim.declared_value == pytest.approx(1.3e10)


def test_shishi_yingshou_with_thousands_separator():
    (claim,) = _claims('600519 2025年三季报：实现营收1,300.5亿元。', ClaimKind.VALUE)
    assert claim.declared_value == pytest.approx(1.3005e11)


def test_lu_de_net_profit_format():
    (claim,) = _claims('600519 2025年三季报：录得净利润60亿元。', ClaimKind.VALUE)
    assert claim.metric == '净利润'
    assert claim.declared_value == pytest.approx(6.0e9)


def test_cost_and_margin_metrics_recognised():
    (cost,) = _claims('600519 2025年三季报：营业成本同比增长3.2%。', ClaimKind.YOY)
    (margin,) = _claims('600519 2025年三季报：毛利率同比下降1.5%。', ClaimKind.YOY)
    assert cost.metric == '营业成本'
    assert margin.metric == '毛利率'
    assert margin.declared_growth_pct == -1.5


def test_revenue_short_alias():
    (claim,) = _claims('600519 2025年三季报：营收同比增长8.3%。', ClaimKind.YOY)
    assert claim.metric == '营业收入'
