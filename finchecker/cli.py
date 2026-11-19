"""Rich CLI: fact-check text (--text / --file), or run the offline demo with `finchecker demo`."""
from __future__ import annotations

import argparse
import sys

from rich.console import Console
from rich.table import Table

from finchecker.extractor import extract_claims
from finchecker.fetcher import get_source
from finchecker.models import ClaimKind, DiscrepancyReport, Status
from finchecker.verifier import verify_claims

SAMPLE = (
    '600519 2025年三季报显示，营业收入130亿元，同比下降8.3%，环比增长4.0%。\n'
    '000001 2025年三季报：营业收入130亿元，同比增长8.3%。\n'
    '600519 2025年三季报：营业收入1300万元。'
)

# Realistic research-note sentences for the offline demo. They are checked against MockSource,
# whose numbers are fictional fixtures, so the outcomes show how the tool behaves.
DEMO_CASES: list[tuple[str, str]] = [
    ('正确的营收表述 (应通过)',
     '600519 2025年三季报显示，实现营收130亿元，同比增长8.3%，环比增长4.0%。'),
    ('同比涨跌方向颠倒 (应失败)',
     '600519 2025年三季报：营业收入同比下降8.3%。'),
    ('单位数量级错误 (应失败)',
     '600519 2025年三季报：实现营业收入1300万元。'),
    ('归母净利润表述 (应通过)',
     '600519 2025年三季报：录得归母净利润60亿元。'),
    ('扣非净利润 (不在模拟数据中, 应无法核实)',
     '600519 2025年三季报：扣非净利润同比增长12.0%。'),
    ('基本每股收益 (不在模拟数据中, 应无法核实)',
     '600519 2025年三季报：基本每股收益38.5元，同比增长6.2%。'),
]

STYLE = {Status.PASS: 'green', Status.FAIL: 'red', Status.UNVERIFIABLE: 'yellow'}
KIND = {ClaimKind.VALUE: '金额', ClaimKind.YOY: '同比', ClaimKind.QOQ: '环比'}


def _render(report: DiscrepancyReport, console: Console, title: str) -> None:
    table = Table(title=title)
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


def run_demo() -> int:
    """Offline demo: built-in sample sentences checked against MockSource. No network needed."""
    console = Console()
    console.print('finchecker 离线演示: 内置样例文本 + 模拟数据 (MockSource), 无需网络或 API Key。')
    console.print('注意: 模拟数据为虚构的测试夹具, 不代表真实财务数据。\n')
    source = get_source('mock')
    for title, text in DEMO_CASES:
        console.print(f'[bold cyan]{title}[/bold cyan]')
        console.print(f'  原文: {text}')
        _render(verify_claims(extract_claims(text), source), console, title)
        console.print()
    return 0


def main(argv: list[str] | None = None) -> int:
    args_list = list(sys.argv[1:]) if argv is None else list(argv)
    if args_list[:1] == ['demo']:
        return run_demo()

    p = argparse.ArgumentParser(prog='finchecker', description='A股财报/研报断言核查')
    p.add_argument('--text', help='待核查文本')
    p.add_argument('--file', help='从文件读取待核查文本')
    p.add_argument('--source', default='mock', choices=['mock', 'akshare'])
    p.add_argument('--json', action='store_true', help='输出 JSON')
    args = p.parse_args(args_list)

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
    _render(report, console, 'A股断言核查结果')
    return 0 if report.failed == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
