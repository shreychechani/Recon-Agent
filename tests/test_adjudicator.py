"""Phase 6 adjudicator tests — driven by a SCRIPTED model, no API key needed.

The adjudicator's whole promise is "the model proposes, deterministic guards
dispose." So the tests it most needs are adversarial: feed it a model that tries
to MATCH everything and prove that 100% precision still holds — every unsafe pick
is rejected by a guard. We also prove the happy path (a genuine unique subset is
recovered) and each guard/branch in isolation.
"""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

from src.eval.harness import load_ground_truth, score
from src.llm import LLMResult
from src.match import adjudicator as A
from src.match.adjudicator import Option, _Verdict, adjudicate, build_options
from src.match.candidates import Candidate, CreditCandidates
from src.pipeline import reconcile_dataset

SEEDS = "data/seeds"


# --------------------------------------------------------------------------- #
# Scripted models (stand in for llm.structured_call)
# --------------------------------------------------------------------------- #


def _ok(v: _Verdict) -> LLMResult:
    return LLMResult(parsed=v, cost_usd=0.0001, input_tokens=100, output_tokens=20, ok=True)


def fake_abstain(system, user, schema, **kw):
    return _ok(_Verdict(decision="abstain", confidence=0.0, reasoning="scripted abstain"))


def fake_match_first(conf: float = 0.95):
    """Adversarial: match the FIRST offered option (or abstain if none offered)."""

    def f(system, user, schema, **kw):
        m = re.search(r"\[([A-P])\]", user)
        if not m:
            return _ok(_Verdict(decision="abstain", confidence=0.0, reasoning="no option"))
        return _ok(_Verdict(decision="match", selected_option=m.group(1),
                            confidence=conf, reasoning="scripted match"))

    return f


def fake_unavailable(system, user, schema, **kw):
    return LLMResult(None, 0.0, 0, 0, ok=False, error="llm_unavailable")


# --------------------------------------------------------------------------- #
# Tiny hand-built fixtures (no DuckDB): the adjudicator only needs line_net /
# line_type off the index, and Candidate / CreditCandidates dataclasses.
# --------------------------------------------------------------------------- #


class FakeIndex:
    def __init__(self, line_net, line_type=None):
        self.line_net = dict(line_net)
        self.line_type = dict(line_type or {})


def _cand(batch_id, line_ids, net_total, settled, ref_strength=0.0, payout_ref=None):
    return Candidate(batch_id=batch_id, net_total_paise=net_total, settled_date=settled,
                     payout_ref=payout_ref, ref_strength=ref_strength,
                     matched_by=("amount_date",), line_ids=list(line_ids))


def _cc(txn, amount, value_date, candidates, narration="", strong=()):
    return CreditCandidates(bank_txn_id=txn, amount_paise=amount, value_date=value_date,
                            strong_utrs=list(strong), candidates=candidates,
                            latency_ms=0.0, narration=narration)


def _run(cc, idx, *, call_fn, threshold=A.DEFAULT_THRESHOLD, extra=None):
    unresolved = [SimpleNamespace(bank_txn_id=cc.bank_txn_id)]
    cc_map = {cc.bank_txn_id: cc}
    if extra:
        for e in extra:
            unresolved.append(SimpleNamespace(bank_txn_id=e.bank_txn_id))
            cc_map[e.bank_txn_id] = e
    return adjudicate(unresolved, cc_map, idx, set(), set(),
                      call_fn=call_fn, threshold=threshold)


# --------------------------------------------------------------------------- #
# Happy path — a genuine unique subset is recovered
# --------------------------------------------------------------------------- #


