from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel


class ClaimKind(str, Enum):
    VALUE = 'value'
    YOY = 'yoy'
    QOQ = 'qoq'


class Status(str, Enum):
    PASS = 'pass'
    FAIL = 'fail'
    UNVERIFIABLE = 'unverifiable'


class ReportStatus(str, Enum):
    NO_CLAIMS = 'no_claims'
    UNVERIFIABLE = 'unverifiable'
    INCOMPLETE = 'incomplete'
    MISMATCH = 'mismatch'
    VERIFIED = 'verified'
    SIMULATION = 'simulation'


class Evidence(BaseModel):
    source: str
    period: str
    title: str
    value_yuan: float
    url: Optional[str] = None
    published_on: Optional[str] = None
    pdf_page: Optional[int] = None
    comparison_period: Optional[str] = None
    comparison_value_yuan: Optional[float] = None


class FinancialClaim(BaseModel):
    """A single checkable assertion extracted from text."""
    raw_text: str
    stock_code: Optional[str] = None
    period: Optional[str] = None  # e.g. 2025Q3, 2025FY
    metric: Optional[str] = None  # canonical metric name
    kind: ClaimKind = ClaimKind.VALUE
    declared_value: Optional[float] = None  # in yuan
    declared_growth_pct: Optional[float] = None  # percent


class VerificationResult(BaseModel):
    claim: FinancialClaim
    status: Status
    issue_type: Optional[str] = None
    message: str = ''
    expected_value: Optional[float] = None
    expected_growth_pct: Optional[float] = None
    evidence: Optional[Evidence] = None


class DiscrepancyReport(BaseModel):
    source: str
    simulated: bool
    status: ReportStatus
    total: int
    passed: int
    failed: int
    unverifiable: int
    results: list[VerificationResult]
