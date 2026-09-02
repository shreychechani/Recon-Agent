"""Tests for the ingest layer: money/date parsing, schema mapping, round-trip."""

import json
from pathlib import Path

import pytest

from src.ingest import schema_map
from src.ingest.loader import (
    load_dataset,
    parse_date,
    parse_dt,
    parse_money_to_paise,
)

SEEDS = Path("data/seeds")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("49,847.00", 4984700),
        ("498.47", 49847),
        ("-300.00", -30000),
        ("1,200", 120000),
        ("₹1,200.50", 120050),
        ("", 0),
        (None, 0),
    ],
)
def test_money_parsing_is_exact_paise(raw, expected):
    assert parse_money_to_paise(raw) == expected


@pytest.mark.parametrize(
    "raw,y,m,d",
    [
        ("15/03/2026", 2026, 3, 15),  # DD/MM/YYYY
        ("2026-03-15", 2026, 3, 15),  # YYYY-MM-DD
        ("06-Jan-26", 2026, 1, 6),    # DD-MMM-YY
        ("06-jan-26", 2026, 1, 6),    # lowercase month
    ],
)
def test_date_parsing_across_formats(raw, y, m, d):
    dt = parse_date(raw)
    assert (dt.year, dt.month, dt.day) == (y, m, d)


def test_schema_mapping_resolves_known_formats_without_llm():
    # Non-obvious settlement headers must still resolve deterministically.
    headers = ["sett_id", "batch", "order_ref", "txn_amt_inr", "comm_amt",
               "tax_on_comm", "net_credit", "ln_type", "settled_on", "utr_no"]
    m = schema_map._alias_match("settlements", headers)
    assert m["gross"] == "txn_amt_inr"
    assert m["net"] == "net_credit"
    assert m["fee"] == "comm_amt"
    assert m["settled_at"] == "settled_on"
    assert schema_map._unresolved_required("settlements", m) == []


def test_load_dataset_roundtrips_against_ground_truth():
    orders, settlements, credits = load_dataset(SEEDS)
    assert orders and settlements and credits

    net = {s.settlement_id: s.net_paise for s in settlements}
    cred = {c.bank_txn_id: c.amount_paise for c in credits}
    gts = {g["bank_txn_id"]: g for g in json.loads((SEEDS / "ground_truth.json").read_text())}

    checked = 0
    for tid, g in gts.items():
        if not g["settlement_ids"]:
            continue
        total = sum(net[s] for s in g["settlement_ids"])
        assert abs(total - cred[tid]) <= 5, (tid, total, cred[tid])
        checked += 1
    assert checked > 0


def test_all_ingested_money_is_int():
    _, settlements, credits = load_dataset(SEEDS)
    assert all(isinstance(s.net_paise, int) for s in settlements)
    assert all(isinstance(c.amount_paise, int) for c in credits)
