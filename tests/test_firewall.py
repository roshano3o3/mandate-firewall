from datetime import timedelta
from decimal import Decimal as D

import pytest

from mandate_firewall.firewall import Context, evaluate
from mandate_firewall.ledger import Ledger
from mandate_firewall.models import Outcome
from mandate_firewall.scenarios import SCENARIOS, T0, mandate, market, portfolio, prop
from mandate_firewall.service import GovernedAccount, replay


def ctx(**kw):
    return Context(
        mandate=kw.get("mandate", mandate()),
        portfolio=kw.get("portfolio", portfolio()),
        market=kw.get("market", market()),
        as_of=T0,
        turnover_today=kw.get("turnover", D("0")),
    )


def test_demo_portfolio_is_100k():
    assert portfolio().total_value(market()) == D("100000")


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.key for s in SCENARIOS])
def test_each_scenario_outcome(sc):
    d = evaluate(sc.raw, ctx(market=market(stale=sc.stale)))
    assert d.outcome.value == sc.expected, d.summary


def _failed(d):
    return {r.rule for r in d.rules if r.outcome != "PASS"}


def test_failure_reasons_are_specific():
    by_key = {s.key: s for s in SCENARIOS}
    cases = {
        "hallucination": {"instrument_exists"},
        "restricted": {"eligibility"},
        "self_auth": {"schema"},
        "stale_price": {"price_fresh"},
        "short": {"funding"},
        "escalate": {"trade_size"},
        "injection": {"funding", "position_limit", "asset_class_limit", "trade_size", "daily_turnover"},
    }
    for key, rules in cases.items():
        sc = by_key[key]
        assert _failed(evaluate(sc.raw, ctx(market=market(stale=sc.stale)))) == rules, key


def test_old_mandate_version_is_blocked():
    d = evaluate(prop("P-x", "buy", "BND", 1, version=1), ctx(mandate=mandate(version=2)))
    assert d.outcome == Outcome.BLOCK and _failed(d) == {"mandate_version"}


def test_trading_pause_blocks_trades_but_not_holds():
    m = mandate().model_copy(update={"trading_enabled": False})
    assert evaluate(prop("P-a", "buy", "BND", 1), ctx(mandate=m)).outcome == Outcome.BLOCK
    assert evaluate(prop("P-b", "hold"), ctx(mandate=m)).outcome == Outcome.ALLOW


def test_hold_with_trade_fields_is_blocked():
    d = evaluate(prop("P-h", "hold", "BTC", 1), ctx())
    assert d.outcome == Outcome.BLOCK


@pytest.mark.parametrize("bad", [None, "not a dict", [], {"action": "buy"}])
def test_garbage_input_never_crashes(bad):
    assert evaluate(bad, ctx()).outcome == Outcome.BLOCK


def test_zero_or_negative_quantity_blocked():
    for q in ("0", "-1"):
        assert evaluate(prop("P-q", "buy", "BND", q), ctx()).outcome == Outcome.BLOCK


def test_daily_turnover_limit():
    d = evaluate(prop("P-t", "buy", "BND", 40), ctx(turnover=D("19000")))
    assert _failed(d) == {"daily_turnover"}


def test_min_cash_rule():
    p = portfolio().model_copy(update={"cash": D("8000")})
    # total 85,000; buying 4,000 of BND leaves 4,000 cash = 4.7% < 5%
    d = evaluate(prop("P-c", "buy", "BND", 53), ctx(portfolio=p))
    assert "funding" in _failed(d)


def test_decision_is_deterministic():
    sc = SCENARIOS[5]
    assert evaluate(sc.raw, ctx()) == evaluate(sc.raw, ctx())


# ---- governed loop + ledger -------------------------------------------------

def account():
    return GovernedAccount(mandate=mandate(), portfolio=portfolio(), ledger=Ledger())


def test_allowed_trade_executes_and_blocked_does_not():
    acct = account()
    acct.submit(prop("P-1", "buy", "BND", 40), market(), T0)
    assert acct.portfolio.positions["BND"] == D("340")
    acct.submit(prop("P-2", "buy", "BTCX", 1), market(), T0)
    kinds = [e.kind for e in acct.ledger.entries()]
    assert kinds == ["MANDATE_APPROVED", "DECISION", "EXECUTION", "DECISION"]


