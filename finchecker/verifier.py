"""Hard-rule verifier: deterministic math and tolerance checks, no LLM involved."""
from __future__ import annotations

from typing import Optional

from finchecker.fetcher import GroundTruthSource
from finchecker.models import (
    ClaimKind, DiscrepancyReport, FinancialClaim, ReportStatus, Status, VerificationResult,
)

REL_TOL = 0.005  # amounts: 0.5% relative tolerance
PCT_TOL = 0.5  # growth rates: 0.5 percentage point tolerance
SCALE_FACTORS = (10, 100, 1e3, 1e4, 1e8, 1e12)  # common unit / magnitude slips


def recompute_growth(current: Optional[float], base: Optional[float]) -> Optional[float]:
    if current is None or base is None or base == 0:
        return None
    return (current - base) / abs(base) * 100


def is_scale_error(declared: Optional[float], actual: Optional[float]) -> bool:
    if not declared or not actual:
        return False
    ratio = abs(declared / actual)
    for f in SCALE_FACTORS:
        for cand in (f, 1 / f):
            if abs(ratio / cand - 1) < 0.01:
                return True
    return False


def check_ratio(numerator: Optional[float], denominator: Optional[float],
                declared_pct: float, tol: float = PCT_TOL) -> tuple[bool, Optional[float]]:
    """Formula check, e.g. gross margin = gross profit / revenue * 100."""
    if numerator is None or not denominator:
        return False, None
    expected = numerator / denominator * 100
    return abs(expected - declared_pct) <= tol, expected


def _result(claim, status, issue=None, message='', exp_value=None, exp_growth=None):
    return VerificationResult(
        claim=claim, status=status, issue_type=issue, message=message,
        expected_value=exp_value, expected_growth_pct=exp_growth,
    )


def _check_value(claim: FinancialClaim, actual: float) -> VerificationResult:
    d = claim.declared_value
    if d is None:
        return _result(claim, Status.UNVERIFIABLE, 'missing_value', '未识别到声明金额', exp_value=actual)
    if abs(d - actual) <= REL_TOL * abs(actual):
        return _result(claim, Status.PASS, None, '金额与真值一致', exp_value=actual)
    if is_scale_error(d, actual):
        return _result(
            claim, Status.FAIL, 'magnitude',
            f'声明 {d:,.0f} 元 与真值 {actual:,.0f} 元 相差约 {d / actual:.3g} 倍, 疑似单位或数量级错误',
            exp_value=actual,
        )
    return _result(
        claim, Status.FAIL, 'value_mismatch',
        f'声明 {d:,.0f} 元 与真值 {actual:,.0f} 元 不一致',
        exp_value=actual,
    )


def _check_growth(claim: FinancialClaim, expected: float) -> VerificationResult:
    d = claim.declared_growth_pct
    label = '同比' if claim.kind == ClaimKind.YOY else '环比'
    if d is None:
        return _result(claim, Status.UNVERIFIABLE, 'missing_value', '未识别到声明增速', exp_growth=expected)
    if d * expected >= 0 and abs(d - expected) <= PCT_TOL:
        return _result(claim, Status.PASS, None, f'{label}增速一致', exp_growth=expected)
    if abs(d + expected) <= PCT_TOL and abs(expected) > PCT_TOL:
        return _result(
            claim, Status.FAIL, 'inverted_growth',
            f'声明{label} {d:+.2f}% 与真值 {expected:+.2f}% 符号相反, 疑似涨跌方向颠倒',
            exp_growth=expected,
        )
    return _result(
        claim, Status.FAIL, 'growth_mismatch',
        f'声明{label} {d:+.2f}% 与真值 {expected:+.2f}% 不一致',
        exp_growth=expected,
    )


def verify_claim(claim: FinancialClaim, source: GroundTruthSource) -> VerificationResult:
    if not claim.stock_code:
        return _result(claim, Status.UNVERIFIABLE, 'missing_code', '未识别到股票代码')
    if source.code_exists(claim.stock_code) is False:
        return _result(claim, Status.FAIL, 'wrong_code',
                       f'股票代码 {claim.stock_code} 不存在于A股列表, 疑似代码张冠李戴')
    if not claim.period or not claim.metric:
        return _result(claim, Status.UNVERIFIABLE, 'missing_context', '报告期或指标缺失或不唯一')
    if claim.kind == ClaimKind.QOQ and not source.simulated:
        return _result(claim, Status.UNVERIFIABLE, 'unsupported_qoq', '缺少可确认的单季度口径，不用累计期末值重算环比')
    if not source.simulated and not claim.period.endswith('FY'):
        return _result(claim, Status.UNVERIFIABLE, 'unsupported_period', '当前仅核查年报，未区分单季度与年初至期末累计口径')
    truth = source.get(claim.stock_code, claim.period, claim.metric)
    if truth is None:
        return _result(claim, Status.UNVERIFIABLE, 'no_ground_truth', '数据源中无对应记录')
    if claim.kind == ClaimKind.VALUE:
        return _check_value(claim, truth.value).model_copy(update={'evidence': truth.evidence})
    base = truth.prior_year_value if claim.kind == ClaimKind.YOY else truth.prior_quarter_value
    expected = recompute_growth(truth.value, base)
    if expected is None:
        return _result(claim, Status.UNVERIFIABLE, 'no_base_period', '缺少对比期数据, 无法重算增速')
    result = _check_growth(claim, expected)
    if truth.evidence is not None and claim.kind == ClaimKind.YOY:
        result.evidence = truth.evidence.model_copy(update={
            'comparison_period': f'{int(truth.period[:4]) - 1}{truth.period[4:]}',
            'comparison_value_yuan': base,
        })
    return result


def verify_claims(claims: list[FinancialClaim], source: GroundTruthSource) -> DiscrepancyReport:
    results = [verify_claim(c, source) for c in claims]
    passed = sum(r.status == Status.PASS for r in results)
    failed = sum(r.status == Status.FAIL for r in results)
    if source.simulated:
        status = ReportStatus.SIMULATION
    elif not results:
        status = ReportStatus.NO_CLAIMS
    elif failed:
        status = ReportStatus.MISMATCH
    elif not passed:
        status = ReportStatus.UNVERIFIABLE
    elif passed < len(results):
        status = ReportStatus.INCOMPLETE
    else:
        status = ReportStatus.VERIFIED
    return DiscrepancyReport(
        source=source.name, simulated=source.simulated, status=status,
        total=len(results), passed=passed, failed=failed,
        unverifiable=len(results) - passed - failed, results=results,
    )
