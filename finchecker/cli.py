"""Rich CLI: run fact-checking on sample text or a file and print a table."""
from __future__ import annotations

import argparse

from rich.console import Console
from rich.table import Table

from finchecker.extractor import extract_claims
from finchecker.fetcher import get_source
from finchecker.models import ClaimKind, Status
from finchecker.verifier import verify_claims

SAMPLE = (
    '600519 2025年三季报显示，营业收入130亿元，同比下降8.3%，环比增长4.0%。\n'
    '000001 2025年三季报：营业收入130亿元，同比增长8.3%。\n'
    '600519 2025年三季报：营业收入1300万元。'
)
STYLE = {Status.PASS: 'green', Status.FAIL: 'red', Status.UNVERIFIABLE: 'yellow'}
KIND = {ClaimKind.VALUE: '金额', ClaimKind.YOY: '同比', ClaimKind.QOQ: '环比'}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog='finchecker', description='A股财报/研报断言核查')
    p.add_argument('--text', help='待核查文本')
    p.add_argument('--file', help='从文件读取待核查文本')
    p.add_argument('--source', default='mock', choices=['mock', 'akshare'])
    p.add_argument('--json', action='store_true', help='输出 JSON')
    args = p.parse_args(argv)

    if args.file:
        with open(args.file, encoding='utf-8') as f:
            text = f.read()
    else:
        text = args.text or SAMPLE

    report = verify_claims(extract_claims(text), get_source(args.source))
    if args.json:
        print(report.model_dump_json(indent=2))
        return 0 if report.failed == 0 else 1

    console = Console()
    table = Table(title='A股断言核查结果')
    for col in ('代码', '报告期', '指标', '类型', '声明', '真值/期望', '结论', '说明'):
        table.add_column(col)
    for r in report.results:
        c = r.claim
        declared = (f'{c.declared_value:,.0f}元' if c.kind == ClaimKind.VALUE
                    else f'{c.declared_growth_pct:+.2f}%')
        if r.expected_value is not None:
            expected = f'{r.expected_value:,.0f}元'
        elif r.expected_growth_pct is not None:
            expected = f'{r.expected_growth_pct:+.2f}%'
        else:
            expected = '-'
        table.add_row(
            c.stock_code or '-', c.period or '-', c.metric or '-', KIND[c.kind], declared,
            expected, f'[{STYLE[r.status]}]{r.status.value}[/]',
            r.message + (f' ({r.issue_type})' if r.issue_type else ''),
        )
    console.print(table)
    console.print(
        f'共 {report.total} 条: 通过 {report.passed}, 失败 {report.failed}, 无法核实 {report.unverifiable}'
    )
    return 0 if report.failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
