"""Tests for DuckDB candidate generation: extraction, recall, blocking power."""

import json
from pathlib import Path

from src.ingest.loader import load_dataset
from src.match.candidates import CandidateIndex, extract_utrs

SEEDS = Path("data/seeds")


def test_utr_extraction_recovers_full_and_truncated():
    strong, weak = extract_utrs("NEFT-HDFCN043321819600-CR-MUM", None)
    assert "HDFCN043321819600" in strong
    # truncated tail shows up as a weak digit run
    strong2, weak2 = extract_utrs("UPI-SETTLEMENT-1503-21819600", None)
    assert any(t == "21819600" for t in weak2)


def test_no_utr_when_absent():
    strong, weak = extract_utrs("NEFT-INWARD-CREDIT", None)
    assert strong == set()


def test_explicit_utr_field_is_included():
    strong, _ = extract_utrs("RAZORPAY SETTLEMENT", "utibn123456789012")
    assert "UTIBN123456789012" in strong


def test_recall_and_blocking_on_seeds():
    orders, settlements, credits = load_dataset(SEEDS)
    idx = CandidateIndex(settlements)
    gts = {g["bank_txn_id"]: g for g in json.loads((SEEDS / "ground_truth.json").read_text())}

    total_cands = 0
    recall_hit = recall_total = 0
    for c in credits:
        cc = idx.for_credit(c)
        total_cands += len(cc.candidates)
        assert len(cc.candidates) < 10  # blocking keeps it small
        g = gts[c.bank_txn_id]
        if g["settlement_ids"]:
            recall_total += 1
            true_batches = {idx.line_batch[s] for s in g["settlement_ids"]}
            cand_batches = {x.batch_id for x in cc.candidates}
            if true_batches & cand_batches:
                recall_hit += 1

    # every matchable credit keeps at least one true batch as a candidate
    assert recall_hit == recall_total
    # strong reduction: far fewer candidates than batches
    assert total_cands / len(credits) < 5
    idx.close()


def test_out_of_window_trap_gets_no_candidates():
    orders, settlements, credits = load_dataset(SEEDS)
    idx = CandidateIndex(settlements)
    gts = {g["bank_txn_id"]: g for g in json.loads((SEEDS / "ground_truth.json").read_text())}
    by_id = {c.bank_txn_id: c for c in credits}
    for tid, g in gts.items():
        if "trap_out_of_window" in g["corruption_tags"]:
            cc = idx.for_credit(by_id[tid])
            assert cc.candidates == [], "an out-of-window trap must have no batch candidates"
    idx.close()
