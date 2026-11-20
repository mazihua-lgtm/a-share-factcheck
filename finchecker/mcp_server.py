"""MCP server exposing check_financial_claims, built on FastMCP (https://github.com/jlowin/fastmcp)."""
from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP

from finchecker.extractor import extract_claims
from finchecker.fetcher import get_source
from finchecker.verifier import verify_claims

mcp = FastMCP('a-share-factcheck')


@mcp.tool()
def check_financial_claims(text: str, source: Literal['disclosure', 'akshare'] = 'disclosure') -> dict:
    """Compare extracted Chinese A-share financial claims against a selected source.

    Args:
        text: Research or news text to check.
        source: 'disclosure' (limited official filing snapshot) or 'akshare' (third-party summary).
    Returns source, overall status, and per-claim results with available evidence.
    """
    if source not in ('disclosure', 'akshare'):
        raise ValueError('Use demo_financial_claims for simulated data')
    report = verify_claims(extract_claims(text), get_source(source))
    return report.model_dump(mode='json')


@mcp.tool()
def demo_financial_claims() -> dict:
    """Run a fixed fictional example. Results are simulated and must not be used as verification."""
    text = '600519 2025年三季报：营业收入130亿元，同比增长8.3%。'
    return verify_claims(extract_claims(text), get_source('mock')).model_dump(mode='json')


def main() -> None:
    mcp.run()


if __name__ == '__main__':
    main()
