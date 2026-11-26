"""Run the existing two-company annual-report sample without network access."""
from pathlib import Path

from finchecker.extractor import extract_claims
from finchecker.fetcher import DisclosureSource
from finchecker.models import ReportStatus
from finchecker.verifier import verify_claims


def main() -> int:
    sample = Path(__file__).parent / 'examples' / '2025-disclosures.txt'
    text = sample.read_text(encoding='utf-8')
    print('两家公司 2025 年报快照演示（离线，不请求最新数据）')
    print('\n输入：examples/2025-disclosures.txt')
    print(text.strip())
    report = verify_claims(extract_claims(text), DisclosureSource())
    print('\n来源：disclosure；仓库已有的公开公告人工摘录，非模拟数据')
    for result in report.results:
        claim = result.claim
        expected = (f'{result.expected_value:,.2f} 元' if result.expected_value is not None
                    else f'{result.expected_growth_pct:+.2f}%'
                    if result.expected_growth_pct is not None else '无对应数据')
        print(f'{claim.stock_code} {claim.period} {claim.metric} '
              f'{claim.kind.value}: {result.status.value}；来源数值 {expected}；{result.message}')
    evidence_by_url = {r.evidence.url: r.evidence for r in report.results if r.evidence}
    for evidence in evidence_by_url.values():
        print(f'依据：{evidence.title}；发布日期 {evidence.published_on}；'
              f'PDF 第 {evidence.pdf_page} 页\n{evidence.url}')
    print(f'\n结果：{report.status.value}；共 {report.total} 条，'
          f'通过 {report.passed}，失败 {report.failed}，无法核实 {report.unverifiable}')
    print('局限：仅覆盖 600519、600900 的 2025FY 营业收入、归母净利润及同比；'
          '快照不会自动更新。只核查已提取断言，不代表全文正确，不构成投资建议。')
    return 0 if report.status == ReportStatus.VERIFIED else 1


if __name__ == '__main__':
    raise SystemExit(main())
