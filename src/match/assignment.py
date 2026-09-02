"""Phase 5 — global bipartite assignment (scipy ``linear_sum_assignment``).

The deterministic layer resolves everything it can *prove* one-to-one. What it
hands over is the genuinely contended remainder: several credits eyeing the same
batch (the reused-UTR duplicate trap and its real twin), or one credit facing two
indistinguishable batches (the near-duplicate). Greedy "first credit to ask claims
the batch" gets these wrong — it will happily hand a batch to a trap that asked
first. So we solve it as one global assignment problem instead.

We build a bipartite graph of unresolved credits × their *amount-viable, still-free*
candidate batches, weight each edge, and take the maximum-weight one-to-one matching.
Edge weight:

    0.5 * ref_strength  +  0.3 * amount_closeness  +  0.2 * date_proximity

One-to-one is the whole point: a batch can be awarded to exactly one credit, so the
real credit (T+2, exact reference) wins its batch and the duplicate trap is left with
nothing — which the pipeline then finalizes as a confident ``no_match``.

Acceptance is deliberately conservative — precision first. An assignment is only
promoted to a match when it is (a) the credit's own top-scoring viable batch,
(b) above an accept threshold, and (c) unambiguous — clear of the credit's next-best
batch by a margin. That last guard is what makes the near-duplicate abstain instead
of guessing between two identical-looking batches.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from src.fees import ROUNDING_TOLERANCE_PAISE as TOL
from src.match.candidates import Candidate, CreditCandidates
from src.models import MatchResult

# Edge-weight mixture.
W_REF, W_AMOUNT, W_DATE = 0.5, 0.3, 0.2
IDEAL_OFFSET_DAYS = 2  # legit settlement lands at value_date = settled + 2
DATE_DECAY_DAYS = 4  # candidate window is [value_date-4, value_date]

# Acceptance gates (tuned on TRAIN to hold 100% precision — see report).
ACCEPT_THRESHOLD = 0.60
AMBIGUITY_MARGIN = 0.10

_NON_EDGE = -1e6  # weight for a (credit, batch) pair that is not amount-viable


@dataclass
class AssignOutcome:
    results: dict[str, MatchResult] = field(default_factory=dict)  # newly matched credits
    consumed_batches: set[str] = field(default_factory=set)
    consumed_lines: set[str] = field(default_factory=set)
    # kept for the greedy-vs-global experiment / drill-down
    considered: dict[str, list[tuple[str, float]]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Edge scoring
# --------------------------------------------------------------------------- #


def _amount_closeness(delta_paise: int) -> float:
    return max(0.0, 1.0 - abs(delta_paise) / (TOL + 1))


def _date_proximity(offset_days: int) -> float:
    return max(0.0, 1.0 - abs(offset_days - IDEAL_OFFSET_DAYS) / DATE_DECAY_DAYS)


def score_edge(cc: CreditCandidates, cand: Candidate) -> float:
    """Weight of matching this credit to this whole batch (higher = better)."""
    delta = cand.net_total_paise - cc.amount_paise
    offset = (cc.value_date - cand.settled_date).days
    return (
        W_REF * cand.ref_strength
        + W_AMOUNT * _amount_closeness(delta)
        + W_DATE * _date_proximity(offset)
    )


def _viable_edges(cc: CreditCandidates, consumed_batches: set[str]) -> list[Candidate]:
    """Free candidate batches whose net total equals the credit within ±TOL.

    Only whole-batch matches are assignment edges. A credit that is merely a subset
    of a bigger candidate batch (split_1n) has no viable edge here and is left for
    subset-sum / adjudication — assignment must not match it whole."""
    return [
        c
        for c in cc.candidates
        if c.batch_id not in consumed_batches
        and abs(c.net_total_paise - cc.amount_paise) <= TOL
    ]


# --------------------------------------------------------------------------- #
# The assignment pass
# --------------------------------------------------------------------------- #


def _make_result(cc: CreditCandidates, cand: Candidate, score: float,
                 ranked: list[tuple[str, float]]) -> MatchResult:
    runner_up = ranked[1] if len(ranked) > 1 else None
    return MatchResult(
        bank_txn_id=cc.bank_txn_id,
        decision="matched",
        settlement_ids=list(cand.line_ids),
        confidence=round(min(1.0, score), 4),
        resolved_by="assignment",
        evidence={
            "batch_id": cand.batch_id,
            "score": round(score, 4),
            "ref_strength": cand.ref_strength,
            "amount_delta_paise": cand.net_total_paise - cc.amount_paise,
            "date_offset_days": (cc.value_date - cand.settled_date).days,
            "runner_up": {"batch_id": runner_up[0], "score": round(runner_up[1], 4)}
            if runner_up
            else None,
            "won_contention": True,
        },
        latency_ms=cc.latency_ms,
    )


def _pair_up(credits, cc_map, consumed_batches, mode: str):
    """Return {credit_id: assigned_batch_id} plus per-credit ranked viable edges.

    ``mode='global'`` = max-weight one-to-one matching (scipy).
    ``mode='greedy'`` = credits in input order, each grabs its best free batch.
    """
    edges: dict[str, list[Candidate]] = {}
    for c in credits:
        vb = _viable_edges(cc_map[c.bank_txn_id], consumed_batches)
        if vb:
            edges[c.bank_txn_id] = vb

    ranked: dict[str, list[tuple[str, float]]] = {
        cid: sorted(((cand.batch_id, score_edge(cc_map[cid], cand)) for cand in vb),
                    key=lambda t: (-t[1], t[0]))
        for cid, vb in edges.items()
    }

    cids = [c.bank_txn_id for c in credits if c.bank_txn_id in edges]
    if not cids:
        return {}, ranked

    if mode == "greedy":
        assigned: dict[str, str] = {}
        taken: set[str] = set()
        for cid in cids:  # input order — first come, first served
            for bid, _s in ranked[cid]:
                if bid not in taken:
                    assigned[cid] = bid
                    taken.add(bid)
                    break
        return assigned, ranked

    # global: max-weight matching via scipy (minimise negative weight).
    batch_ids = sorted({cand.batch_id for vb in edges.values() for cand in vb})
    col_of = {bid: j for j, bid in enumerate(batch_ids)}
    m = len(batch_ids)
    n = len(cids)
    # real-batch columns + n dummy columns (weight 0 = "match nothing").
    W = np.zeros((n, m + n))
    W[:, m:] = 0.0
    W[:, :m] = _NON_EDGE
    score_lookup: dict[tuple[str, str], float] = {}
    for i, cid in enumerate(cids):
        for bid, s in ranked[cid]:
            W[i, col_of[bid]] = s
            score_lookup[(cid, bid)] = s
    row_ind, col_ind = linear_sum_assignment(-W)
    assigned = {}
    for i, j in zip(row_ind, col_ind):
        if j < m:  # a real batch, not a dummy
            assigned[cids[i]] = batch_ids[j]
    return assigned, ranked


def assign(
    credits,
    cc_map: dict[str, CreditCandidates],
    consumed_batches: set[str],
    consumed_lines: set[str],
    *,
    mode: str = "global",
    accept: float = ACCEPT_THRESHOLD,
    margin: float = AMBIGUITY_MARGIN,
) -> AssignOutcome:
    """Assign the unresolved ``credits`` to free batches; promote confident,
    unambiguous assignments to matches. Never reads ground truth."""
    out = AssignOutcome()
    assigned, ranked = _pair_up(credits, cc_map, consumed_batches, mode)
    cand_by = {c.bank_txn_id: {x.batch_id: x for x in cc_map[c.bank_txn_id].candidates}
               for c in credits}

    # Un-forgeable evidence: every unresolved claimant of a batch, with its date
    # proximity. A reused-UTR trap can copy a reference, but it cannot land on the
    # real payout's value date — the genuine settlement is T+2, the dup is later.
    claimants: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for c in credits:
        cc = cc_map[c.bank_txn_id]
        for cand in _viable_edges(cc, consumed_batches):
            dp = _date_proximity((cc.value_date - cand.settled_date).days)
            claimants[cand.batch_id].append((c.bank_txn_id, dp))

    for cid, bid in assigned.items():
        rk = ranked[cid]
        out.considered[cid] = rk
        top_bid, top_score = rk[0]
        second = rk[1][1] if len(rk) > 1 else 0.0
        cand = cand_by[cid][bid]
        # (a) must be the credit's own best batch, (b) confident, (c) unambiguous
        # among the credit's own candidates.
        if bid != top_bid:
            continue
        if top_score < accept:
            continue
        if (top_score - second) < margin:
            continue  # e.g. near_dup: two indistinguishable batches -> abstain
        # (d) must be the strictly best-dated claimant of this batch. This is what
        # refuses the reused-UTR trap even when its forged reference gives it the
        # highest raw score: an earlier-dated real credit also matches the batch.
        my_dp = _date_proximity((cc_map[cid].value_date - cand.settled_date).days)
        if any(oc != cid and odp >= my_dp - 1e-9 for oc, odp in claimants[bid]):
            continue
        out.results[cid] = _make_result(cc_map[cid], cand, top_score, rk)
        out.consumed_batches.add(bid)
        out.consumed_lines.update(cand.line_ids)

    return out
