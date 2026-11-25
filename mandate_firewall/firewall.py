"""The firewall: deterministic checks between an AI proposal and execution.

There is no model call in this file. Given the same mandate, portfolio,
market snapshot and proposal, it always returns the same decision, which
is what makes every decision replayable from the audit ledger.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from pydantic import ValidationError

from .models import (
    Decision,
    Mandate,
    MarketSnapshot,
    Outcome,
    Portfolio,
    Proposal,
    RuleResult,
)


@dataclass(frozen=True)
class Context:
    mandate: Mandate
    portfolio: Portfolio
    market: MarketSnapshot
    as_of: datetime
    turnover_today: Decimal = Decimal("0")


def _pct(x: Decimal) -> str:
    return f"{(x * 100).quantize(Decimal('0.01'))}%"


def _finish(proposal_id, rules: list[RuleResult], mandate: Mandate) -> Decision:
    if any(r.outcome == "BLOCK" for r in rules):
        outcome = Outcome.BLOCK
        failed = [r for r in rules if r.outcome == "BLOCK"]
    elif any(r.outcome == "ESCALATE" for r in rules):
        outcome = Outcome.ESCALATE
        failed = [r for r in rules if r.outcome == "ESCALATE"]
    else:
        outcome = Outcome.ALLOW
        failed = []
    if failed:
        summary = "; ".join(f"{r.rule}: {r.detail}" for r in failed)
    else:
        summary = "All checks passed."
    return Decision(
        proposal_id=proposal_id,
        outcome=outcome,
        rules=rules,
        mandate_digest=mandate.digest(),
        summary=summary,
    )


def evaluate(raw_proposal: dict, ctx: Context) -> Decision:
    """Evaluate a raw (untrusted) proposal from the AI against the mandate."""
    m = ctx.mandate
    rules: list[RuleResult] = []

    def add(rule: str, outcome: str, detail: str) -> None:
        rules.append(RuleResult(rule=rule, outcome=outcome, detail=detail))

    # 1. Strict schema. The AI's output is data, never authority.
    try:
        p = Proposal.model_validate(raw_proposal)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or 'proposal'}: {err['msg']}"
            for err in e.errors()
        )
        add("schema", "BLOCK", f"Proposal rejected before any check ran ({problems})")
        pid = raw_proposal.get("proposal_id") if isinstance(raw_proposal, dict) else None
        return _finish(pid if isinstance(pid, str) else None, rules, m)
    add("schema", "PASS", "Proposal matches the strict schema")

    # 2. Mandate identity and version.
    if p.mandate_id != m.mandate_id or p.mandate_version != m.version:
        add(
            "mandate_version",
            "BLOCK",
            f"Proposal targets {p.mandate_id} v{p.mandate_version}; "
            f"current approved mandate is {m.mandate_id} v{m.version}",
        )
        return _finish(p.proposal_id, rules, m)
    add("mandate_version", "PASS", f"Matches approved mandate v{m.version}")

    # 3. Deliberate inaction is a valid, recorded decision.
    if p.action == "hold":
        if p.symbol or p.quantity:
            add("hold_shape", "BLOCK", "A hold must not carry a symbol or quantity")
        else:
            add("hold_shape", "PASS", "Decision not to trade recorded")
        return _finish(p.proposal_id, rules, m)

    # 4. Trading switch.
    if not m.trading_enabled:
        add("trading_enabled", "BLOCK", "Trading is paused for this mandate")
        return _finish(p.proposal_id, rules, m)
    add("trading_enabled", "PASS", "Trading is enabled")

    # 5. Shape of a trade.
    if not p.symbol or p.quantity is None or p.quantity <= 0:
        add("trade_shape", "BLOCK", "Buy/sell needs a symbol and a positive quantity")
        return _finish(p.proposal_id, rules, m)
    add("trade_shape", "PASS", f"{p.action} {p.quantity} {p.symbol}")

    # 6. The instrument must exist in market data (catches invented tickers).
    quote = ctx.market.get(p.symbol)
    if quote is None:
        add("instrument_exists", "BLOCK", f"{p.symbol} is not in the market data feed")
        return _finish(p.proposal_id, rules, m)
    add("instrument_exists", "PASS", f"{p.symbol} found ({quote.asset_class})")

    # 7. Eligibility under the mandate.
    if p.symbol in m.restricted_symbols:
        add("eligibility", "BLOCK", f"{p.symbol} is restricted by the client")
    elif p.symbol not in m.allowed_symbols:
        add("eligibility", "BLOCK", f"{p.symbol} is not on the approved list")
    else:
        add("eligibility", "PASS", f"{p.symbol} is approved")

    # 8. Price freshness.
    age = (ctx.as_of - quote.as_of).total_seconds()
    if age > m.max_price_age_seconds:
        add(
            "price_fresh",
            "BLOCK",
            f"Price is {int(age)}s old; limit is {m.max_price_age_seconds}s",
        )
    else:
        add("price_fresh", "PASS", f"Price is {int(age)}s old")

    total = ctx.portfolio.total_value(ctx.market)
    value = p.quantity * quote.price
    held = ctx.portfolio.positions.get(p.symbol, Decimal("0"))

    # 9. Funding / holdings.
    if p.action == "buy":
        cash_after = ctx.portfolio.cash - value
        if cash_after < 0:
            add("funding", "BLOCK", f"Needs {value:.2f} but only {ctx.portfolio.cash:.2f} cash")
        elif cash_after / total < m.min_cash_pct:
            add(
                "funding",
                "BLOCK",
                f"Cash would fall to {_pct(cash_after / total)}; minimum is {_pct(m.min_cash_pct)}",
            )
        else:
            add("funding", "PASS", f"Cash after trade {_pct(cash_after / total)}")
    else:
        if p.quantity > held:
            add(
                "funding",
                "BLOCK",
                f"Sell of {p.quantity} exceeds holding of {held} (no shorting)",
            )
        else:
            add("funding", "PASS", f"Holding {held} covers the sale")

    # 10. Concentration limits (buys only increase exposure).
    if p.action == "buy":
        pos_after = (held + p.quantity) * quote.price / total
        if pos_after > m.max_position_pct:
            add(
                "position_limit",
                "BLOCK",
                f"{p.symbol} would be {_pct(pos_after)}; limit is {_pct(m.max_position_pct)}",
            )
        else:
            add("position_limit", "PASS", f"{p.symbol} would be {_pct(pos_after)}")

        limit = m.max_asset_class_pct.get(quote.asset_class)
        if limit is not None:
            class_value = value
            for sym, qty in ctx.portfolio.positions.items():
                q = ctx.market.get(sym)
                if q and q.asset_class == quote.asset_class:
                    class_value += qty * q.price
            class_after = class_value / total
            if class_after > limit:
                add(
                    "asset_class_limit",
                    "BLOCK",
                    f"{quote.asset_class} would be {_pct(class_after)}; limit is {_pct(limit)}",
                )
            else:
                add("asset_class_limit", "PASS", f"{quote.asset_class} would be {_pct(class_after)}")

    # 11. Trade size: hard cap, then human escalation band.
    trade_pct = value / total
    if trade_pct > m.max_single_trade_pct:
        add(
            "trade_size",
            "BLOCK",
            f"Trade is {_pct(trade_pct)} of portfolio; hard cap is {_pct(m.max_single_trade_pct)}",
        )
    elif trade_pct > m.escalate_trade_pct:
        add(
            "trade_size",
            "ESCALATE",
            f"Trade is {_pct(trade_pct)} of portfolio; above {_pct(m.escalate_trade_pct)} needs human approval",
        )
    else:
        add("trade_size", "PASS", f"Trade is {_pct(trade_pct)} of portfolio")

    # 12. Daily turnover.
    turnover_after = (ctx.turnover_today + value) / total
    if turnover_after > m.max_daily_turnover_pct:
        add(
            "daily_turnover",
            "BLOCK",
            f"Turnover would reach {_pct(turnover_after)}; daily limit is {_pct(m.max_daily_turnover_pct)}",
        )
    else:
        add("daily_turnover", "PASS", f"Turnover would be {_pct(turnover_after)}")

    return _finish(p.proposal_id, rules, m)
