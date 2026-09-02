"""Tests for the deterministic matcher + end-to-end pipeline (zero LLM).

Focus of these tests is the property the whole system is sold on — **100%
precision, willing to abstain** — and the two subtle safeties that protect it:

  * ``trap_dup_utr`` (a trap that copies a real credit's UTR *and* exact amount)
    must never be auto-matched. It is amount-viable for the same batch as the real
    credit, so 4a/4b defer it on contention; 4c subset-sum must NOT back-door around
    that by grabbing the whole batch as a "subset".
  * ``split_1n`` (one batch legitimately paid out as two subset credits) must still
    resolve through 4c — the fix above must not over-block it.

Everything runs with the LLM disabled; these are pure deterministic-layer tests.
"""

import json
from datetime import date, datetime
from pathlib import Path

from src.eval.harness import is_correct, load_ground_truth, score
from src.match import deterministic
from src.match.candidates import CandidateIndex
from src.match.deterministic import AMBIGUOUS, unique_subset
from src.models import BankCredit, SettlementLine
from src.pipeline import reconcile_dataset

SEEDS = Path("data/seeds")


# --------------------------------------------------------------------------- #
# Unit: bounded subset-sum
# --------------------------------------------------------------------------- #


def test_unique_subset_finds_single_subset():
    vals = [("a", 100), ("b", 250), ("c", 400)]
    assert unique_subset(vals, 350) == ["a", "b"]  # 100 + 250


def test_unique_subset_flags_ambiguous():
    # two distinct subsets hit 500: {a,d} and {b,c}
    vals = [("a", 100), ("b", 200), ("c", 300), ("d", 400)]
    assert unique_subset(vals, 500) == AMBIGUOUS


def test_unique_subset_none_when_unreachable():
    assert unique_subset([("a", 100), ("b", 200)], 999) is None


def test_unique_subset_respects_tolerance():
    # 3-paise drift is within the ±5 tolerance band
    assert unique_subset([("a", 1000), ("b", 2003)], 3000) == ["a", "b"]


# --------------------------------------------------------------------------- #
# Safety: a reused-UTR + reused-amount trap must never be matched
# --------------------------------------------------------------------------- #


def _sale_line(sid: str, batch: str, net: int, ref: str, day: int) -> SettlementLine:
    return SettlementLine(
        settlement_id=sid,
        payout_batch_id=batch,
        order_id=f"ORD-{sid}",
        gross_paise=net + 400,
        fee_paise=340,
        gst_paise=60,
        net_paise=net,
        line_type="sale",
        settled_at=datetime(2026, 1, day, 12, 0, 0),
        payout_ref=ref,
    )


def _dup_utr_fixture():
    """One real batch (net 10000, UTR HDFCN0433218196) and a trap credit that
    copies the real credit's UTR and amount, dated one day later."""
    ref = "HDFCN0433218196"
    lines = [
        _sale_line("STL-1", "BATCH-A", 3000, ref, 10),
        _sale_line("STL-2", "BATCH-A", 3000, ref, 10),
        _sale_line("STL-3", "BATCH-A", 4000, ref, 10),
    ]
    real = BankCredit(
        bank_txn_id="TXN-REAL",
        amount_paise=10000,
        value_date=date(2026, 1, 12),
        narration=f"NEFT-{ref}-CR",
        utr=ref,
    )
    trap = BankCredit(
        bank_txn_id="TXN-TRAP",
        amount_paise=10000,
        value_date=date(2026, 1, 13),  # dup: same UTR/amount, +1 day
        narration=f"NEFT-{ref}-CR",
        utr=ref,
    )
    return lines, [real, trap]


def test_dup_utr_trap_is_never_matched():
    lines, credits = _dup_utr_fixture()
    idx = CandidateIndex(lines)
    cc_map = {c.bank_txn_id: idx.for_credit(c) for c in credits}
    det = deterministic.run(credits, cc_map, idx)

    # The trap must not be auto-resolved by the deterministic layer at all.
    trap_res = det.results.get("TXN-TRAP")
    assert trap_res is None or trap_res.decision != "matched"

    # If the real credit was matched, it must be to exactly the real batch's lines
    # (never a hallucinated partial). Under contention it may instead be deferred.
    real_res = det.results.get("TXN-REAL")
    if real_res is not None and real_res.decision == "matched":
        assert set(real_res.settlement_ids) == {"STL-1", "STL-2", "STL-3"}
    idx.close()


