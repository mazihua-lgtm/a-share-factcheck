"""MCP server exposing check_financial_claims, built on FastMCP (https://github.com/jlowin/fastmcp)."""
from __future__ import annotations

from fastmcp import FastMCP

from finchecker.extractor import extract_claims
from finchecker.fetcher import get_source
from finchecker.verifier import verify_claims

mcp = FastMCP('a-share-factcheck')


@mcp.tool()
def check_financial_claims(text: str, source: str = 'mock') -> dict:
    """Verify financial figures, YoY/QoQ growth rates and stock codes in Chinese A-share text.

    Args:
        text: Research or news text to check.
        source: 'mock' (test fixtures) or 'akshare' (live A-share data).
    Returns a DiscrepancyReport with per-claim status, issue_type and expected values.
    """
    report = verify_claims(extract_claims(text), get_source(source))
    return report.model_dump(mode='json')


def main() -> None:
    mcp.run()


if __name__ == '__main__':
    main()
