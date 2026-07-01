from __future__ import annotations

from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True)
class Account:
    account_id: str
    name: str
    mask: str | None
    kind: str  # depository / credit / investment / loan
    subtype: str | None
    iso_currency: str | None
    current_balance: float


@dataclass(frozen=True)
class NetWorthPoint:
    date: date
    assets: float
    liabilities: float
    net: float
    potential_net: float  # net + value of accounts excluded from net worth (e.g. unvested RSUs)


@dataclass(frozen=True)
class CategorySpend:
    category: str
    total: float
    transaction_count: int


@dataclass(frozen=True)
class BudgetProgress:
    category: str
    monthly_limit: float
    spent_so_far: float
    remaining: float
    pct_used: float


@dataclass(frozen=True)
class TransactionDetail:
    transaction_id: str
    date: date
    name: str
    merchant: str | None
    amount: float
    category_detailed: str | None
    account_name: str | None


@dataclass(frozen=True)
class Subscription:
    """A recurring transaction detected from cadence + amount similarity."""
    label: str                   # merchant or name used as the group key
    amount: float                # representative (median) amount
    occurrences: int             # how many times seen
    last_date: date              # most recent occurrence
    avg_interval_days: float     # mean spacing between occurrences
    category: str                # friendly category, if any


@dataclass(frozen=True)
class SectorAllocation:
    """Portfolio slice aggregated by asset sector."""
    sector: str
    total: float
    asset_count: int


@dataclass(frozen=True)
class AssetDetail:
    """A single portfolio position or cash account in a sector."""
    account_id: str
    account_name: str
    institution: str | None
    security_id: str | None
    ticker: str | None
    name: str
    sector: str               # effective sector (after override)
    value: float
    quantity: float | None
    cost_basis: float | None


@dataclass(frozen=True)
class DebtLoan:
    """A manually-tracked loan (e.g. student loan) not synced via Plaid."""
    loan_id: str
    name: str
    loan_type: str | None
    interest_rate: float | None      # annual percent, e.g. 6.54
    min_payment: float | None
    current_balance: float | None
    next_due_date: date | None
    auto_pay: bool
    status: str
    notes: str | None


@dataclass(frozen=True)
class LoanPayment:
    """A payment logged against a loan (or combined across all loans)."""
    payment_id: str
    loan_id: str | None              # None = combined payment across all loans
    payment_date: date
    amount: float
    status: str
    source: str | None
    notes: str | None