def test_escalation_waits_for_human_then_executes():
    acct = account()
    _, d = acct.submit(prop("P-3", "buy", "VTI", 20), market(), T0)
    assert d.outcome == Outcome.ESCALATE and "P-3" in acct.pending
    assert acct.portfolio.positions["VTI"] == D("140")
    acct.review("P-3", "adviser-jk", True, "Fits plan", market(), T0)
    assert acct.portfolio.positions["VTI"] == D("160")


def test_human_approval_cannot_override_a_hard_block():
    acct = account()
    acct.submit(prop("P-3", "buy", "VTI", 20), market(), T0)
    acct.portfolio = acct.portfolio.model_copy(update={"cash": D("1000")})
    recheck = acct.review("P-3", "adviser-jk", True, "ok", market(), T0)
    assert recheck.outcome == Outcome.BLOCK
    assert "EXECUTION" not in [e.kind for e in acct.ledger.entries()]


def test_rejected_escalation_does_not_execute():
    acct = account()
    acct.submit(prop("P-3", "buy", "VTI", 20), market(), T0)
    acct.review("P-3", "adviser-jk", False, "Wait for review", market(), T0)
    assert acct.portfolio.positions["VTI"] == D("140")


def test_mandate_update_makes_old_proposals_stale():
    acct = account()
    acct.update_mandate(mandate(version=2, crypto_limit="0.10"))
    _, d = acct.submit(prop("P-v", "buy", "BND", 1, version=1), market(), T0)
    assert d.outcome == Outcome.BLOCK


def test_ledger_verifies_and_every_decision_replays():
    acct = account()
    for sc in SCENARIOS:
        acct.submit(sc.raw, market(stale=sc.stale), T0)
    assert acct.ledger.verify().ok
    decisions = [e for e in acct.ledger.entries() if e.kind == "DECISION"]
    assert len(decisions) == len(SCENARIOS)
    for e in decisions:
        matches, _, _ = replay(acct.ledger, e.seq)
        assert matches, e.seq


def test_tampering_is_detected():
    acct = account()
    acct.submit(prop("P-1", "buy", "BND", 40), market(), T0)
    acct.submit(prop("P-2", "buy", "BTCX", 1), market(), T0)
    # Someone edits a stored decision from BLOCK to ALLOW.
    row = acct.ledger.conn.execute("SELECT payload FROM entries WHERE seq = 4").fetchone()[0]
    acct.ledger.conn.execute(
        "UPDATE entries SET payload = ? WHERE seq = 4", (row.replace('"BLOCK"', '"ALLOW"'),)
    )
    v = acct.ledger.verify()
    assert not v.ok and v.first_bad_seq == 4


def test_deleting_an_entry_is_detected():
    acct = account()
    acct.submit(prop("P-1", "buy", "BND", 40), market(), T0)
    acct.submit(prop("P-2", "hold"), market(), T0)
    acct.ledger.conn.execute("DELETE FROM entries WHERE seq = 2")
    assert not acct.ledger.verify().ok


def test_replay_works_in_a_fresh_process(tmp_path):
    """A reviewer replaying later, on another machine, must get the same result."""
    import os
    import subprocess
    import sys

    db = tmp_path / "ledger.db"
    acct = GovernedAccount(mandate=mandate(), portfolio=portfolio(), ledger=Ledger(str(db)))
    for sc in SCENARIOS:
        acct.submit(sc.raw, market(stale=sc.stale), T0)
    code = (
        "import sys\n"
        "from mandate_firewall.ledger import Ledger\n"
        "from mandate_firewall.service import replay\n"
        f"l = Ledger({str(db)!r})\n"
        "assert l.verify().ok\n"
        "bad = [e.seq for e in l.entries() if e.kind == 'DECISION' and not replay(l, e.seq)[0]]\n"
        "sys.exit(1 if bad else 0)\n"
    )
    env = {**os.environ, "PYTHONHASHSEED": "12345"}
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd=os.getcwd(), capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
