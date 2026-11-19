from finchecker.extractor import extract_claims
from finchecker.fetcher import MockSource
from finchecker.models import ClaimKind, Status
from finchecker.verifier import check_ratio, is_scale_error, recompute_growth, verify_claim, verify_claims

SRC = MockSource()


def _one(text, kind):
    claims = [c for c in extract_claims(text) if c.kind == kind]
    assert claims, text
    return verify_claim(claims[0], SRC)


def test_correct_yoy_passes():
    r = _one('600519 2025年三季报：营业收入同比增长8.3%', ClaimKind.YOY)
    assert r.status == Status.PASS


def test_inverted_yoy_is_caught():
    # true YoY is +8.33%; claim says it fell 8.3%
    r = _one('600519 2025年三季报：营业收入同比下降8.3%', ClaimKind.YOY)
    assert r.status == Status.FAIL and r.issue_type == 'inverted_growth'


def test_inverted_qoq_is_caught():
    # true QoQ is +4.0%
    r = _one('600519 2025年三季报：营业收入环比下降4.0%', ClaimKind.QOQ)
    assert r.status == Status.FAIL and r.issue_type == 'inverted_growth'


def test_correct_qoq_passes():
    r = _one('600519 2025年三季报：营业收入环比增长4.0%', ClaimKind.QOQ)
    assert r.status == Status.PASS


def test_off_magnitude_is_caught():
    # 1300万元 is 1000x too small versus 130亿元
    r = _one('600519 2025年三季报：营业收入1300万元', ClaimKind.VALUE)
    assert r.status == Status.FAIL and r.issue_type == 'magnitude'


def test_correct_value_passes():
    r = _one('600519 2025年三季报：营业收入130亿元', ClaimKind.VALUE)
    assert r.status == Status.PASS


def test_plain_value_mismatch():
    r = _one('600519 2025年三季报：营业收入150亿元', ClaimKind.VALUE)
    assert r.status == Status.FAIL and r.issue_type == 'value_mismatch'


def test_wrong_stock_code_is_caught():
    r = _one('000001 2025年三季报：营业收入130亿元', ClaimKind.VALUE)
    assert r.status == Status.FAIL and r.issue_type == 'wrong_code'


def test_missing_ground_truth_is_unverifiable():
    r = _one('600519 2024年报：营业收入100亿元', ClaimKind.VALUE)
    assert r.status == Status.UNVERIFIABLE


def test_report_counts():
    text = ('600519 2025年三季报：营业收入同比下降8.3%。'
            '600519 2025年三季报：营业收入同比增长8.3%。')
    report = verify_claims(extract_claims(text), SRC)
    assert (report.total, report.passed, report.failed) == (2, 1, 1)


def test_helpers():
    assert round(recompute_growth(1.30e10, 1.20e10), 2) == 8.33
    assert is_scale_error(1.3e7, 1.3e10)
    assert not is_scale_error(1.3e10, 1.3e10)
    ok, expected = check_ratio(30, 100, 30.0)
    assert ok and expected == 30.0
