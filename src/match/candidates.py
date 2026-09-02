"""Candidate generation — pure DuckDB, zero LLM calls.

For each bank credit we narrow ~900 settlement batches down to a handful of
plausible ones using three cheap, provable filters:

1. UTR: extract every UTR-shaped token from the narration (multiple regex
   patterns, keep them all) and match against batch payout references.
2. Date: batch settlement date within [value_date - 4d, value_date].
3. Amount: batch net total within ±0.5% of the credit, with a ±500 paise floor
   so tiny credits still get a usable band.

This is constraint satisfaction over a flat set — range predicates on indexed
columns — which is exactly what an indexed DuckDB scan is good at. No embeddings
(amount tolerance is arithmetic, not similarity); no graph (no multi-hop).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb

from src.models import BankCredit, SettlementLine

DATE_LOOKBACK_DAYS = 4
AMOUNT_REL_TOL = 0.005  # ±0.5%
AMOUNT_ABS_FLOOR_PAISE = 500
SUBSET_POOL_CAP = 60  # bound the subset-sum line pool (Phase 4c)

# UTR tokens: 3-5 letter bank prefix, optional N, then 8-16 digits (full UTR).
_STRONG_UTR = re.compile(r"[A-Z]{3,5}N?\d{8,16}")
# Bare digit runs (>=6) recover truncated UTR tails from narration.
_DIGIT_RUN = re.compile(r"\d{6,}")


def extract_utrs(narration: str, explicit_utr: str | None) -> tuple[set[str], list[str]]:
    """Return (strong tokens, weak digit-run tokens) recovered from a narration."""
    up = (narration or "").upper()
    strong = set(_STRONG_UTR.findall(up))
    if explicit_utr:
        strong.add(explicit_utr.upper())
    weak = [t for t in _DIGIT_RUN.findall(up)]
    return strong, weak


def _ref_strength(payout_ref: str | None, strong: set[str], weak: list[str]) -> float:
    """1.0 exact reference match, 0.6 truncated-suffix match, 0.0 none."""
    if not payout_ref:
        return 0.0
    p = payout_ref.upper()
    if p in strong:
        return 1.0
    for t in weak:
        if len(t) >= 6 and p.endswith(t):
            return 0.6
    return 0.0


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #


@dataclass
class Candidate:
    batch_id: str
    net_total_paise: int
    settled_date: date
    payout_ref: str | None
    ref_strength: float
    matched_by: tuple[str, ...]  # subset of ("utr", "amount_date")
    line_ids: list[str]


@dataclass
class CreditCandidates:
    bank_txn_id: str
    amount_paise: int
    value_date: date
    strong_utrs: list[str]
    candidates: list[Candidate]
    latency_ms: float
    # subset-sum line pool: window lines not tied to any UTR-matched batch
    pool_line_ids: list[str] = field(default_factory=list)
    # raw bank narration, carried through for the Phase-6 adjudicator's evidence
    narration: str = ""


# --------------------------------------------------------------------------- #
# Index over batches + lines
# --------------------------------------------------------------------------- #


class CandidateIndex:
    """Holds an in-memory DuckDB index of batches and lines for blocking."""

    def __init__(self, settlements: list[SettlementLine]):
        # Aggregate lines into batches.
        self._batch_lines: dict[str, list[str]] = {}
        self._batch_ref: dict[str, str | None] = {}
        agg: dict[str, dict] = {}
        self.line_net: dict[str, int] = {}
        self.line_batch: dict[str, str] = {}
        self.line_type: dict[str, str] = {}  # for adjudicator evidence (Phase 6)
        for l in settlements:
            self.line_type[l.settlement_id] = l.line_type
            b = agg.setdefault(
                l.payout_batch_id,
                {"net": 0, "date": l.settled_at.date(), "ref": None},
            )
            b["net"] += l.net_paise
            b["date"] = min(b["date"], l.settled_at.date())
            if b["ref"] is None and l.payout_ref:
                b["ref"] = l.payout_ref
            self._batch_lines.setdefault(l.payout_batch_id, []).append(l.settlement_id)
            self.line_net[l.settlement_id] = l.net_paise
            self.line_batch[l.settlement_id] = l.payout_batch_id

        self.n_batches = len(agg)
        self._ref_to_batch: dict[str, str] = {}
        batch_rows = []
        for bid, b in agg.items():
            self._batch_ref[bid] = b["ref"]
            if b["ref"]:
                self._ref_to_batch[b["ref"].upper()] = bid
            batch_rows.append((bid, int(b["net"]), b["date"], b["ref"]))
        self._batch_meta = {r[0]: r for r in batch_rows}

        line_rows = [
            (l.settlement_id, l.payout_batch_id, l.net_paise, l.settled_at.date())
            for l in settlements
        ]

        self.con = duckdb.connect(":memory:")
        self.con.execute(
            "CREATE TABLE batches(batch_id VARCHAR, net_total BIGINT, settled_date DATE, payout_ref VARCHAR)"
        )
        self.con.executemany("INSERT INTO batches VALUES (?,?,?,?)", batch_rows)
        self.con.execute(
            "CREATE TABLE lines(settlement_id VARCHAR, batch_id VARCHAR, net_paise BIGINT, settled_date DATE)"
        )
        self.con.executemany("INSERT INTO lines VALUES (?,?,?,?)", line_rows)
        # Indexes on the columns we range-scan.
        self.con.execute("CREATE INDEX idx_b_date ON batches(settled_date)")
        self.con.execute("CREATE INDEX idx_b_net ON batches(net_total)")
        self.con.execute("CREATE INDEX idx_l_date ON lines(settled_date)")
        self.con.execute("CREATE INDEX idx_l_net ON lines(net_paise)")

    # -- helpers -----------------------------------------------------------

    def batch_line_ids(self, batch_id: str) -> list[str]:
        return self._batch_lines.get(batch_id, [])

    def batch_for_ref(self, ref: str) -> str | None:
        """Batch whose payout_ref == ref (case-insensitive), or None. Used by the
        Phase-7 learned-rule layer to resolve a reference the base extractor missed."""
        return self._ref_to_batch.get(ref.upper())

    def _amount_band(self, amount: int) -> tuple[int, int]:
        band = max(int(round(amount * AMOUNT_REL_TOL)), AMOUNT_ABS_FLOOR_PAISE)
        return amount - band, amount + band

    # -- per-credit candidate generation -----------------------------------

    def for_credit(self, credit: BankCredit) -> CreditCandidates:
        t0 = time.perf_counter()
        strong, weak = extract_utrs(credit.narration, credit.utr)

        lo_amt, hi_amt = self._amount_band(credit.amount_paise)
        lo_date = credit.value_date - timedelta(days=DATE_LOOKBACK_DAYS)
        hi_date = credit.value_date

        # (2)+(3): indexed range scan for amount+date candidate batches.
        rows = self.con.execute(
            "SELECT batch_id, net_total, settled_date, payout_ref FROM batches "
            "WHERE settled_date BETWEEN ? AND ? AND net_total BETWEEN ? AND ?",
            [lo_date, hi_date, lo_amt, hi_amt],
        ).fetchall()

        chosen: dict[str, Candidate] = {}
        for bid, net_total, sdate, pref in rows:
            chosen[bid] = Candidate(
                batch_id=bid,
                net_total_paise=int(net_total),
                settled_date=sdate,
                payout_ref=pref,
                ref_strength=_ref_strength(pref, strong, weak),
                matched_by=("amount_date",),
                line_ids=self.batch_line_ids(bid),
            )

        # (1): UTR-matched batches (exact via dict; suffix scan only if needed).
        utr_batches: set[str] = set()
        for tok in strong:
            bid = self._ref_to_batch.get(tok)
            if bid:
                utr_batches.add(bid)
        if weak:
            for ref, bid in self._ref_to_batch.items():
                if any(len(t) >= 6 and ref.endswith(t) for t in weak):
                    utr_batches.add(bid)

        for bid in utr_batches:
            bid_, net_total, sdate, pref = self._batch_meta[bid]
            if bid in chosen:
                c = chosen[bid]
                c.matched_by = tuple(sorted(set(c.matched_by) | {"utr"}))
            else:
                chosen[bid] = Candidate(
                    batch_id=bid,
                    net_total_paise=int(net_total),
                    settled_date=sdate,
                    payout_ref=pref,
                    ref_strength=_ref_strength(pref, strong, weak),
                    matched_by=("utr",),
                    line_ids=self.batch_line_ids(bid),
                )

        # Subset-sum line pool: lines in the date window with net <= amount+floor,
        # nearest to the credit first, capped. Used only when no exact/fee match.
        pool = self.con.execute(
            "SELECT settlement_id FROM lines "
            "WHERE settled_date BETWEEN ? AND ? AND net_paise > 0 AND net_paise <= ? "
            "ORDER BY abs(net_paise - ?) LIMIT ?",
            [lo_date, hi_date, hi_amt, credit.amount_paise, SUBSET_POOL_CAP],
        ).fetchall()

        latency = (time.perf_counter() - t0) * 1000
        return CreditCandidates(
            bank_txn_id=credit.bank_txn_id,
            amount_paise=credit.amount_paise,
            value_date=credit.value_date,
            strong_utrs=sorted(strong),
            candidates=sorted(chosen.values(), key=lambda c: -c.ref_strength),
            latency_ms=latency,
            pool_line_ids=[r[0] for r in pool],
            narration=credit.narration or "",
        )

    def close(self) -> None:
        self.con.close()
