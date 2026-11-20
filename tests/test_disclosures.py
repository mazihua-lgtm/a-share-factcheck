from pathlib import Path

import pytest

from finchecker.extractor import extract_claims
from finchecker.fetcher import DisclosureSource, MockSource
from finchecker.models import ClaimKind, ReportStatus, Status
from finchecker.verifier import verify_claims


def test_public_filings_match_the_published_percentages_and_amounts():
    text = (Path(__file__).parents[1] / 'examples' / '2025-disclosures.txt').read_text(encoding='utf-8')
    report = verify_claims(extract_claims(text), DisclosureSource())
    assert report.status == ReportStatus.VERIFIED
    assert report.total == report.passed == 8
    expected = {
        ('600519', '营业收入'): (168_838_102_514.79, -1.21, 4),
        ('600519', '归属于上市公司股东的净利润'): (82_320_067_101.68, -4.53, 4),
        ('600900', '营业收入'): (86_241_940_222.20, 2.07, 3),
        ('600900', '归属于上市公司股东的净利润'): (34_502_809_176.39, 6.17, 3),
    }
    for result in report.results:
        value, growth, page = expected[result.claim.stock_code, result.claim.metric]
        assert result.status == Status.PASS
        evidence = result.evidence
        assert evidence.period == '2025FY' and evidence.pdf_page == page
        assert evidence.url.startswith('https://static.cninfo.com.cn/')
        assert evidence.value_yuan == value
        if result.claim.kind == ClaimKind.YOY:
            assert round(result.expected_growth_pct, 2) == growth
            assert evidence.comparison_period == '2024FY'
            assert evidence.comparison_value_yuan is not None
        else:
            assert result.expected_value == value


@pytest.mark.parametrize('text', [
    '600519 2025Q4：营业收入1688.38亿元。',
    '600519 2025年报：营业收入环比增长2%。',
    '600519 2025年报：营业总收入1688.38亿元。',
    '600519 2025年报：营业收入1688.38亿元，归母净利润823.20亿元。',
    '600519 2025年报：营业收入1688.38亿元，上年营业收入1708.99亿元。',
    '600519、600900 2025年报：营业收入1688.38亿元。',
])
def test_unsupported_or_ambiguous_claims_are_not_verified(text):
    report = verify_claims(extract_claims(text), DisclosureSource())
    assert report.status == ReportStatus.UNVERIFIABLE
    assert report.total == report.unverifiable


def test_simulation_cannot_be_reported_as_real_verification():
    claims = extract_claims('600519 2025年三季报：营业收入130亿元。')
    report = verify_claims(claims, MockSource())
    assert report.passed == 1 and report.simulated
    assert report.status == ReportStatus.SIMULATION
