"""Phase 7 learning-loop experiment: resolve one exception, re-run, measure the lift.

The scenario is the canonical one learned rules are *for*: a counterparty settles
under a payout-reference format our extractor doesn't recognise
(``PYT-1784-5678-9012`` — grouped digits the UTR regex can't read). Its credits look
like near-duplicates (two identical-total batches, same date, no parseable reference)
and correctly abstain. An analyst resolves ONE of them; we induce a rule from that
single resolution; on re-run the rule recovers every such reference and the whole
cohort auto-resolves — at 100% precision, because each recovered reference is
re-verified against a real batch's amount.

    python -m src.rules.experiment
"""

from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from src.eval.harness import score
from src.fees import compute_net_from_gross
from src.models import BankCredit, GroundTruth, SettlementLine
from src.pipeline import reconcile
from src.rules.learned import induce_rule


def _sale_line(stl_id, batch_id, ref, sdate, gross):
    fee, gst, net = compute_net_from_gross(gross)
    return SettlementLine(
        settlement_id=stl_id, payout_batch_id=batch_id, order_id=None,
        gross_paise=gross, fee_paise=fee, gst_paise=gst, net_paise=net,
        line_type="sale", settled_at=datetime.combine(sdate, time(12, 0)), payout_ref=ref,
    )


def build_demo(n_pairs: int = 8, seed: int = 7):
    """Return (settlements, credits, gts) for the demo. A handful of ordinary
    (base-extractor-resolvable) credits, plus ``n_pairs`` novel-reference pairs."""
    rng = random.Random(seed)
    settlements: list[SettlementLine] = []
    credits: list[BankCredit] = []
    gts: list[GroundTruth] = []
    stl = batch = txn = 0
    base = date(2026, 3, 2)

    # ---- ordinary credits: a normal bank UTR the extractor reads fine -----------
    for _ in range(6):
        batch += 1
        bid = f"BATCH-{batch:03d}"
        utr = "HDFCN" + "".join(str(rng.randint(0, 9)) for _ in range(12))
        sdate = base + timedelta(days=rng.randint(0, 60))
        lines = []
        for _ in range(rng.randint(6, 12)):
            stl += 1
            lines.append(_sale_line(f"STL-{stl:04d}", bid, utr, sdate, rng.randint(500, 40000) * 100))
        settlements += lines
        amt = sum(l.net_paise for l in lines)
        txn += 1
        tid = f"TXN-{txn:04d}"
        credits.append(BankCredit(bank_txn_id=tid, amount_paise=amt,
                                  value_date=sdate + timedelta(days=2),
                                  narration=f"NEFT {utr} RAZORPAY", utr=None))
        gts.append(GroundTruth(bank_txn_id=tid, settlement_ids=[l.settlement_id for l in lines],
                               difficulty="easy", corruption_tags=[]))

    # ---- novel-reference pairs: two identical-total batches, same date, each credit
    # carrying its OWN batch's reference in the unparsed 'PYT-####-####-####' format --
    for _ in range(n_pairs):
        sdate = base + timedelta(days=rng.randint(0, 60))
        grosses = [rng.randint(500, 40000) * 100 for _ in range(rng.randint(8, 15))]
        for _ in range(2):
            batch += 1
            bid = f"BATCH-{batch:03d}"
            digits = "".join(str(rng.randint(0, 9)) for _ in range(12))
            ref = f"PYT{digits}"  # stored on the settlement line, no separators
            lines = [_sale_line(f"STL-{(stl := stl + 1):04d}", bid, ref, sdate, g) for g in grosses]
            settlements += lines
            amt = sum(l.net_paise for l in lines)
            grouped = f"PYT-{digits[0:4]}-{digits[4:8]}-{digits[8:12]}"  # as the bank prints it
            txn += 1
            tid = f"TXN-{txn:04d}"
            credits.append(BankCredit(bank_txn_id=tid, amount_paise=amt,
                                      value_date=sdate + timedelta(days=2),
                                      narration=f"SETTLEMENT {grouped} CR", utr=None))
            gts.append(GroundTruth(bank_txn_id=tid, settlement_ids=[l.settlement_id for l in lines],
                                   difficulty="hard", corruption_tags=["novel_ref"]))
    return settlements, credits, gts


def _ref_of(gt: GroundTruth, settlements) -> str:
    by_id = {l.settlement_id: l for l in settlements}
    return by_id[gt.settlement_ids[0]].payout_ref


def write_demo_dataset(out_dir, n_pairs: int = 8) -> None:
    """Materialise the novel-reference demo as a normal dataset dir (orders.xlsx,
    settlements.csv, bank.json, ground_truth.json) so the API / UI can load it and
    walk the full resolve -> learn -> re-run loop end to end."""
    from pathlib import Path

    from src.generator.generate import (
        write_bank_json,
        write_ground_truth,
        write_orders_xlsx,
        write_settlements_csv,
    )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    settlements, credits, gts = build_demo(n_pairs=n_pairs)
    write_orders_xlsx(out / "orders.xlsx", [])  # matcher doesn't need orders
    write_settlements_csv(out / "settlements.csv", settlements)
    write_bank_json(out / "bank.json", credits)
    write_ground_truth(out / "ground_truth.json", gts)


def run_experiment(n_pairs: int = 8) -> None:
    settlements, credits, gts = build_demo(n_pairs=n_pairs)

    # ---- before: no learned rules ------------------------------------------------
    before = score(reconcile([], settlements, credits, rules=None), gts)

    # ---- the analyst resolves ONE novel-ref exception ---------------------------
    novel = [g for g in gts if "novel_ref" in g.corruption_tags]
    seed_gt = novel[0]
    seed_credit = next(c for c in credits if c.bank_txn_id == seed_gt.bank_txn_id)
    rule = induce_rule(seed_credit.narration, _ref_of(seed_gt, settlements),
                       created_by="analyst@ops", source_txn_id=seed_gt.bank_txn_id)

    # ---- after: the single induced rule is now consulted on every credit --------
    after_results = reconcile([], settlements, credits, rules=[rule])
    after = score(after_results, gts)
    learned_hits = sum(1 for r in after_results if r.resolved_by == "learned_rule")

    print("LEARNING-LOOP EXPERIMENT — novel payout-reference format")
    print("=" * 64)
    print(f"  dataset: {len(credits)} credits ({2 * n_pairs} in the novel-ref cohort)")
    print(f"  analyst resolved 1 exception ({seed_gt.bank_txn_id}); induced 1 rule:")
    print(f"    pattern       {rule.pattern}")
    print(f"    scope_contains {rule.scope_contains!r}   normalizer {rule.normalizer}")
    print()
    print(f"  {'':18}{'coverage':>10}{'precision':>11}{'halluc':>8}")
    print(f"  {'before (0 rules)':18}{before.auto_resolve_rate:>9.1%}"
          f"{before.precision:>11.1%}{before.hallucinated_matches:>8}")
    print(f"  {'after  (1 rule) ':18}{after.auto_resolve_rate:>9.1%}"
          f"{after.precision:>11.1%}{after.hallucinated_matches:>8}")
    print()
    lift = after.auto_resolve_rate - before.auto_resolve_rate
    print(f"  auto-resolve lift: +{lift:.1%}  "
          f"({learned_hits} credits recovered by the learned rule, "
          f"from a single analyst resolution)")


if __name__ == "__main__":
    run_experiment()
