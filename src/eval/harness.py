"""Eval harness — scores any matcher's output against ground truth.

Built BEFORE the matcher on purpose: you cannot tune a matcher you cannot score,
and the headline claim (auto-resolve rate at 100% precision) is only meaningful if
the scorer is trustworthy. The abstain-everything stub at the bottom exists to
prove the harness itself is correct before any real logic exists.

Correctness rules:
* decision == "matched"  is correct iff the returned settlement_ids EXACTLY equal
  the ground-truth set (and the record is genuinely matchable).
* decision == "no_match" is correct iff the record is a trap (empty ground truth).
* decision == "abstain"  is never scored for precision. It is coverage lost to the
  exception queue — a safe outcome, not a wrong one.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from src.models import GroundTruth, MatchResult

DECIDED = {"matched", "no_match"}


# --------------------------------------------------------------------------- #
# Metrics data model
# --------------------------------------------------------------------------- #


class TierMetrics(BaseModel):
    tier: str
    total: int
    decided: int
    correct: int
    coverage: float
    precision: float


class Metrics(BaseModel):
    total: int
    decided: int
    abstained: int
    matched: int
    no_match: int
    correct: int
    incorrect: int

    auto_resolve_rate: float  # coverage: decided / total
    precision: float  # correct / decided (1.0 when nothing decided)

    traps_total: int
    hallucinated_matches: int
    hallucinated_match_rate: float  # traps wrongly matched / traps

    per_tier: list[TierMetrics] = Field(default_factory=list)
    resolution_sources: dict[str, int] = Field(default_factory=dict)

    # cost / latency
    total_cost_usd: float = 0.0
    cost_per_record_usd: float = 0.0
    llm_calls_total: int = 0
    records_with_llm: int = 0
    records_deterministic: int = 0
    latency_p50_ms: float = 0.0
    latency_p99_ms: float = 0.0
    det_latency_p50_ms: float = 0.0
    det_latency_p99_ms: float = 0.0
    llm_latency_p50_ms: float = 0.0
    llm_latency_p99_ms: float = 0.0

    # for debugging: which decided records were wrong
    errors: list[dict] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    idx = q * (len(s) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(s) - 1)
    frac = idx - lo
    return s[lo] * (1 - frac) + s[hi] * frac


def is_correct(result: MatchResult, gt: GroundTruth) -> bool:
    """Is this DECIDED result correct? (abstains are never passed here)"""
    if result.decision == "no_match":
        return gt.settlement_ids == []
    if result.decision == "matched":
        return set(result.settlement_ids) == set(gt.settlement_ids) and gt.settlement_ids != []
    return False


def score(results: list[MatchResult], truth: list[GroundTruth]) -> Metrics:
    gt_by_id = {g.bank_txn_id: g for g in truth}
    if len(results) != len(truth):
        # not fatal, but worth surfacing — a matcher must decide on every credit
        missing = {g.bank_txn_id for g in truth} - {r.bank_txn_id for r in results}
        extra = {r.bank_txn_id for r in results} - {g.bank_txn_id for g in truth}
        raise ValueError(f"result/truth mismatch: missing={len(missing)} extra={len(extra)}")

    total = len(results)
    decided = correct = incorrect = matched = no_match = 0
    traps_total = sum(1 for g in truth if g.difficulty == "trap")
    hallucinated = 0
    errors: list[dict] = []

    tiers = ["easy", "medium", "hard", "trap"]
    tier_total = {t: 0 for t in tiers}
    tier_decided = {t: 0 for t in tiers}
    tier_correct = {t: 0 for t in tiers}

    sources: dict[str, int] = {}

    all_lat: list[float] = []
    det_lat: list[float] = []
    llm_lat: list[float] = []
    total_cost = 0.0
    llm_calls_total = 0
    records_with_llm = 0

    for r in results:
        gt = gt_by_id[r.bank_txn_id]
        tier = gt.difficulty
        tier_total[tier] += 1

        sources[r.resolved_by] = sources.get(r.resolved_by, 0) + 1

        all_lat.append(r.latency_ms)
        total_cost += r.cost_usd
        llm_calls_total += r.llm_calls
        if r.llm_calls > 0:
            records_with_llm += 1
            llm_lat.append(r.latency_ms)
        else:
            det_lat.append(r.latency_ms)

        if r.decision == "matched":
            matched += 1
        elif r.decision == "no_match":
            no_match += 1

        if r.decision in DECIDED:
            decided += 1
            tier_decided[tier] += 1
            ok = is_correct(r, gt)
            if ok:
                correct += 1
                tier_correct[tier] += 1
            else:
                incorrect += 1
                errors.append(
                    {
                        "bank_txn_id": r.bank_txn_id,
                        "tier": tier,
                        "decision": r.decision,
                        "predicted": r.settlement_ids,
                        "truth": gt.settlement_ids,
                        "resolved_by": r.resolved_by,
                        "confidence": r.confidence,
                    }
                )
            # a trap that was MATCHED (not no_match) is a hallucination
            if tier == "trap" and r.decision == "matched":
                hallucinated += 1

    per_tier = [
        TierMetrics(
            tier=t,
            total=tier_total[t],
            decided=tier_decided[t],
            correct=tier_correct[t],
            coverage=(tier_decided[t] / tier_total[t]) if tier_total[t] else 0.0,
            precision=(tier_correct[t] / tier_decided[t]) if tier_decided[t] else 1.0,
        )
        for t in tiers
    ]

    return Metrics(
        total=total,
        decided=decided,
        abstained=total - decided,
        matched=matched,
        no_match=no_match,
        correct=correct,
        incorrect=incorrect,
        auto_resolve_rate=decided / total if total else 0.0,
        precision=(correct / decided) if decided else 1.0,
        traps_total=traps_total,
        hallucinated_matches=hallucinated,
        hallucinated_match_rate=(hallucinated / traps_total) if traps_total else 0.0,
        per_tier=per_tier,
        resolution_sources=sources,
        total_cost_usd=round(total_cost, 6),
        cost_per_record_usd=round(total_cost / total, 8) if total else 0.0,
        llm_calls_total=llm_calls_total,
        records_with_llm=records_with_llm,
        records_deterministic=total - records_with_llm,
        latency_p50_ms=round(_percentile(all_lat, 0.50), 3),
        latency_p99_ms=round(_percentile(all_lat, 0.99), 3),
        det_latency_p50_ms=round(_percentile(det_lat, 0.50), 3),
        det_latency_p99_ms=round(_percentile(det_lat, 0.99), 3),
        llm_latency_p50_ms=round(_percentile(llm_lat, 0.50), 3),
        llm_latency_p99_ms=round(_percentile(llm_lat, 0.99), 3),
        errors=errors,
    )


# --------------------------------------------------------------------------- #
# Coverage / precision tradeoff curve (used by the Phase 6 threshold sweep)
# --------------------------------------------------------------------------- #


class CurvePoint(BaseModel):
    threshold: float
    coverage: float
    precision: float
    decided: int


def tradeoff_curve(
    results: list[MatchResult], truth: list[GroundTruth], steps: int = 21
) -> list[CurvePoint]:
    """Sweep a confidence threshold and report (coverage, precision) at each.

    A record counts as decided only if its confidence >= threshold; below it, the
    record is treated as an abstain regardless of the matcher's stated decision.
    This is how we find the highest-coverage threshold that still holds 100%
    precision on the train set.
    """
    gt_by_id = {g.bank_txn_id: g for g in truth}
    total = len(results)
    points: list[CurvePoint] = []
    for i in range(steps):
        tau = i / (steps - 1)
        decided = correct = 0
        for r in results:
            if r.decision in DECIDED and r.confidence >= tau:
                decided += 1
                if is_correct(r, gt_by_id[r.bank_txn_id]):
                    correct += 1
        points.append(
            CurvePoint(
                threshold=round(tau, 4),
                coverage=decided / total if total else 0.0,
                precision=(correct / decided) if decided else 1.0,
                decided=decided,
            )
        )
    return points


def pick_threshold(
    results: list[MatchResult], truth: list[GroundTruth], *, target_precision: float = 1.0,
    steps: int = 101,
) -> CurvePoint:
    """Lowest confidence threshold (⇒ highest coverage) that still holds
    ``target_precision`` on this set. Tune on TRAIN only; then report what the
    chosen threshold gives on holdout. Falls back to the strictest point if no
    threshold reaches the target (shouldn't happen — tau=1.0 decides nothing extra).
    """
    curve = tradeoff_curve(results, truth, steps=steps)
    ok = [p for p in curve if p.precision >= target_precision - 1e-9]
    if not ok:
        return curve[-1]
    return max(ok, key=lambda p: (p.coverage, -p.threshold))


# --------------------------------------------------------------------------- #
# IO helpers + the abstain-everything stub matcher
# --------------------------------------------------------------------------- #


def load_ground_truth(data_dir: str | Path) -> list[GroundTruth]:
    raw = json.loads((Path(data_dir) / "ground_truth.json").read_text())
    return [GroundTruth(**g) for g in raw]


def load_bank_txn_ids(data_dir: str | Path) -> list[str]:
    """Read just the credit ids from bank.json (no full ingest required)."""
    raw = json.loads((Path(data_dir) / "bank.json").read_text())
    return [t["bank_txn_id"] for t in raw["transactions"]]


def abstain_all(bank_txn_ids: list[str]) -> list[MatchResult]:
    """The stub: refuse to decide anything. Should score 0% coverage, 100%
    precision (no wrong decisions), 0% hallucinations."""
    return [
        MatchResult(bank_txn_id=tid, decision="abstain", resolved_by="none")
        for tid in bank_txn_ids
    ]
