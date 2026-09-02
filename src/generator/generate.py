"""Synthetic data generator — the foundation of the whole project.

If the fake data is not realistically messy, every downstream metric is
meaningless. This module produces three files that describe the same money from
three angles, in three different on-disk formats, plus a separate ground-truth
file the matcher must never read.

    python -m src.generator.generate --records 800 --seed 42 --out data/generated/train/

Design notes that matter for the matcher:

* Money is integer paise everywhere in memory. Rupee strings only appear when we
  WRITE the messy files (that is the external representation ingest must undo).
* A "record" is one bank credit — the thing we reconcile. ``--records`` counts
  credits. Non-trap credits each map to one payout batch of 15-60 settlement
  lines; split (1:N) and near-duplicate scenarios emit two credits.
* Traps are generated last so they can reference already-created batches (the
  reused-UTR and subset-of-matched traps depend on that).
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook

from src.fees import compute_net_from_gross
from src.generator import corruptions as C
from src.generator import narration as N
from src.models import BankCredit, GroundTruth, Order, SettlementLine

BASE_DATE = date(2026, 1, 6)
SETTLEMENT_SPAN_DAYS = 180
UTR_TRANSPOSE_RATE = 0.02  # ~2% of credits get a keying error in the UTR

TIER_MIX = {"easy": 0.45, "medium": 0.30, "hard": 0.20}  # trap = remainder


# --------------------------------------------------------------------------- #
# Generation context — accumulates everything, hands out IDs
# --------------------------------------------------------------------------- #


@dataclass
class MatchableBatch:
    """A real, matchable batch recorded so traps can reference it."""

    batch_id: str
    lines: list[SettlementLine]
    settlement_date: date
    value_date: date
    utr: str
    amount_paise: int
    credit_id: str


@dataclass
class GenContext:
    rng: random.Random
    orders: list[Order] = field(default_factory=list)
    lines: list[SettlementLine] = field(default_factory=list)
    credits: list[BankCredit] = field(default_factory=list)
    gts: list[GroundTruth] = field(default_factory=list)
    matchable: list[MatchableBatch] = field(default_factory=list)
    utrs_seen: set[str] = field(default_factory=set)
    _order_n: int = 0
    _stl_n: int = 0
    _batch_n: int = 0
    _txn_n: int = 0

    def next_order_id(self) -> str:
        self._order_n += 1
        return f"ORD-2026-{self._order_n:06d}"

    def next_stl_id(self) -> str:
        self._stl_n += 1
        return f"STL-{self._stl_n:06d}"

    def next_batch_id(self) -> str:
        self._batch_n += 1
        return f"BATCH-{self._batch_n:05d}"

    def next_txn_id(self) -> str:
        self._txn_n += 1
        return f"TXN-{self._txn_n:06d}"

    def unique_utr(self) -> str:
        while True:
            utr = N.make_utr(self.rng)
            if utr not in self.utrs_seen:
                self.utrs_seen.add(utr)
                return utr


# --------------------------------------------------------------------------- #
# Low-level builders
# --------------------------------------------------------------------------- #


def _rand_settlement_date(rng: random.Random) -> date:
    return BASE_DATE + timedelta(days=rng.randint(0, SETTLEMENT_SPAN_DAYS))


def _dt_on(d: date, rng: random.Random) -> datetime:
    return datetime.combine(d, time(rng.randint(9, 20), rng.randint(0, 59), rng.randint(0, 59)))


def _rand_gross_paise(rng: random.Random) -> int:
    """A plausible order value. Mostly whole rupees; occasionally odd paise."""
    rupees = rng.randint(150, 40000)
    paise = rupees * 100
    if rng.random() < 0.15:
        paise += rng.randint(1, 99)
    return paise


def _make_sale_line(
    ctx: GenContext,
    batch_id: str,
    utr: str,
    settled_dt: datetime,
    *,
    partial_refund: bool = False,
    drift: int = 0,
    gross_paise: int | None = None,
) -> SettlementLine:
    """Create one sale settlement line and its backing order.

    ``gross_paise`` may be supplied to reproduce a specific value (used by the
    near-duplicate scenario, which needs two batches of identical composition).
    """
    order_gross = gross_paise if gross_paise is not None else _rand_gross_paise(ctx.rng)

    # A partial refund makes the ORDER value differ from the settled gross:
    # the shop recorded the full order, but only the retained amount settled.
    if partial_refund:
        refunded = ctx.rng.randint(1, order_gross // 3)
        settled_gross = order_gross - refunded
        status = "partially_refunded"
    else:
        settled_gross = order_gross
        status = "captured"

    fee, gst, net = compute_net_from_gross(settled_gross)
    net += drift  # injected rounding drift (hard tier only)

    order_id = ctx.next_order_id()
    ctx.orders.append(
        Order(
            order_id=order_id,
            amount_paise=order_gross,
            created_at=_dt_on(settled_dt.date() - timedelta(days=ctx.rng.randint(1, 6)), ctx.rng),
            status=status,
            customer_ref=f"CUST-{ctx.rng.randint(0, 999999):06d}",
        )
    )
    line = SettlementLine(
        settlement_id=ctx.next_stl_id(),
        payout_batch_id=batch_id,
        order_id=order_id,
        gross_paise=settled_gross,
        fee_paise=fee,
        gst_paise=gst,
        net_paise=net,
        line_type="sale",
        settled_at=settled_dt,
        payout_ref=utr,
    )
    ctx.lines.append(line)
    return line


def _make_negative_line(
    ctx: GenContext,
    batch_id: str,
    utr: str,
    settled_dt: datetime,
    line_type: str,
) -> SettlementLine:
    """A refund / chargeback line: a clawback with negative net."""
    amount = _rand_gross_paise(ctx.rng)
    # Reference an existing order where possible and flip its status.
    order_id = None
    for order in reversed(ctx.orders):
        if order.status == "captured":
            order_id = order.order_id
            order.status = "refunded" if line_type == "refund" else order.status
            break
    line = SettlementLine(
        settlement_id=ctx.next_stl_id(),
        payout_batch_id=batch_id,
        order_id=order_id,
        gross_paise=-amount,
        fee_paise=0,
        gst_paise=0,
        net_paise=-amount,
        line_type=line_type,  # type: ignore[arg-type]
        settled_at=settled_dt,
        payout_ref=utr,
    )
    ctx.lines.append(line)
    return line


def _apply_utr_corruptions(ctx: GenContext, narration: str, utr: str) -> tuple[str, list[str]]:
    """Maybe transpose the UTR inside the narration (~2% of records)."""
    tags: list[str] = []
    if utr and utr in narration and ctx.rng.random() < UTR_TRANSPOSE_RATE:
        bad = C.transpose_utr_chars(ctx.rng, utr)
        narration = narration.replace(utr, bad)
        tags.append("utr_transposed")
    return narration, tags


def _add_credit(
    ctx: GenContext,
    *,
    amount_paise: int,
    value_date: date,
    narration: str,
    difficulty: str,
    settlement_ids: list[str],
    tags: list[str],
    utr_field: str | None = None,
) -> BankCredit:
    credit = BankCredit(
        bank_txn_id=ctx.next_txn_id(),
        amount_paise=amount_paise,
        value_date=value_date,
        narration=narration,
        utr=utr_field,
    )
    ctx.credits.append(credit)
    ctx.gts.append(
        GroundTruth(
            bank_txn_id=credit.bank_txn_id,
            settlement_ids=settlement_ids,
            difficulty=difficulty,  # type: ignore[arg-type]
            corruption_tags=tags,
        )
    )
    return credit


# --------------------------------------------------------------------------- #
# Tier builders — each returns the number of credits it emitted
# --------------------------------------------------------------------------- #


def build_easy(ctx: GenContext) -> int:
    rng = ctx.rng
    batch_id = ctx.next_batch_id()
    utr = ctx.unique_utr()
    settlement_date = _rand_settlement_date(rng)
    settled_dt = _dt_on(settlement_date, rng)
    size = rng.randint(15, 60)

    lines = [_make_sale_line(ctx, batch_id, utr, settled_dt) for _ in range(size)]
    amount = sum(l.net_paise for l in lines)
    value_date = settlement_date + timedelta(days=2)  # exactly T+2

    ddmm = value_date.strftime("%d%m")
    narration = N.render_narration(rng, utr, ddmm, "clean")
    narration, tags = _apply_utr_corruptions(ctx, narration, utr)

    # Occasionally the bank also populates a dedicated UTR field.
    utr_field = utr if rng.random() < 0.15 else None

    credit = _add_credit(
        ctx,
        amount_paise=amount,
        value_date=value_date,
        narration=narration,
        difficulty="easy",
        settlement_ids=[l.settlement_id for l in lines],
        tags=tags,
        utr_field=utr_field,
    )
    ctx.matchable.append(
        MatchableBatch(batch_id, lines, settlement_date, value_date, utr, amount, credit.bank_txn_id)
    )
    return 1


def build_medium(ctx: GenContext) -> int:
    rng = ctx.rng
    features = rng.sample(
        ["refund_netted", "date_drift", "utr_in_noise", "partial_refund"],
        k=rng.randint(1, 2),
    )
    tags = list(features)

    batch_id = ctx.next_batch_id()
    utr = ctx.unique_utr()
    settlement_date = _rand_settlement_date(rng)
    settled_dt = _dt_on(settlement_date, rng)
    size = rng.randint(15, 60)

    partial = "partial_refund" in features
    lines = [
        _make_sale_line(ctx, batch_id, utr, settled_dt, partial_refund=partial and i % 7 == 0)
        for i in range(size)
    ]
    if "refund_netted" in features:
        lines.append(_make_negative_line(ctx, batch_id, utr, settled_dt, "refund"))

    amount = sum(l.net_paise for l in lines)

    if "date_drift" in features:
        drift_days = rng.choice([1, 3])
        tags.append(f"date_drift_t{drift_days}")
    else:
        drift_days = 2
    value_date = settlement_date + timedelta(days=drift_days)

    ddmm = value_date.strftime("%d%m")
    exposure = "noisy" if "utr_in_noise" in features else "clean"
    narration = N.render_narration(rng, utr, ddmm, exposure)
    if exposure == "noisy":
        narration = C.add_whitespace_noise(rng, C.lowercase_sometimes(rng, narration))
    narration = C.truncate_to(narration, 40)
    narration, extra = _apply_utr_corruptions(ctx, narration, utr)
    tags += extra

    credit = _add_credit(
        ctx,
        amount_paise=amount,
        value_date=value_date,
        narration=narration,
        difficulty="medium",
        settlement_ids=[l.settlement_id for l in lines],
        tags=tags,
        utr_field=None,
    )
    ctx.matchable.append(
        MatchableBatch(batch_id, lines, settlement_date, value_date, utr, amount, credit.bank_txn_id)
    )
    return 1


def build_hard(ctx: GenContext) -> int:
    """Pick one hard scenario. Each combines 2+ corruptions."""
    scenario = ctx.rng.choice(
        ["chargeback_drift", "split_1n", "near_dup", "utr_absent_cb", "truncated_refund"]
    )
    return _HARD_SCENARIOS[scenario](ctx)


def _hard_chargeback_drift(ctx: GenContext) -> int:
    # chargeback debited mid-payout + rounding drift; UTR truncated in narration.
    rng = ctx.rng
    batch_id = ctx.next_batch_id()
    utr = ctx.unique_utr()
    settlement_date = _rand_settlement_date(rng)
    settled_dt = _dt_on(settlement_date, rng)
    size = rng.randint(15, 60)

    lines = [_make_sale_line(ctx, batch_id, utr, settled_dt) for _ in range(size)]
    # inject rounding drift on exactly one line
    drift = C.rounding_drift_paise(rng)
    lines[0].net_paise += drift
    lines.append(_make_negative_line(ctx, batch_id, utr, settled_dt, "chargeback"))

    amount = sum(l.net_paise for l in lines)
    value_date = settlement_date + timedelta(days=rng.choice([1, 2, 3]))
    ddmm = value_date.strftime("%d%m")
    narration = N.render_narration(rng, utr, ddmm, "truncated")

    _add_credit(
        ctx,
        amount_paise=amount,
        value_date=value_date,
        narration=narration,
        difficulty="hard",
        settlement_ids=[l.settlement_id for l in lines],
        tags=["chargeback", "rounding_drift", "utr_truncated"],
        utr_field=None,
    )
    ctx.matchable.append(
        MatchableBatch(batch_id, lines, settlement_date, value_date, utr, amount, ctx.credits[-1].bank_txn_id)
    )
    return 1


def _hard_split_1n(ctx: GenContext) -> int:
    # one batch settled as TWO bank credits (1:N) + rounding drift. UTR kept clean
    # so candidate gen can find the batch; subset-sum recovers each subset.
    rng = ctx.rng
    batch_id = ctx.next_batch_id()
    utr = ctx.unique_utr()
    settlement_date = _rand_settlement_date(rng)
    settled_dt = _dt_on(settlement_date, rng)
    size = rng.randint(20, 60)

    lines = [_make_sale_line(ctx, batch_id, utr, settled_dt) for _ in range(size)]
    lines[0].net_paise += C.rounding_drift_paise(rng)

    split = rng.randint(size // 3, 2 * size // 3)
    part_a, part_b = lines[:split], lines[split:]
    value_date = settlement_date + timedelta(days=2)
    ddmm = value_date.strftime("%d%m")

    for part in (part_a, part_b):
        amount = sum(l.net_paise for l in part)
        narration = N.render_narration(rng, utr, ddmm, "clean")
        _add_credit(
            ctx,
            amount_paise=amount,
            value_date=value_date,
            narration=narration,
            difficulty="hard",
            settlement_ids=[l.settlement_id for l in part],
            tags=["split_1n", "rounding_drift"],
            utr_field=None,
        )
    # Record the full batch so subset-sum has the line pool available via UTR.
    ctx.matchable.append(
        MatchableBatch(batch_id, lines, settlement_date, value_date, utr, sum(l.net_paise for l in lines), ctx.credits[-1].bank_txn_id)
    )
    return 2


def _hard_near_dup(ctx: GenContext) -> int:
    # two batches, same date, IDENTICAL net totals, both UTR-absent narration.
    # Genuinely ambiguous: the correct behaviour is to abstain, not guess.
    rng = ctx.rng
    settlement_date = _rand_settlement_date(rng)
    settled_dt = _dt_on(settlement_date, rng)
    value_date = settlement_date + timedelta(days=2)
    ddmm = value_date.strftime("%d%m")

    made = 0
    # Both batches share the SAME set of gross amounts, so their net totals are
    # identical (and every line stays fee-consistent). Same day, no UTR -> the
    # correct behaviour downstream is to abstain, not guess.
    size = rng.randint(15, 40)
    shared_grosses = [_rand_gross_paise(rng) for _ in range(size)]
    for _ in range(2):
        batch_id = ctx.next_batch_id()
        utr = ctx.unique_utr()
        lines = [_make_sale_line(ctx, batch_id, utr, settled_dt, gross_paise=g) for g in shared_grosses]
        amount = sum(l.net_paise for l in lines)
        narration = N.render_narration(rng, utr, ddmm, "absent")
        _add_credit(
            ctx,
            amount_paise=amount,
            value_date=value_date,
            narration=narration,
            difficulty="hard",
            settlement_ids=[l.settlement_id for l in lines],
            tags=["near_dup_ambiguous", "utr_absent"],
            utr_field=None,
        )
        ctx.matchable.append(
            MatchableBatch(batch_id, lines, settlement_date, value_date, utr, amount, ctx.credits[-1].bank_txn_id)
        )
        made += 1
    return made


def _hard_utr_absent_cb(ctx: GenContext) -> int:
    # UTR absent + chargeback -> amount+date reasoning only.
    rng = ctx.rng
    batch_id = ctx.next_batch_id()
    utr = ctx.unique_utr()
    settlement_date = _rand_settlement_date(rng)
    settled_dt = _dt_on(settlement_date, rng)
    size = rng.randint(15, 60)
    lines = [_make_sale_line(ctx, batch_id, utr, settled_dt) for _ in range(size)]
    lines.append(_make_negative_line(ctx, batch_id, utr, settled_dt, "chargeback"))
    amount = sum(l.net_paise for l in lines)
    value_date = settlement_date + timedelta(days=rng.choice([1, 2, 3]))
    narration = N.render_narration(rng, utr, value_date.strftime("%d%m"), "absent")
    _add_credit(
        ctx,
        amount_paise=amount,
        value_date=value_date,
        narration=narration,
        difficulty="hard",
        settlement_ids=[l.settlement_id for l in lines],
        tags=["utr_absent", "chargeback"],
        utr_field=None,
    )
    ctx.matchable.append(
        MatchableBatch(batch_id, lines, settlement_date, value_date, utr, amount, ctx.credits[-1].bank_txn_id)
    )
    return 1


def _hard_truncated_refund(ctx: GenContext) -> int:
    # UTR truncated + refund netted + rounding drift.
    rng = ctx.rng
    batch_id = ctx.next_batch_id()
    utr = ctx.unique_utr()
    settlement_date = _rand_settlement_date(rng)
    settled_dt = _dt_on(settlement_date, rng)
    size = rng.randint(15, 60)
    lines = [_make_sale_line(ctx, batch_id, utr, settled_dt) for _ in range(size)]
    lines[0].net_paise += C.rounding_drift_paise(rng)
    lines.append(_make_negative_line(ctx, batch_id, utr, settled_dt, "refund"))
    amount = sum(l.net_paise for l in lines)
    value_date = settlement_date + timedelta(days=rng.choice([1, 2, 3]))
    narration = N.render_narration(rng, utr, value_date.strftime("%d%m"), "truncated")
    _add_credit(
        ctx,
        amount_paise=amount,
        value_date=value_date,
        narration=narration,
        difficulty="hard",
        settlement_ids=[l.settlement_id for l in lines],
        tags=["utr_truncated", "refund_netted", "rounding_drift"],
        utr_field=None,
    )
    ctx.matchable.append(
        MatchableBatch(batch_id, lines, settlement_date, value_date, utr, amount, ctx.credits[-1].bank_txn_id)
    )
    return 1


_HARD_SCENARIOS = {
    "chargeback_drift": _hard_chargeback_drift,
    "split_1n": _hard_split_1n,
    "near_dup": _hard_near_dup,
    "utr_absent_cb": _hard_utr_absent_cb,
    "truncated_refund": _hard_truncated_refund,
}


# --------------------------------------------------------------------------- #
# Traps — the most important records. GT settlement_ids is always empty.
# --------------------------------------------------------------------------- #


def build_trap(ctx: GenContext) -> int:
    rng = ctx.rng
    kinds = ["dup_utr", "unrelated", "subset_of_matched", "out_of_window"]
    # subset/dup need existing matchable batches; fall back to unrelated early on.
    if not ctx.matchable:
        kind = "unrelated"
    else:
        kind = rng.choice(kinds)
    return _TRAP_KINDS[kind](ctx)


def _trap_dup_utr(ctx: GenContext) -> int:
    # A duplicate posting reusing a real credit's UTR and amount, one day later.
    # The batch is a genuine candidate, so only one-to-one assignment (not UTR
    # alone) can refuse it. GT is empty.
    rng = ctx.rng
    src = rng.choice(ctx.matchable)
    narration = N.render_narration(rng, src.utr, src.value_date.strftime("%d%m"), "clean")
    _add_credit(
        ctx,
        amount_paise=src.amount_paise,
        value_date=src.value_date + timedelta(days=1),
        narration=narration,
        difficulty="trap",
        settlement_ids=[],
        tags=["trap_dup_utr"],
        utr_field=None,
    )
    return 1


def _trap_unrelated(ctx: GenContext) -> int:
    # Salary / vendor inbound transfer in a plausible amount+date window. Round
    # rupee amount so it is very unlikely to equal any fee-adjusted net sum.
    rng = ctx.rng
    settlement_date = _rand_settlement_date(rng)
    value_date = settlement_date + timedelta(days=2)
    amount = rng.randint(20, 200) * 1000 * 100  # round tens-of-thousands of rupees
    if rng.random() < 0.5:
        narration = N.render_salary_narration(rng, value_date.strftime("%d%m"))
    else:
        narration = N.render_vendor_refund_narration(rng, value_date.strftime("%d%m"))
    _add_credit(
        ctx,
        amount_paise=amount,
        value_date=value_date,
        narration=narration,
        difficulty="trap",
        settlement_ids=[],
        tags=["trap_unrelated"],
        utr_field=None,
    )
    return 1


def _trap_subset_of_matched(ctx: GenContext) -> int:
    # The sharpest trap: amount equals a subset-sum of a matched batch's lines.
    # Those lines get consumed by their batch's exact match, so a correct system
    # finds no free subset and refuses. Greedy line-level subset-sum falls for it.
    rng = ctx.rng
    candidates = [m for m in ctx.matchable if len([l for l in m.lines if l.net_paise > 0]) >= 4]
    if not candidates:
        return _trap_unrelated(ctx)
    src = rng.choice(candidates)
    positive = [l for l in src.lines if l.net_paise > 0]
    subset = rng.sample(positive, k=rng.randint(2, 4))
    amount = sum(l.net_paise for l in subset)
    value_date = src.value_date  # within the batch's window on purpose
    narration = N.render_narration(rng, src.utr, value_date.strftime("%d%m"), "absent")
    _add_credit(
        ctx,
        amount_paise=amount,
        value_date=value_date,
        narration=narration,
        difficulty="trap",
        settlement_ids=[],
        tags=["trap_subset_of_matched"],
        utr_field=None,
    )
    return 1


def _trap_out_of_window(ctx: GenContext) -> int:
    # Credit dated well outside any valid settlement window -> no candidates.
    rng = ctx.rng
    settlement_date = _rand_settlement_date(rng)
    value_date = settlement_date + timedelta(days=rng.randint(15, 40))
    amount = _rand_gross_paise(rng) * rng.randint(15, 40)
    narration = N.render_narration(rng, ctx.unique_utr(), value_date.strftime("%d%m"), "clean")
    _add_credit(
        ctx,
        amount_paise=amount,
        value_date=value_date,
        narration=narration,
        difficulty="trap",
        settlement_ids=[],
        tags=["trap_out_of_window"],
        utr_field=None,
    )
    return 1


_TRAP_KINDS = {
    "dup_utr": _trap_dup_utr,
    "unrelated": _trap_unrelated,
    "subset_of_matched": _trap_subset_of_matched,
    "out_of_window": _trap_out_of_window,
}


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def generate_dataset(records: int, seed: int) -> GenContext:
    ctx = GenContext(rng=random.Random(seed))

    n_easy = round(records * TIER_MIX["easy"])
    n_medium = round(records * TIER_MIX["medium"])
    n_hard = round(records * TIER_MIX["hard"])
    n_trap = records - n_easy - n_medium - n_hard

    _fill(ctx, build_easy, n_easy)
    _fill(ctx, build_medium, n_medium)
    _fill(ctx, build_hard, n_hard)

    # A handful of failed orders that never settle, so the order ledger is a
    # superset of what appears in settlements (realistic).
    for _ in range(max(1, len(ctx.orders) // 10)):
        d = _rand_settlement_date(ctx.rng)
        ctx.orders.append(
            Order(
                order_id=ctx.next_order_id(),
                amount_paise=_rand_gross_paise(ctx.rng),
                created_at=_dt_on(d, ctx.rng),
                status="failed",
                customer_ref=f"CUST-{ctx.rng.randint(0, 999999):06d}",
            )
        )

    _fill(ctx, build_trap, n_trap)  # traps last: they reference real batches
    return ctx


def _fill(ctx: GenContext, builder, target: int) -> None:
    made = 0
    while made < target:
        made += builder(ctx)


# --------------------------------------------------------------------------- #
# File writers — the three different on-disk formats
# --------------------------------------------------------------------------- #


def _paise_to_rupees(paise: int, *, commas: bool = False) -> str:
    rupees = Decimal(paise) / Decimal(100)
    return f"{rupees:,.2f}" if commas else f"{rupees:.2f}"


def write_orders_xlsx(path: Path, orders: list[Order]) -> None:
    """orders.xlsx — two junk header rows above the real header; dates DD/MM/YYYY."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Orders"
    ws.append(["Acme Merchant Pvt Ltd — Order Ledger Export", "", "", "", ""])
    ws.append([f"Generated {datetime.now():%Y-%m-%d} | CONFIDENTIAL — internal use", "", "", "", ""])
    ws.append(["Order ID", "Order Value (INR)", "Order Date", "Order Status", "Customer"])
    for o in orders:
        ws.append(
            [
                o.order_id,
                _paise_to_rupees(o.amount_paise),
                o.created_at.strftime("%d/%m/%Y"),
                o.status,
                o.customer_ref,
            ]
        )
    wb.save(path)


