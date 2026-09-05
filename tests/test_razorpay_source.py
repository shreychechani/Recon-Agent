"""Razorpay ingestion: the pure recon->models mapping, the networked fetch seam
(with an injected fake client), and an end-to-end reconcile of live-shaped data."""

from __future__ import annotations

import json
from pathlib import Path

from src.ingest.razorpay_source import (
    fetch_recon,
    recon_items_to_models,
)
from src.pipeline import reconcile

SAMPLE = Path("data/razorpay_recon_sample.json")


def _items() -> list[dict]:
    return json.loads(SAMPLE.read_text())["items"]


def test_mapping_shapes_and_money():
    orders, settlements, credits, truth = recon_items_to_models(_items())

    # 8 recon lines -> 8 settlement lines across 3 payout batches -> 3 bank credits.
    assert len(settlements) == 8
    assert len({s.payout_batch_id for s in settlements}) == 3
    assert len(credits) == 3
    assert len(truth) == 3

    # Every payout batch's UTR became the line's payout_ref (the matchable reference).
    assert all(s.payout_ref for s in settlements)

    # The refund line is negative and reduces its batch's net (Batch B: 195280+73230-30000).
    refunds = [s for s in settlements if s.line_type == "refund"]
    assert len(refunds) == 1 and refunds[0].net_paise == -30000
    by_batch = {}
    for s in settlements:
        by_batch.setdefault(s.payout_batch_id, 0)
        by_batch[s.payout_batch_id] += s.net_paise
    b = next(c for c in credits if c.amount_paise == 238510)
    assert b.narration.startswith("RAZORPAY SETTLEMENT") and b.utr in b.narration


def test_ground_truth_matches_batch_composition():
    _, settlements, credits, truth = recon_items_to_models(_items())
    lines_by_batch: dict[str, set[str]] = {}
    # reconstruct batch -> its line ids to compare against the emitted ground truth
    for s in settlements:
        lines_by_batch.setdefault(s.payout_batch_id, set()).add(s.settlement_id)
    # each credit's truth is exactly the set of settlement lines composing its batch
    for gt in truth:
        assert set(gt.settlement_ids) in lines_by_batch.values()
        assert gt.corruption_tags == ["razorpay_live"]


def test_live_shaped_data_reconciles_at_full_precision():
    orders, settlements, credits, truth = recon_items_to_models(_items())
    truth_by_id = {g.bank_txn_id: set(g.settlement_ids) for g in truth}

    results = reconcile(orders, settlements, credits)

    assert len(results) == len(credits)
    for r in results:
        # clean live data resolves deterministically, no LLM, no mistake
        assert r.decision == "matched", f"{r.bank_txn_id} not matched: {r.decision}"
        assert set(r.settlement_ids) == truth_by_id[r.bank_txn_id]
        assert r.resolved_by == "exact_ref"


def test_settled_false_items_are_skipped():
    items = _items()
    items.append({**items[0], "entity_id": "pay_UNSETTLED", "settled": False,
                  "settlement_id": "setl_PENDING", "settlement_utr": None})
    _, settlements, _, _ = recon_items_to_models(items)
    assert all(s.settlement_id != "pay_UNSETTLED" for s in settlements)


def test_fetch_recon_paginates_with_injected_client():
    """The network seam is injectable: a fake client returns two pages, and fetch_recon
    stitches them together and stops when a short page arrives."""
    pages = [
        {"items": [{"entity_id": f"e{i}"} for i in range(1000)]},  # full page -> keep going
        {"items": [{"entity_id": "eLast"}]},                        # short page -> stop
    ]
    calls: list[dict] = []

    class FakeSettlement:
        def report(self, params):
            calls.append(params)
            return pages[len(calls) - 1]

    class FakeClient:
        settlement = FakeSettlement()

    items = fetch_recon(2026, 8, client=FakeClient())
    assert len(items) == 1001
    assert calls[0]["skip"] == 0 and calls[1]["skip"] == 1000
    assert calls[0]["year"] == 2026 and calls[0]["month"] == 8
