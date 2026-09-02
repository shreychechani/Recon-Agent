"""Tests for the synthetic data generator.

These guard the invariants every downstream metric depends on: credits reconcile
to their ground-truth lines, no line is double-claimed, and traps are genuinely
unmatchable.
"""

from collections import Counter

import pytest

from src.fees import ROUNDING_TOLERANCE_PAISE, compute_net_from_gross
from src.generator.generate import generate_dataset


@pytest.fixture(scope="module")
def ctx():
    return generate_dataset(records=400, seed=99)


def test_tier_mix_is_controlled(ctx):
    tiers = Counter(g.difficulty for g in ctx.gts)
    assert tiers["easy"] == pytest.approx(400 * 0.45, abs=2)
    assert tiers["medium"] == pytest.approx(400 * 0.30, abs=2)
    assert tiers["hard"] == pytest.approx(400 * 0.20, abs=2)
    # trap is the remainder; must be non-trivial (~5%)
    assert 10 <= tiers["trap"] <= 40


def test_credit_reconciles_to_ground_truth_lines(ctx):
    net = {l.settlement_id: l.net_paise for l in ctx.lines}
    credit = {c.bank_txn_id: c.amount_paise for c in ctx.credits}
    for g in ctx.gts:
        if not g.settlement_ids:
            continue
        total = sum(net[s] for s in g.settlement_ids)
        assert abs(total - credit[g.bank_txn_id]) <= ROUNDING_TOLERANCE_PAISE, g


def test_no_settlement_line_claimed_twice(ctx):
    seen = set()
    for g in ctx.gts:
        for s in g.settlement_ids:
            assert s not in seen, f"line {s} claimed by two credits"
            seen.add(s)


def test_traps_are_unmatchable(ctx):
    traps = [g for g in ctx.gts if g.difficulty == "trap"]
    assert traps
    for g in traps:
        assert g.settlement_ids == [], "a trap must have no valid match"


def test_all_money_is_integer_paise(ctx):
    for l in ctx.lines:
        assert isinstance(l.net_paise, int)
        assert isinstance(l.gross_paise, int)
    for c in ctx.credits:
        assert isinstance(c.amount_paise, int)


def test_fee_model_matches_stored_sale_lines(ctx):
    # Every clean sale line's stored net equals a fresh fee recomputation
    # (except where rounding drift was deliberately injected).
    for l in ctx.lines:
        if l.line_type != "sale":
            continue
        _, _, expected_net = compute_net_from_gross(l.gross_paise)
        assert abs(expected_net - l.net_paise) <= 3  # <=3 paise injected drift


def test_trap_variety_present(ctx):
    tags = Counter(t for g in ctx.gts for t in g.corruption_tags)
    # At least the two batch-referencing traps and one standalone trap appear.
    assert tags["trap_unrelated"] >= 1
    assert tags["trap_out_of_window"] >= 1 or tags["trap_dup_utr"] >= 1