def write_settlements_csv(path: Path, lines: list[SettlementLine]) -> None:
    """settlements.csv — non-obvious column names; dates DD-MMM-YY; rupee strings."""
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            ["sett_id", "batch", "order_ref", "txn_amt_inr", "comm_amt",
             "tax_on_comm", "net_credit", "ln_type", "settled_on", "utr_no"]
        )
        for l in lines:
            w.writerow(
                [
                    l.settlement_id,
                    l.payout_batch_id,
                    l.order_id or "",
                    _paise_to_rupees(l.gross_paise),
                    _paise_to_rupees(l.fee_paise),
                    _paise_to_rupees(l.gst_paise),
                    _paise_to_rupees(l.net_paise),
                    l.line_type,
                    l.settled_at.strftime("%d-%b-%y"),
                    l.payout_ref or "",
                ]
            )


def write_bank_json(path: Path, credits: list[BankCredit]) -> None:
    """bank.json — nested under 'transactions'; amounts as comma strings; ISO dates."""
    payload = {
        "account": "RAZORPAY SETTLEMENT A/C ****4471",
        "currency": "INR",
        "transactions": [
            {
                "bank_txn_id": c.bank_txn_id,
                "amount": _paise_to_rupees(c.amount_paise, commas=True),
                "value_date": c.value_date.strftime("%Y-%m-%d"),
                "narration": c.narration,
                "utr": c.utr,
            }
            for c in credits
        ],
    }
    path.write_text(json.dumps(payload, indent=2))