def test_dup_utr_neither_credit_consumes_the_batch():
    """The whole batch must stay free (deferred to assignment). The original bug
    let 4c subset-sum grab the entire batch's lines for whichever credit — often
    the trap — turning a contended batch into a hallucinated match."""
    lines, credits = _dup_utr_fixture()
    idx = CandidateIndex(lines)
    cc_map = {c.bank_txn_id: idx.for_credit(c) for c in credits}
    det = deterministic.run(credits, cc_map, idx)
    batch_lines = {"STL-1", "STL-2", "STL-3"}
    assert not (det.consumed_lines & batch_lines), "contended batch must stay free"
    assert "BATCH-A" not in det.consumed_batches
    idx.close()


# --------------------------------------------------------------------------- #
# The 4c gate must not over-block: a real split (subset) still resolves
# --------------------------------------------------------------------------- #


def test_split_resolves_via_subset_sum():
    """One batch legitimately paid as two subset credits. Neither credit's amount
    equals the whole batch total, so the 4c gate lets them through and each finds
    its unique subset. Guards against the dup_utr fix over-blocking real splits."""
    ref = "ICICN0012345678"
    nets = [("STL-A", 1000), ("STL-B", 2000), ("STL-C", 4000), ("STL-D", 8000)]
    lines = [
        SettlementLine(
            settlement_id=sid,
            payout_batch_id="BATCH-S",
            order_id=f"ORD-{sid}",
            gross_paise=net + 400,
            fee_paise=340,
            gst_paise=60,
            net_paise=net,
            line_type="sale",
            settled_at=datetime(2026, 2, 10, 9, 0, 0),
            payout_ref=ref,
        )
        for sid, net in nets
    ]
    credit_a = BankCredit(  # 1000 + 2000
        bank_txn_id="TXN-A", amount_paise=3000, value_date=date(2026, 2, 12),
        narration=f"NEFT-{ref}-CR", utr=ref,
    )
    credit_b = BankCredit(  # 4000 + 8000
        bank_txn_id="TXN-B", amount_paise=12000, value_date=date(2026, 2, 12),
        narration=f"NEFT-{ref}-CR", utr=ref,
    )
    idx = CandidateIndex(lines)
    cc_map = {c.bank_txn_id: idx.for_credit(c) for c in (credit_a, credit_b)}
    det = deterministic.run([credit_a, credit_b], cc_map, idx)

    ra, rb = det.results.get("TXN-A"), det.results.get("TXN-B")
    assert ra and ra.resolved_by == "subset_sum" and set(ra.settlement_ids) == {"STL-A", "STL-B"}
    assert rb and rb.resolved_by == "subset_sum" and set(rb.settlement_ids) == {"STL-C", "STL-D"}
    idx.close()


# --------------------------------------------------------------------------- #
# End-to-end: precision + determinism on the committed seeds sample
# --------------------------------------------------------------------------- #


def test_pipeline_is_100pct_precise_on_seeds():
    truth = load_ground_truth(SEEDS)
    m = score(reconcile_dataset(SEEDS), truth)
    assert m.precision == 1.0, f"precision must be 100%, got {m.precision}: {m.errors}"
    assert m.hallucinated_matches == 0
    assert m.auto_resolve_rate >= 0.70  # deterministic checkpoint (spec §18)


def _decisions(results):
    # everything that defines the answer, excluding measured latency
    return [
        (r.bank_txn_id, r.decision, sorted(r.settlement_ids), r.resolved_by, r.confidence)
        for r in results
    ]


def test_pipeline_decisions_are_deterministic_on_seeds():
    # decisions must not depend on set-iteration / hash order (only latency may vary)
    assert _decisions(reconcile_dataset(SEEDS)) == _decisions(reconcile_dataset(SEEDS))
