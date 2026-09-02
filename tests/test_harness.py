"""Tests for the eval harness — the scorer everything else is judged by."""

from src.eval.harness import abstain_all, is_correct, score, tradeoff_curve
from src.models import GroundTruth, MatchResult


def _truth():
    return [
        GroundTruth(bank_txn_id="T1", settlement_ids=["a", "b"], difficulty="easy"),
        GroundTruth(bank_txn_id="T2", settlement_ids=["c"], difficulty="medium"),
        GroundTruth(bank_txn_id="T3", settlement_ids=[], difficulty="trap"),
    ]


def test_abstain_all_is_zero_coverage_full_precision():
    truth = _truth()
    m = score(abstain_all([g.bank_txn_id for g in truth]), truth)
    assert m.auto_resolve_rate == 0.0
    assert m.precision == 1.0
    assert m.hallucinated_matches == 0


def test_oracle_matcher_is_full_coverage_full_precision():
    truth = _truth()
    results = [
        MatchResult(bank_txn_id="T1", decision="matched", settlement_ids=["b", "a"], confidence=1.0, resolved_by="exact_ref"),
        MatchResult(bank_txn_id="T2", decision="matched", settlement_ids=["c"], confidence=0.95, resolved_by="fee_adjusted"),
        MatchResult(bank_txn_id="T3", decision="no_match", settlement_ids=[], confidence=0.9, resolved_by="assignment"),
    ]
    m = score(results, truth)
    assert m.auto_resolve_rate == 1.0
    assert m.precision == 1.0
    assert m.hallucinated_match_rate == 0.0
    assert m.matched == 2 and m.no_match == 1


def test_wrong_subset_is_a_precision_error():
    truth = _truth()
    results = [
        MatchResult(bank_txn_id="T1", decision="matched", settlement_ids=["a"], confidence=1.0),  # missing b
        MatchResult(bank_txn_id="T2", decision="abstain"),
        MatchResult(bank_txn_id="T3", decision="no_match"),
    ]
    m = score(results, truth)
    assert m.decided == 2  # T1 matched, T3 no_match
    assert m.incorrect == 1
    assert m.precision == 0.5
    assert len(m.errors) == 1 and m.errors[0]["bank_txn_id"] == "T1"


def test_matching_a_trap_is_a_hallucination():
    truth = _truth()
    results = [
        MatchResult(bank_txn_id="T1", decision="no_match"),  # wrong: real match called no_match
        MatchResult(bank_txn_id="T2", decision="abstain"),
        MatchResult(bank_txn_id="T3", decision="matched", settlement_ids=["c"], confidence=0.8),  # hallucination
    ]
    m = score(results, truth)
    assert m.hallucinated_matches == 1
    assert m.hallucinated_match_rate == 1.0  # 1 of 1 trap


def test_no_match_on_trap_is_correct():
    gt = GroundTruth(bank_txn_id="X", settlement_ids=[], difficulty="trap")
    r = MatchResult(bank_txn_id="X", decision="no_match")
    assert is_correct(r, gt) is True


def test_tradeoff_curve_coverage_is_monotonic_nonincreasing():
    truth = _truth()
    results = [
        MatchResult(bank_txn_id="T1", decision="matched", settlement_ids=["a", "b"], confidence=0.9),
        MatchResult(bank_txn_id="T2", decision="matched", settlement_ids=["c"], confidence=0.5),
        MatchResult(bank_txn_id="T3", decision="no_match", confidence=0.7),
    ]
    pts = tradeoff_curve(results, truth, steps=11)
    covs = [p.coverage for p in pts]
    assert covs == sorted(covs, reverse=True)  # raising threshold never raises coverage
    assert pts[0].coverage == 1.0  # threshold 0 decides everything