def test_recovers_unique_subset():
    idx = FakeIndex({"L1": 100, "L2": 250, "L3": 400, "L4": 900})
    batch = _cand("B1", ["L1", "L2", "L3", "L4"], 1650, date(2026, 6, 20))
    cc = _cc("T1", 650, date(2026, 6, 22), [batch])  # 250+400 = 650, the only subset

    out = _run(cc, idx, call_fn=fake_match_first())
    r = out.results["T1"]
    assert r.decision == "matched"
    assert r.resolved_by == "llm"
    assert sorted(r.settlement_ids) == ["L2", "L3"]
    assert out.consumed_batches == {"B1"}
    assert out.consumed_lines == {"L2", "L3"}
    assert r.llm_calls == 1


def test_build_options_flags_ambiguous_subset_as_note():
    # 100+300 == 400 == 250+150? craft two distinct subsets summing to target.
    idx = FakeIndex({"L1": 100, "L2": 300, "L3": 250, "L4": 150})
    batch = _cand("B1", ["L1", "L2", "L3", "L4"], 800, date(2026, 6, 20))
    cc = _cc("T1", 400, date(2026, 6, 22), [batch])
    options, notes = build_options(cc, idx, set(), set())
    assert options == []  # ambiguous -> no concrete option offered
    assert any("multiple distinct line-subsets" in n for n in notes)


# --------------------------------------------------------------------------- #
# Precision guards — each must beat a model that WANTS to match
# --------------------------------------------------------------------------- #


def test_near_dup_abstains_despite_match():
    idx = FakeIndex({"X1": 500, "Y1": 500})
    a = _cand("B1", ["X1"], 500, date(2026, 6, 20))
    b = _cand("B2", ["Y1"], 500, date(2026, 6, 20))  # identical total, same date
    cc = _cc("T1", 500, date(2026, 6, 22), [a, b])

    out = _run(cc, idx, call_fn=fake_match_first())
    r = out.results["T1"]
    assert r.decision == "abstain"
    assert r.evidence["reason"] == "ambiguous_tie"


def test_contended_batch_date_guard_refuses_trap():
    idx = FakeIndex({"Z1": 500})
    # Both credits eye the same batch B1. Trap lands a day later (worse date).
    trap = _cc("TRAP", 500, date(2026, 6, 23),
               [_cand("B1", ["Z1"], 500, date(2026, 6, 20), ref_strength=1.0)])
    real = _cc("REAL", 500, date(2026, 6, 22),
               [_cand("B1", ["Z1"], 500, date(2026, 6, 20))])

    out = adjudicate([SimpleNamespace(bank_txn_id="TRAP"), SimpleNamespace(bank_txn_id="REAL")],
                     {"TRAP": trap, "REAL": real}, idx, set(), set(),
                     call_fn=fake_match_first(), threshold=A.DEFAULT_THRESHOLD)
    assert out.results["TRAP"].decision == "abstain"
    assert out.results["TRAP"].evidence["reason"] == "lost_contention_on_date"
    # the genuine, earlier-dated credit is the one allowed to win its batch
    assert out.results["REAL"].decision == "matched"


def test_invalid_option_label_abstains():
    idx = FakeIndex({"L1": 500})
    cc = _cc("T1", 500, date(2026, 6, 22), [_cand("B1", ["L1"], 500, date(2026, 6, 20))])

    def bad_label(system, user, schema, **kw):
        return _ok(_Verdict(decision="match", selected_option="Z", confidence=0.99, reasoning="x"))

    out = _run(cc, idx, call_fn=bad_label)
    assert out.results["T1"].decision == "abstain"
    assert out.results["T1"].evidence["reason"] == "invalid_option_label"


def test_below_threshold_abstains():
    idx = FakeIndex({"L1": 500})
    cc = _cc("T1", 500, date(2026, 6, 22), [_cand("B1", ["L1"], 500, date(2026, 6, 20))])
    out = _run(cc, idx, call_fn=fake_match_first(conf=0.50), threshold=0.70)
    assert out.results["T1"].decision == "abstain"
    assert out.results["T1"].evidence["reason"] == "below_threshold"