def write_ground_truth(path: Path, gts: list[GroundTruth]) -> None:
    path.write_text(json.dumps([g.model_dump() for g in gts], indent=2))


def write_dataset(out_dir: Path, ctx: GenContext) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    # Shuffle the bank credits so trap/tier ordering is not a leak.
    order = list(range(len(ctx.credits)))
    ctx.rng.shuffle(order)
    credits = [ctx.credits[i] for i in order]
    gts = [ctx.gts[i] for i in order]

    write_orders_xlsx(out_dir / "orders.xlsx", ctx.orders)
    write_settlements_csv(out_dir / "settlements.csv", ctx.lines)
    write_bank_json(out_dir / "bank.json", credits)
    write_ground_truth(out_dir / "ground_truth.json", gts)


def _print_summary(ctx: GenContext, out_dir: Path) -> None:
    from collections import Counter

    tiers = Counter(g.difficulty for g in ctx.gts)
    tags = Counter(t for g in ctx.gts for t in g.corruption_tags)
    print(f"Wrote dataset to {out_dir}")
    print(f"  orders:            {len(ctx.orders):>6}")
    print(f"  settlement lines:  {len(ctx.lines):>6}")
    print(f"  bank credits:      {len(ctx.credits):>6}")
    print(f"  tiers: " + ", ".join(f"{k}={tiers[k]}" for k in ["easy", "medium", "hard", "trap"]))
    print("  corruption tags:")
    for tag, n in sorted(tags.items(), key=lambda kv: -kv[1]):
        print(f"    {tag:<24} {n}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthetic reconciliation data generator")
    ap.add_argument("--records", type=int, default=800, help="number of bank credits to emit")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    args = ap.parse_args()

    ctx = generate_dataset(args.records, args.seed)
    write_dataset(args.out, ctx)
    _print_summary(ctx, args.out)


if __name__ == "__main__":
    main()
