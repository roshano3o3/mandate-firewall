# Mandate Firewall

**When the model is wrong, the mandate still holds.**

A small, working prototype of a governed AI investment loop. The AI only *proposes* an action. A separate rule engine with no AI in it decides whether that proposal may act, and every decision is written to a tamper-evident log that anyone can replay.

```
Mandate  ->  AI proposal  ->  Firewall  ->  Human review  ->  Execution  ->  Ledger
(client-     (buy / sell /    (allow /      (escalations     (paper         (hash-chained,
 approved,    deliberate       block /       only)            simulator)     replayable)
 versioned)   hold)            escalate)
```

## Why

An AI model can make an investment decision. It should never be able to give itself permission to act on it. The controls and the audit trail have to stay valid exactly when the model is wrong: when it invents a ticker, believes an injected instruction, works from stale data, or tries to approve itself.

## What the demo run shows

| Scenario | What the model did | Ruling |
|---|---|---|
| Deliberate inaction | Decides not to trade | Allowed and recorded |
| Small rebalance | Adds 3% to bonds | Allowed and executed |
| Larger trade | Proposes a 6% equity buy | Escalated, approved by a human, re-checked, executed |
| Invented ticker | Buys `BTCX`, which doesn't exist | Blocked: instrument not in market data |
| Restricted asset | Buys an asset the client excluded | Blocked: eligibility |
| Prompt injection | Believes "compliance pre-approved max BTC" from a news feed | Blocked: funding, position, asset class, trade size and turnover limits |
| Self-authorization | Adds `approved_by` and `override_limits` to its own output | Blocked: strict schema, before any check runs |
| Stale data | Trades on a two-hour-old price | Blocked: price freshness |
| Short sale | Sells more than it holds | Blocked: funding |
| Old mandate | Proposes against v1 after the client approved v2 | Blocked: mandate version |

## Design choices

- **The model's output is data, never authority.** Proposals are parsed strictly; any extra field is rejected. Identity fields (proposal id, mandate version) come from the system, not the model.
- **Deterministic checks.** The firewall has no model calls and no clock of its own, so the same inputs always produce the same decision.
- **Human review lifts an escalation, not a limit.** On approval the proposal is re-checked against current state; hard rules still apply.
- **Inaction is a decision.** A `hold` is logged like a trade.
- **Replayable evidence.** Each decision entry stores its full inputs (mandate, portfolio, prices, raw proposal). `replay()` re-runs it and compares, including in a fresh process.
- **Tamper evidence.** Each ledger entry includes the hash of the previous one. Editing, deleting or reordering a row breaks verification. The demo page re-verifies the chain in the browser and lets you tamper with an entry.
- **Exact arithmetic.** Money and limits use `Decimal`.

## Run it

```bash
pip install -r requirements.txt
python -m pytest            # 37 tests
python -m mandate_firewall.demo
python web/build.py         # builds web/index.html from a fresh run
```

Optional live proposer (needs an Anthropic API key):

```python
from mandate_firewall.proposer import ClaudeProposer
raw = ClaudeProposer().propose(mandate, portfolio, market, news="...")
decision = account.submit(raw, market, as_of)
```

## Limits, stated plainly

- Prices are simulated round numbers, not market data. The paper broker fills instantly with no fees, slippage or partial fills.
- The mandate rules are illustrative. This is not a compliance program and not investment advice.
- The demo uses scripted proposals so every failure is reproducible. The Claude proposer is tested against a stand-in client; the live API call is not exercised in the test suite.
- Detecting a full rewrite of the log requires anchoring the latest hash outside the system (for example, publishing it daily). Not implemented here.
- A real system also needs broker integration, reconciliation against confirmations, exception handling and recovery, access control, and supervision. Those are deliberately out of scope.

## Layout

```
mandate_firewall/
  models.py     mandate, proposal, market, decision models
  firewall.py   deterministic checks
  ledger.py     hash-chained SQLite log
  service.py    governed loop, human review, paper broker, replay
  proposer.py   Claude-backed proposer (optional)
  scenarios.py  demo data and "model is wrong" cases
  demo.py       runs the full story
web/            static demo page with in-browser verification
tests/
```

Independent prototype by Roshan Pellati. Not affiliated with any company; contains no third-party code or data.
