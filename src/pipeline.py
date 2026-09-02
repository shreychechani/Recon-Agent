"""End-to-end reconciliation pipeline: files -> list[MatchResult].

Fixed sequence, one model call at one point (not yet wired):

    ingest -> candidate generation -> [learned rules] -> deterministic (4a/4b/4c)
           -> [global assignment] -> [LLM adjudication] -> finalize

Phases 5 (assignment) and 6 (adjudication) are not built yet; today, credits the
deterministic layer leaves undecided are finalized here as either a confident
``no_match`` (nothing left to match against — correct for out-of-window / unrelated
/ subset-of-matched traps) or an ``abstain`` (candidates exist but can't be
disambiguated — these will flow to assignment/adjudication once those land).

``report.py --matcher pipeline`` imports ``reconcile_dataset`` from here.
"""

from __future__ import annotations

from pathlib import Path

from src import llm
from src.ingest.loader import load_dataset
from src.match import adjudicator, assignment, deterministic
from src.match.candidates import CandidateIndex
from src.match.deterministic import AMBIGUOUS, unique_subset
from src.models import MatchResult
from src.rules import learned


def _abstain(cc, reason: str) -> MatchResult:
    return MatchResult(
        bank_txn_id=cc.bank_txn_id,
        decision="abstain",
        confidence=0.0,
        resolved_by="none",
        evidence={
            "reason": reason,
            "candidates": [
                {
                    "batch_id": c.batch_id,
                    "net_total_paise": c.net_total_paise,
                    "amount_delta_paise": c.net_total_paise - cc.amount_paise,
                    "ref_strength": c.ref_strength,
                    "matched_by": list(c.matched_by),
                }
                for c in cc.candidates
            ],
        },
        latency_ms=cc.latency_ms,
    )


def _no_match(cc, reason: str) -> MatchResult:
    return MatchResult(
        bank_txn_id=cc.bank_txn_id,
        decision="no_match",
        confidence=0.9,
        resolved_by="none",
        evidence={"reason": reason},
        latency_ms=cc.latency_ms,
    )


def reconcile_dataset(
    data_dir: str | Path,
    *,
    assignment_mode: str = "global",
    adjudicate: bool | None = None,
    adj_call_fn=None,
    adj_threshold: float = adjudicator.DEFAULT_THRESHOLD,
    rules=None,
) -> list[MatchResult]:
    """Files -> list[MatchResult].

    ``assignment_mode`` selects the Phase-5 strategy: ``"global"`` (max-weight
    one-to-one, the default), ``"greedy"`` (first-come-claims, for the baseline
    experiment), or ``"off"`` (deterministic layer only).

    ``adjudicate`` toggles the Phase-6 LLM adjudicator on the contended remainder.
    Default (``None``) = on iff a model is available (``llm.available()``); with no
    key it is skipped and coverage comes entirely from the deterministic + assignment
    layers. ``adj_call_fn`` injects a scripted model for tests.

    ``rules`` is the Phase-7 learned-rule list (analyst resolutions generalised to
    reusable narration rules), consulted BEFORE the deterministic layer.
    """
    orders, settlements, credits = load_dataset(data_dir)
    return reconcile(
        orders, settlements, credits,
        assignment_mode=assignment_mode, adjudicate=adjudicate,
        adj_call_fn=adj_call_fn, adj_threshold=adj_threshold, rules=rules,
    )


def reconcile(
    orders,
    settlements,
    credits,
    *,
    assignment_mode: str = "global",
    adjudicate: bool | None = None,
    adj_call_fn=None,
    adj_threshold: float = adjudicator.DEFAULT_THRESHOLD,
    rules=None,
) -> list[MatchResult]:
    """In-memory core: pre-loaded models -> list[MatchResult]. Same pipeline as
    ``reconcile_dataset`` without file IO — used by the learning-loop experiment
    and the tests, which build model objects directly."""
    index = CandidateIndex(settlements)
    cc_map = {c.bank_txn_id: index.for_credit(c) for c in credits}

    results: dict[str, MatchResult] = {}
    consumed_batches: set[str] = set()
    consumed_lines: set[str] = set()

    # ---- Phase 7: learned rules first — recover references the base extractor
    # missed (an analyst taught us the format). Verified, so precision is preserved.
    if rules:
        lr = learned.run_rules(credits, cc_map, index, rules)
        results.update(lr.results)
        consumed_batches |= lr.consumed_batches
        consumed_lines |= lr.consumed_lines

    # ---- Phase 4: deterministic (exact / fee / subset-sum), consumed-tracked.
    pending = [c for c in credits if c.bank_txn_id not in results]
    det = deterministic.run(pending, cc_map, index, consumed_batches, consumed_lines)
    results.update(det.results)
    consumed_batches |= det.consumed_batches
    consumed_lines |= det.consumed_lines

    # ---- Phase 5: global bipartite assignment over the contended remainder.
    if assignment_mode != "off":
        unresolved = [c for c in credits if c.bank_txn_id not in results]
        asg = assignment.assign(
            unresolved, cc_map, consumed_batches, consumed_lines, mode=assignment_mode
        )
        results.update(asg.results)
        consumed_batches |= asg.consumed_batches
        consumed_lines |= asg.consumed_lines

    # ---- Phase 6: LLM adjudicator on the contended remainder (opt-in / gated).
    adj_on = adjudicate if adjudicate is not None else (adj_call_fn is not None or llm.available())
    if adj_on:
        still = [c for c in credits if c.bank_txn_id not in results]
        kwargs = {"threshold": adj_threshold}
        if adj_call_fn is not None:
            kwargs["call_fn"] = adj_call_fn
        adj = adjudicator.adjudicate(
            still, cc_map, index, consumed_batches, consumed_lines, **kwargs
        )
        results.update(adj.results)
        consumed_batches |= adj.consumed_batches
        consumed_lines |= adj.consumed_lines

    # ---- Finalize: everything still undecided is a confident no_match (nothing
    # left to match against) or an abstain (candidates remain but can't be chosen).
    for c in credits:
        tid = c.bank_txn_id
        if tid in results:
            continue
        cc = cc_map[tid]
        free_cands = [x for x in cc.candidates if x.batch_id not in consumed_batches]
        if free_cands:
            # candidates exist but deterministic couldn't disambiguate (contention,
            # multiple viable batches, near-duplicate totals) -> hand to Phase 5/6.
            results[tid] = _abstain(cc, "ambiguous_candidates")
            continue

        # No free candidate batch. Is there any subset of free window lines?
        pool = [
            (lid, index.line_net[lid])
            for lid in dict.fromkeys(cc.pool_line_ids)
            if lid not in consumed_lines
        ]
        res = unique_subset(pool, cc.amount_paise)
        if res is None:
            # nothing to match against -> confidently unmatchable
            results[tid] = _no_match(cc, "no_viable_candidate")
        else:
            # a unique/ambiguous window subset survived — be cautious, abstain
            results[tid] = _abstain(cc, "window_subset_uncertain")

    index.close()
    return [results[c.bank_txn_id] for c in credits]
