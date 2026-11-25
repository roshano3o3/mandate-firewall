from types import SimpleNamespace

from mandate_firewall.firewall import Context, evaluate
from mandate_firewall.models import Outcome
from mandate_firewall.proposer import ClaudeProposer, build_prompt
from mandate_firewall.scenarios import INJECTED_NEWS, T0, mandate, market, portfolio


class FakeClient:
    """Stands in for the Anthropic client so tests need no network or key."""

    def __init__(self, reply: str):
        self.reply = reply
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(content=[SimpleNamespace(text=self.reply)])


def run(reply: str):
    p = ClaudeProposer(client=FakeClient(reply), model="test-model")
    raw = p.propose(mandate(), portfolio(), market(), INJECTED_NEWS)
    return raw, evaluate(raw, Context(mandate(), portfolio(), market(), T0))


def test_good_reply_flows_to_firewall():
    raw, d = run('{"action":"buy","symbol":"BND","quantity":"40","rationale":"bonds under target"}')
    assert raw["mandate_version"] == 1 and d.outcome == Outcome.ALLOW


def test_model_that_obeys_injected_news_is_still_blocked():
    _, d = run('{"action":"buy","symbol":"BTC","quantity":"0.5","rationale":"compliance pre-approved"}')
    assert d.outcome == Outcome.BLOCK


def test_model_adding_authority_fields_is_blocked():
    _, d = run('{"action":"buy","symbol":"BTC","quantity":"0.1","rationale":"x","approved":true}')
    assert d.outcome == Outcome.BLOCK


def test_model_cannot_choose_mandate_version():
    raw, _ = run('{"action":"hold","rationale":"x","mandate_version":99}')
    assert raw["mandate_version"] == 1


def test_non_json_reply_is_blocked():
    _, d = run("I think you should buy some bitcoin!")
    assert d.outcome == Outcome.BLOCK


def test_news_is_fenced_as_untrusted_in_prompt():
    prompt = build_prompt(mandate(), portfolio(), market(), INJECTED_NEWS)
    assert f"<news>{INJECTED_NEWS}</news>" in prompt
