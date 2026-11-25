"""Core data models: the client mandate, market data, portfolio, and AI proposals.

Money and percentages use Decimal so that limit checks are exact and
reproducible on replay.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def canonical_json(data: object) -> str:
    """Stable JSON encoding used for hashing and the audit ledger."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Mandate(BaseModel):
    """A client-approved, versioned investment mandate.

    Every limit is a fraction of total portfolio value (0.10 == 10%).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mandate_id: str
    version: int = Field(ge=1)
    client_ref: str = Field(description="Pseudonymous client reference, never PII")
    approved_by: str = Field(description="Who approved this version (the client)")
    approved_at: datetime
    objective: str

    allowed_symbols: frozenset[str]
    restricted_symbols: frozenset[str] = frozenset()
    max_position_pct: Decimal = Field(gt=0, le=1)
    max_asset_class_pct: dict[str, Decimal] = Field(default_factory=dict)
    min_cash_pct: Decimal = Field(ge=0, le=1)
    max_single_trade_pct: Decimal = Field(gt=0, le=1)
    escalate_trade_pct: Decimal = Field(gt=0, le=1)
    max_daily_turnover_pct: Decimal = Field(gt=0)
    max_price_age_seconds: int = Field(gt=0)
    trading_enabled: bool = True

    @field_validator("allowed_symbols", "restricted_symbols", mode="before")
    @classmethod
    def _upper(cls, v):
        return frozenset(s.upper() for s in v)

    @field_serializer("allowed_symbols", "restricted_symbols")
    def _sorted(self, v: frozenset[str]) -> list[str]:
        # Sets have no stable order; sort so the digest is identical across runs.
        return sorted(v)

    def digest(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))


class Quote(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbol: str
    asset_class: str
    price: Decimal = Field(gt=0)
    as_of: datetime


class MarketSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    quotes: dict[str, Quote]

    def get(self, symbol: str) -> Quote | None:
        return self.quotes.get(symbol.upper())


class Portfolio(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cash: Decimal = Field(ge=0)
    positions: dict[str, Decimal] = Field(default_factory=dict)

    def total_value(self, market: MarketSnapshot) -> Decimal:
        value = self.cash
        for sym, qty in self.positions.items():
            quote = market.get(sym)
            if quote is None:
                raise ValueError(f"No price for held position {sym}")
            value += qty * quote.price
        return value


class Proposal(BaseModel):
    """What the AI proposes. Parsed strictly: unknown fields are rejected,
    so the model cannot smuggle in fields like 'approved' or 'override'."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    mandate_id: str
    mandate_version: int
    action: Literal["buy", "sell", "hold"]
    symbol: str | None = None
    quantity: Decimal | None = None
    rationale: str = Field(min_length=1, max_length=2000)
    proposer: str

    @field_validator("symbol")
    @classmethod
    def _upper_symbol(cls, v):
        return v.upper() if v else v


class Outcome(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    ESCALATE = "ESCALATE"


class RuleResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule: str
    outcome: Literal["PASS", "BLOCK", "ESCALATE"]
    detail: str


class Decision(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str | None
    outcome: Outcome
    rules: list[RuleResult]
    mandate_digest: str
    summary: str
