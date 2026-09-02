"""Tests for Phase 5 — global bipartite assignment.

The assignment layer exists to resolve the *contended* remainder the deterministic
layer refuses to guess on. Its correctness rests on one idea: a reused-UTR trap can
forge a reference, but it cannot land on the real payout's value date. So the winner
of a contended batch is decided by un-forgeable evidence (date proximity), never by
reference alone.

These tests pin down the three shapes that matter:
  * simple dup_utr        -> real credit wins its batch; the trap becomes no_match.
  * near_dup              -> two indistinguishable batches -> abstain, never guess.
  * dup_utr + near_dup    -> the collision that first fooled max-weight assignment
                             (the trap's forged UTR out-scored the real owner);
                             nobody is matched, precision preserved.
"""

from datetime import date, datetime

from src.eval.harness import load_ground_truth, score
from src.match import assignment, deterministic
from src.match.candidates import CandidateIndex
from src.models import BankCredit, SettlementLine
from src.pipeline import reconcile_dataset

SEEDS = "data/seeds"


def _lines(batch: str, ref: str | None, nets: list[int], day: int = 10) -> list[SettlementLine]:
    out = []
    for i, net in enumerate(nets):
        out.append(
            SettlementLine(
                settlement_id=f"{batch}-{i}",
                payout_batch_id=batch,
                order_id=f"ORD-{batch}-{i}",
                gross_paise=net + 400,
                fee_paise=340,
                gst_paise=60,
                net_paise=net,
                line_type="sale",
                settled_at=datetime(2026, 3, day, 10, 0, 0),
                payout_ref=ref,
            )
        )
    return out


def _credit(tid: str, amount: int, offset: int, utr: str | None) -> BankCredit:
    return BankCredit(
        bank_txn_id=tid,
        amount_paise=amount,
        value_date=date(2026, 3, 10 + offset),  # settled day 10 + offset
        narration=(f"NEFT-{utr}-CR" if utr else "NEFT-INWARD-SETTLEMENT"),
        utr=utr,
    )


def _run(lines, credits):
    idx = CandidateIndex(lines)
    cc_map = {c.bank_txn_id: idx.for_credit(c) for c in credits}
    det = deterministic.run(credits, cc_map, idx)
    unresolved = [c for c in credits if c.bank_txn_id not in det.results]
    asg = assignment.assign(
        unresolved, cc_map, set(det.consumed_batches), set(det.consumed_lines)
    )
    idx.close()
    return det, asg


def test_simple_dup_utr_real_wins_trap_is_refused():
    ref = "HDFCN0433218196"
    lines = _lines("BATCH-A", ref, [3000, 3000, 4000])
    real = _credit("TXN-REAL", 10000, offset=2, utr=ref)   # lands on the value date
    trap = _credit("TXN-TRAP", 10000, offset=3, utr=ref)   # dup: same UTR, a day late
    det, asg = _run(lines, [real, trap])

    assert "TXN-REAL" in asg.results
    assert set(asg.results["TXN-REAL"].settlement_ids) == {"BATCH-A-0", "BATCH-A-1", "BATCH-A-2"}
    assert asg.results["TXN-REAL"].resolved_by == "assignment"
    # the trap must not be matched, and its batch is now consumed -> it will be no_match
    assert "TXN-TRAP" not in asg.results
    assert "BATCH-A" in asg.consumed_batches


def test_near_dup_two_identical_batches_abstains():
    # two batches, identical totals, credit has no UTR -> genuinely ambiguous
    lines = _lines("BATCH-A", "REFA0000000001", [10000]) + _lines("BATCH-B", "REFB0000000002", [10000])
    credit = _credit("TXN-X", 10000, offset=2, utr=None)
    det, asg = _run(lines, [credit])
    assert "TXN-X" not in asg.results  # must abstain, never pick one at random


def test_dup_utr_and_near_dup_collision_matches_nobody():
    """The regression for BUG-002: a reused-UTR trap collides with a near_dup batch.
    Its forged reference gives it the top raw score, but two real credits with the
    ideal value date also claim the batch -> the trap cannot win, and the near_dup
    pair stays ambiguous. Result: no match, precision preserved."""
    ref_b = "ICICN0012345678"
    lines = _lines("BATCH-A", "REFA0000000001", [10000]) + _lines("BATCH-B", ref_b, [10000])
    # near_dup real owners: no UTR, ideal date, exact amount
    x = _credit("TXN-X", 10000, offset=2, utr=None)
    y = _credit("TXN-Y", 10000, offset=2, utr=None)
    # trap: carries BATCH-B's reference but lands a day late
    trap = _credit("TXN-TRAP", 10000, offset=3, utr=ref_b)
    det, asg = _run(lines, [x, y, trap])

    assert asg.results == {}, "no credit may be matched in the collision"


def test_greedy_vs_global_both_precise_global_covers_more():
    """Required experiment, as an invariant: across assignment modes precision stays
    100% and global resolves at least as much as greedy, which beats off."""
    truth = load_ground_truth(SEEDS)
    cov = {}
    for mode in ("off", "greedy", "global"):
        m = score(reconcile_dataset(SEEDS, assignment_mode=mode), truth)
        assert m.precision == 1.0, f"{mode} broke precision: {m.errors}"
        assert m.hallucinated_matches == 0
        cov[mode] = m.auto_resolve_rate
    assert cov["global"] >= cov["greedy"] >= cov["off"]