def test_resum_guard_rejects_wrong_amount():
    # Directly exercise the "never trust the model's arithmetic" guard: an option
    # whose lines do NOT sum to the credit must be refused even if selected.
    idx = FakeIndex({"L1": 100})
    cc = _cc("T1", 999, date(2026, 6, 22), [])  # credit wants 999, option pays 100
    opt = Option(label="A", kind="whole_batch", batch_id="B1", settlement_ids=["L1"],
                 net_paise=100, amount_delta_paise=0, ref_strength=0.0,
                 date_offset_days=2, n_lines=1)
    verdict = _Verdict(decision="match", selected_option="A", confidence=0.99, reasoning="x")
    r = A._finalize_verdict(cc, verdict, [opt], [], idx, set(), set(), {}, 0.70, _ok(verdict))
    assert r.decision == "abstain"
    assert r.evidence["reason"] == "amount_mismatch_on_recompute"


def test_no_match_with_options_is_downgraded_to_abstain():
    idx = FakeIndex({"L1": 500})
    cc = _cc("T1", 500, date(2026, 6, 22), [_cand("B1", ["L1"], 500, date(2026, 6, 20))])

    def says_no_match(system, user, schema, **kw):
        return _ok(_Verdict(decision="no_match", confidence=0.99, reasoning="x"))

    out = _run(cc, idx, call_fn=says_no_match)
    assert out.results["T1"].decision == "abstain"
    assert out.results["T1"].evidence["reason"] == "llm_no_match_with_options"


def test_model_abstain_is_recorded_with_reasoning():
    idx = FakeIndex({"L1": 500})
    cc = _cc("T1", 500, date(2026, 6, 22), [_cand("B1", ["L1"], 500, date(2026, 6, 20))])
    out = _run(cc, idx, call_fn=fake_abstain)
    r = out.results["T1"]
    assert r.decision == "abstain"
    assert r.evidence["reason"] == "llm_abstain"
    assert r.evidence["llm_reasoning"] == "scripted abstain"


def test_model_unavailable_abstains_and_costs_nothing():
    idx = FakeIndex({"L1": 500})
    cc = _cc("T1", 500, date(2026, 6, 22), [_cand("B1", ["L1"], 500, date(2026, 6, 20))])
    out = _run(cc, idx, call_fn=fake_unavailable)
    r = out.results["T1"]
    assert r.decision == "abstain"
    assert r.evidence["reason"].startswith("llm_unavailable")
    assert r.llm_calls == 0 and out.llm_calls == 0


# --------------------------------------------------------------------------- #
# End-to-end on the seed sample — the real precision proof
# --------------------------------------------------------------------------- #


def _score_seeds(**kw):
    results = reconcile_dataset(SEEDS, **kw)
    return results, score(results, load_ground_truth(SEEDS))


def test_adversarial_model_cannot_break_precision():
    """A model that MATCHES every hard case must still yield 100% precision and
    zero hallucinated traps — the guards, not the model, own precision."""
    _, base = _score_seeds(adjudicate=False)
    results, m = _score_seeds(adj_call_fn=fake_match_first(conf=0.99))

    assert m.precision == 1.0
    assert m.hallucinated_matches == 0
    # the adjudicator genuinely ran on the residue
    assert sum(r.llm_calls for r in results) > 0
    # it can only ever ADD coverage over the deterministic+assignment baseline
    assert m.auto_resolve_rate >= base.auto_resolve_rate


def test_passive_model_preserves_baseline_and_annotates():
    """A model that abstains on everything must leave coverage exactly at baseline
    and precision at 100% — Phase 6 is safe to ship even if the model is useless."""
    _, base = _score_seeds(adjudicate=False)
    results, m = _score_seeds(adj_call_fn=fake_abstain)

    assert m.precision == 1.0
    assert m.auto_resolve_rate == base.auto_resolve_rate
    # every credit the adjudicator touched carries a model reasoning for the queue
    touched = [r for r in results if r.llm_calls > 0]
    assert touched, "expected the adjudicator to run on the seed residue"
    assert all("llm_reasoning" in r.evidence or "llm_decision" in r.evidence for r in touched)
