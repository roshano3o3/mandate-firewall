"""Run the full demo story and print it. Optionally export it for the web page.

    python -m mandate_firewall.demo              # print the run
    python -m mandate_firewall.demo --export run.json
"""

from __future__ import annotations

import argparse
import json
from datetime import timedelta

from .ledger import Ledger
from .models import Outcome
from .scenarios import SCENARIOS, T0, mandate, market, portfolio, prop
from .service import GovernedAccount, replay

MARK = {Outcome.ALLOW: "ALLOW   ", Outcome.BLOCK: "BLOCK   ", Outcome.ESCALATE: "ESCALATE"}


def run() -> dict:
    acct = GovernedAccount(mandate=mandate(), portfolio=portfolio(), ledger=Ledger())
    steps = []
    t = T0
    for sc in SCENARIOS:
        t += timedelta(minutes=1)
        seq, d = acct.submit(sc.raw, market(t, sc.stale), t)
        steps.append({"key": sc.key, "title": sc.title, "what_the_model_did": sc.what_the_model_did,
                      "raw": sc.raw, "expected": sc.expected, "seq": seq,
                      "decision": d.model_dump(mode="json")})
        if sc.key == "escalate":
            t += timedelta(minutes=1)
            acct.review("P-03", "human-reviewer", True, "Within plan; approved.", market(t), t)
            steps[-1]["human_review"] = "Approved by human-reviewer, re-checked, then executed"

    # Client tightens the crypto limit; an old-version proposal must now fail.
    t += timedelta(minutes=1)
    acct.update_mandate(mandate(version=2, crypto_limit="0.10").model_copy(update={"approved_at": t}))
    t += timedelta(minutes=1)
    raw = prop("P-10", "buy", "ETH", "0.1", "Written against the old mandate.", version=1)
    seq, d = acct.submit(raw, market(t), t)
    steps.append({"key": "stale_mandate", "title": "Proposal written for an old mandate",
                  "what_the_model_did": "Client tightened the crypto limit to 10% (v2). The model still proposes against v1.",
                  "raw": raw, "expected": "BLOCK", "seq": seq, "decision": d.model_dump(mode="json")})

    verify = acct.ledger.verify()
    replays = {s["seq"]: replay(acct.ledger, s["seq"])[0] for s in steps}
    rows = acct.ledger.conn.execute(
        "SELECT seq, ts, kind, payload, prev_hash, hash FROM entries ORDER BY seq").fetchall()
    return {
        "steps": steps,
        "verify": {"ok": verify.ok, "entries": verify.entries},
        "replays_match": all(replays.values()),
        "ledger": [dict(zip(["seq", "ts", "kind", "payload", "prev_hash", "hash"], r)) for r in rows],
        "final_portfolio": acct.portfolio.model_dump(mode="json"),
        "mandate_v1": mandate().model_dump(mode="json"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--export")
    args = ap.parse_args()
    data = run()
    for s in data["steps"]:
        d = s["decision"]
        print(f"[{d['outcome']:<8}] {s['title']}")
        print(f"           {d['summary']}")
        if s.get("human_review"):
            print(f"           -> {s['human_review']}")
    print()
    print(f"Ledger: {data['verify']['entries']} entries, chain valid = {data['verify']['ok']}")
    print(f"Every decision replays identically = {data['replays_match']}")
    bad = [s for s in data["steps"] if s["decision"]["outcome"] != s["expected"]]
    print(f"Outcomes as expected = {not bad}")
    if args.export:
        with open(args.export, "w") as f:
            json.dump(data, f, indent=1)
        print(f"Exported to {args.export}")


if __name__ == "__main__":
    main()
