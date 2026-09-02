"""Deterministic matcher — three strategies, ZERO LLM calls.

Runs with GLOBAL consumed-batch / consumed-line tracking, because the sharpest
traps depend on it: a batch (or its lines) claimed by one credit must not be
claimable by another. Strategy order:

  4a exact reference — extracted UTR == batch payout_ref, amount within ±5 paise.
  4b fee-adjusted   — recomputed net matches within ±5 paise, single candidate.
     (4a and 4b are unified here as "batch-total match"; they differ only in the
      resolved_by label and confidence, driven by whether the reference matched.)
  4c bounded subset-sum — N:1, a credit covers a subset of a batch's lines. Unique
     subset within tolerance → match; more than one distinct subset → ambiguous,
     pass downstream (never pick).

Contention safety: a batch wanted (amount-viable) by more than one credit is NOT
auto-matched — it is deferred to global assignment (Phase 5). This is what stops
the reused-UTR duplicate trap from stealing a batch via greedy logic.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field

from src.fees import ROUNDING_TOLERANCE_PAISE as TOL
from src.match.candidates import Candidate, CreditCandidates
from src.models import MatchResult

SUBSET_MAX_POOL = 26  # meet-in-the-middle cap; larger pools skip subset-sum


@dataclass
class DetOutcome:
    results: dict[str, MatchResult] = field(default_factory=dict)  # matched credits only
    consumed_batches: set[str] = field(default_factory=set)
    consumed_lines: set[str] = field(default_factory=set)


# --------------------------------------------------------------------------- #
# Bounded subset-sum (meet in the middle) — returns the unique subset or a flag
# --------------------------------------------------------------------------- #

AMBIGUOUS = "AMBIGUOUS"


def unique_subset(values: list[tuple[str, int]], target: int, tol: int = TOL,
                  cap: int = SUBSET_MAX_POOL):
    """Find subsets of ``values`` (id, paise) summing to target ± tol.

    Returns the list of ids of the single subset if exactly one distinct subset
    exists, ``AMBIGUOUS`` if two or more do, or ``None`` if none / pool too big.

    ``cap`` bounds the meet-in-the-middle pool (2**(cap/2) work); the deterministic
    pass uses the default, the Phase-6 adjudicator raises it to enumerate a few
    larger batches it is willing to spend more compute on.
    """
    n = len(values)
    if n == 0 or n > cap:
        return None

    half = n // 2
    left, right = values[:half], values[half:]

    def all_subsets(items):
        subs = [(0, ())]  # (sum, position-tuple)
        for i, (_id, v) in enumerate(items):
            subs += [(s + v, p + (i,)) for s, p in subs]
        return subs

    right_subs = all_subsets(right)  # positions relative to `right`
    right_subs.sort(key=lambda x: x[0])
    r_sums = [s for s, _ in right_subs]

    found: set[frozenset[int]] = set()
    for lsum, lpos in all_subsets(left):
        lo = target - tol - lsum
        hi = target + tol - lsum
        for j in range(bisect_left(r_sums, lo), bisect_right(r_sums, hi)):
            rpos = right_subs[j][1]
            combo = frozenset([p for p in lpos] + [half + p for p in rpos])
            if not combo:
                continue  # skip empty subset
            found.add(combo)
            if len(found) >= 2:
                return AMBIGUOUS
    if not found:
        return None
    (only,) = list(found)
    return [values[i][0] for i in sorted(only)]


# --------------------------------------------------------------------------- #
# Main deterministic pass
# --------------------------------------------------------------------------- #


def _viable(cc: CreditCandidates, consumed_batches: set[str]) -> list[Candidate]:
    """Candidate batches whose net total matches the credit within ±5 paise."""
    return [
        c
        for c in cc.candidates
        if c.batch_id not in consumed_batches and abs(c.net_total_paise - cc.amount_paise) <= TOL
    ]


def _make_match(cc: CreditCandidates, cand: Candidate) -> MatchResult:
    exact = cand.ref_strength >= 1.0
    return MatchResult(
        bank_txn_id=cc.bank_txn_id,
        decision="matched",
        settlement_ids=list(cand.line_ids),
        confidence=1.0 if exact else 0.95,
        resolved_by="exact_ref" if exact else "fee_adjusted",
        evidence={
            "batch_id": cand.batch_id,
            "ref_strength": cand.ref_strength,
            "matched_by": list(cand.matched_by),
            "amount_delta_paise": cand.net_total_paise - cc.amount_paise,
        },
        latency_ms=cc.latency_ms,
    )


def run(credits, cc_map: dict[str, CreditCandidates], index,
        consumed_batches: set[str] | None = None,
        consumed_lines: set[str] | None = None) -> DetOutcome:
    # Seed with anything an earlier layer (e.g. Phase-7 learned rules) already took,
    # so the deterministic pass never re-claims a consumed batch or line.
    out = DetOutcome(
        consumed_batches=set(consumed_batches or ()),
        consumed_lines=set(consumed_lines or ()),
    )
    by_id = {c.bank_txn_id: c for c in credits}
    unmatched = set(by_id)

    # ---- 4a + 4b: iterate batch-total matches to a fixpoint, contention-aware.
    # Iterate in sorted id order so the outcome is independent of set hashing
    # (PYTHONHASHSEED) — the 100%-precision claim must be reproducible.
    changed = True
    while changed:
        changed = False
        demand: dict[str, set[str]] = defaultdict(set)  # batch -> credits wanting it
        viable_map: dict[str, list[Candidate]] = {}
        for tid in sorted(unmatched):
            vb = _viable(cc_map[tid], out.consumed_batches)
            viable_map[tid] = vb
            for c in vb:
                demand[c.batch_id].add(tid)

        for tid in sorted(unmatched):
            vb = viable_map[tid]
            if len(vb) == 1 and len(demand[vb[0].batch_id]) == 1:
                cand = vb[0]
                out.results[tid] = _make_match(cc_map[tid], cand)
                out.consumed_batches.add(cand.batch_id)
                out.consumed_lines.update(cand.line_ids)
                unmatched.discard(tid)
                changed = True

    # ---- 4c: bounded subset-sum over free lines, for remaining credits.
    # Prefer candidate-batch lines (UTR/amount linked); fall back to window pool.
    for tid in sorted(unmatched):
        cc = cc_map[tid]
        # A credit whose amount matches a WHOLE free candidate batch is not a
        # strict-subset case — it lost 4a/4b to contention or multi-candidate
        # ambiguity (e.g. the trap_dup_utr pair, or near_dup). Subset-sum must not
        # back-door around that safety by grabbing the entire batch as its "subset";
        # defer such credits to global assignment (Phase 5).
        if _viable(cc, out.consumed_batches):
            continue
        pool_ids: list[str] = []
        for c in cc.candidates:
            if c.batch_id not in out.consumed_batches:
                pool_ids += [l for l in c.line_ids if l not in out.consumed_lines]
        if not pool_ids:
            pool_ids = [l for l in cc.pool_line_ids if l not in out.consumed_lines]
        # dedupe, keep order
        seen = set()
        pool = []
        for lid in pool_ids:
            if lid not in seen:
                seen.add(lid)
                pool.append((lid, index.line_net[lid]))

        res = unique_subset(pool, cc.amount_paise)
        if isinstance(res, list):
            out.results[tid] = MatchResult(
                bank_txn_id=tid,
                decision="matched",
                settlement_ids=res,
                confidence=0.9,
                resolved_by="subset_sum",
                evidence={"subset_size": len(res), "pool_size": len(pool)},
                latency_ms=cc.latency_ms,
            )
            out.consumed_lines.update(res)
            unmatched.discard(tid)

    return out
