"""The governed loop: mandate -> AI proposal -> firewall -> human review
(when escalated) -> simulated execution -> ledger.

Execution here is a paper simulator that fills at the snapshot price.
It does not model partial fills, fees or slippage; a real broker
integration would replace PaperBroker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from .firewall import Context, evaluate
from .ledger import Ledger
from .models import Decision, Mandate, MarketSnapshot, Outcome, Portfolio, Proposal


class PaperBroker:
    def fill(self, portfolio: Portfolio, proposal: Proposal, price: Decimal) -> Portfolio:
        positions = dict(portfolio.positions)
        value = proposal.quantity * price
        held = positions.get(proposal.symbol, Decimal("0"))
        if proposal.action == "buy":
            cash = portfolio.cash - value
            positions[proposal.symbol] = held + proposal.quantity
        else:
            cash = portfolio.cash + value
            remaining = held - proposal.quantity
            if remaining == 0:
                positions.pop(proposal.symbol, None)
            else:
                positions[proposal.symbol] = remaining
        return Portfolio(cash=cash, positions=positions)


@dataclass
class Pending:
    seq: int
    raw: dict
    decision: Decision


@dataclass
class GovernedAccount:
    mandate: Mandate
    portfolio: Portfolio
    ledger: Ledger
    broker: PaperBroker = field(default_factory=PaperBroker)
    turnover_today: Decimal = Decimal("0")
    pending: dict[str, Pending] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ledger.append(
            "MANDATE_APPROVED",
            {"mandate": self.mandate.model_dump(mode="json")},
            self.mandate.approved_at,
        )

    def update_mandate(self, new: Mandate) -> None:
        """A new version must be approved by the client and is logged."""
        if new.mandate_id != self.mandate.mandate_id or new.version <= self.mandate.version:
            raise ValueError("New mandate must keep the id and increase the version")
        self.mandate = new
        self.ledger.append("MANDATE_APPROVED", {"mandate": new.model_dump(mode="json")}, new.approved_at)

    def _context(self, market: MarketSnapshot, as_of: datetime) -> Context:
        return Context(self.mandate, self.portfolio, market, as_of, self.turnover_today)

    def submit(self, raw: dict, market: MarketSnapshot, as_of: datetime) -> tuple[int, Decision]:
        """Run one AI proposal through the firewall. Returns (ledger seq, decision)."""
        ctx = self._context(market, as_of)
        decision = evaluate(raw, ctx)
        entry = self.ledger.append(
            "DECISION",
            {
                "inputs": {
                    "mandate": self.mandate.model_dump(mode="json"),
                    "portfolio": self.portfolio.model_dump(mode="json"),
                    "market": market.model_dump(mode="json"),
                    "as_of": as_of.isoformat(),
                    "turnover_today": str(self.turnover_today),
                    "raw_proposal": raw,
                },
                "decision": decision.model_dump(mode="json"),
            },
            as_of,
        )
        if decision.outcome == Outcome.ALLOW:
            self._execute(raw, market, as_of)
        elif decision.outcome == Outcome.ESCALATE:
            self.pending[decision.proposal_id] = Pending(entry.seq, raw, decision)
        return entry.seq, decision

    def review(
        self,
        proposal_id: str,
        reviewer: str,
        approve: bool,
        note: str,
        market: MarketSnapshot,
        as_of: datetime,
    ) -> Decision:
        """A human resolves an escalation. Approval only lifts the escalation:
        the proposal is re-checked against current state and every hard
        rule must still pass."""
        item = self.pending.pop(proposal_id)
        self.ledger.append(
            "HUMAN_REVIEW",
            {"proposal_id": proposal_id, "reviewer": reviewer, "approved": approve,
             "note": note, "decision_seq": item.seq},
            as_of,
        )
        recheck = evaluate(item.raw, self._context(market, as_of))
        blocked = recheck.outcome == Outcome.BLOCK
        if approve and not blocked:
            self._execute(item.raw, market, as_of)
        return recheck

    def _execute(self, raw: dict, market: MarketSnapshot, as_of: datetime) -> None:
        p = Proposal.model_validate(raw)
        if p.action == "hold":
            return
        price = market.get(p.symbol).price
        self.portfolio = self.broker.fill(self.portfolio, p, price)
        self.turnover_today += p.quantity * price
        self.ledger.append(
            "EXECUTION",
            {
                "proposal_id": p.proposal_id,
                "venue": "paper-simulator",
                "side": p.action,
                "symbol": p.symbol,
                "quantity": str(p.quantity),
                "price": str(price),
                "portfolio_after": self.portfolio.model_dump(mode="json"),
            },
            as_of,
        )


def replay(ledger: Ledger, seq: int) -> tuple[bool, Decision, Decision]:
    """Re-run a logged decision from its stored inputs.

    Returns (matches, recorded, replayed)."""
    entry = ledger.get(seq)
    if entry is None or entry.kind != "DECISION":
        raise ValueError(f"Entry {seq} is not a decision")
    inp = entry.payload["inputs"]
    ctx = Context(
        mandate=Mandate.model_validate(inp["mandate"]),
        portfolio=Portfolio.model_validate(inp["portfolio"]),
        market=MarketSnapshot.model_validate(inp["market"]),
        as_of=datetime.fromisoformat(inp["as_of"]),
        turnover_today=Decimal(inp["turnover_today"]),
    )
    recorded = Decision.model_validate(entry.payload["decision"])
    replayed = evaluate(inp["raw_proposal"], ctx)
    return recorded == replayed, recorded, replayed
