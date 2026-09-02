"""Canonical Pydantic data models for the reconciliation pipeline.

ALL MONEY IS INTEGER PAISE. There are no floats for money anywhere in this
codebase. Rupees->paise conversion happens exactly once, in the ingest layer
(``src/ingest/loader.py``), via ``Decimal``.

These models are the single source of truth for the shape of data flowing between
stages. The generator emits them; ingest reconstructs them from messy files; the
matcher consumes them; the eval harness scores against ``GroundTruth``.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Source records — the three views of the same money
# --------------------------------------------------------------------------- #


class Order(BaseModel):
    """A line from the merchant's order ledger — what the shop sold."""

    order_id: str  # "ORD-2026-004471"
    amount_paise: int  # gross order value
    created_at: datetime
    status: Literal["captured", "refunded", "partially_refunded", "failed"]
    customer_ref: str


class SettlementLine(BaseModel):
    """A line from the payment gateway's settlement file.

    ``net_paise`` is what the gateway says it is paying out for this line, net of
    fee and GST. It is NEGATIVE for refunds and chargebacks (money clawed back).
    Many lines share a ``payout_batch_id``; the batch sums to one bank credit.
    """

    settlement_id: str  # "STL-88213"
    payout_batch_id: str  # groups lines into one bank credit
    order_id: str | None  # None for adjustment lines
    gross_paise: int
    fee_paise: int  # 2% of gross
    gst_paise: int  # 18% of fee
    net_paise: int  # gross - fee - gst (negative for refunds / chargebacks)
    line_type: Literal["sale", "refund", "chargeback", "adjustment"]
    settled_at: datetime
    # Payout reference the gateway assigns to a batch; often echoed in the bank
    # narration as the UTR. Present on real settlement files; the matcher treats
    # it as the ground-truth reference to match against.
    payout_ref: str | None = None


class BankCredit(BaseModel):
    """A credit line from the bank statement — what actually landed."""

    bank_txn_id: str
    amount_paise: int  # the lump sum that actually landed
    value_date: date
    narration: str  # semi-structured, messy
    utr: str | None = None  # often only recoverable by parsing the narration


# --------------------------------------------------------------------------- #
# Ground truth — produced by the generator, NEVER seen by the matcher
# --------------------------------------------------------------------------- #

Difficulty = Literal["easy", "medium", "hard", "trap"]


class GroundTruth(BaseModel):
    """The correct answer for one bank credit.

    ``settlement_ids`` empty == unmatchable trap record. The matcher must never
    read this file; the eval harness reads it to score.
    """

    bank_txn_id: str
    settlement_ids: list[str]  # empty list == unmatchable trap record
    difficulty: Difficulty
    corruption_tags: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Matcher output contract (Phase 1) — every layer emits one of these per credit
# --------------------------------------------------------------------------- #

ResolvedBy = Literal[
    "exact_ref",
    "fee_adjusted",
    "subset_sum",
    "assignment",
    "llm",
    "learned_rule",
    "analyst",  # manual resolution in the exception queue (Phase 8)
    "none",  # abstained / no layer resolved it
]


class MatchResult(BaseModel):
    """The system's decision about one bank credit."""

    bank_txn_id: str
    decision: Literal["matched", "no_match", "abstain"]
    settlement_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0  # 0.0-1.0
    resolved_by: ResolvedBy = "none"
    evidence: dict = Field(default_factory=dict)  # candidates + rejection reasons
    llm_calls: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


# --------------------------------------------------------------------------- #
# LLM adjudication output (Phase 6) — validated structured output
# --------------------------------------------------------------------------- #


class RejectedCandidate(BaseModel):
    payout_batch_id: str
    reason: str


class Adjudication(BaseModel):
    """Structured, validated output of the single adjudication LLM call."""

    decision: Literal["match", "no_match", "abstain"]
    settlement_ids: list[str] = Field(default_factory=list)
    confidence: float
    reasoning: str  # cited to specific evidence
    rejected_candidates: list[RejectedCandidate] = Field(default_factory=list)
