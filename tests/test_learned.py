"""Phase 7 learned-rule tests — induction, verified application, persistence, and
the end-to-end learning loop. As everywhere else, the sharp tests are the precision
ones: a learned rule (or an over-broad one) must never manufacture a false match.
"""

from __future__ import annotations

from datetime import date, datetime, time

from src.eval.harness import score
from src.fees import compute_net_from_gross
from src.match.candidates import CandidateIndex
from src.models import BankCredit, SettlementLine
from src.pipeline import reconcile
from src.rules.experiment import build_demo
from src.rules.learned import (
    LearnedRule,
    RuleStore,
    apply_rules,
    induce_rule,
)


def _line(sid, bid, ref, gross, sdate=date(2026, 3, 10)):
    fee, gst, net = compute_net_from_gross(gross)
    return SettlementLine(settlement_id=sid, payout_batch_id=bid, order_id=None,
                          gross_paise=gross, fee_paise=fee, gst_paise=gst, net_paise=net,
                          line_type="sale", settled_at=datetime.combine(sdate, time(12, 0)),
                          payout_ref=ref)


# --------------------------------------------------------------------------- #
# Induction
# --------------------------------------------------------------------------- #


def test_induce_generalises_and_scopes():
    rule = induce_rule("SETTLEMENT PYT-1784-5678-9012 CR", "PYT178456789012",
                       source_txn_id="TXN-1")
    assert rule is not None
    assert rule.scope_contains == "PYT"
    # recovers the reference it was taught...
    assert rule.recover("SETTLEMENT PYT-1784-5678-9012 CR") == ["PYT178456789012"]
    # ...and generalises to a different value in the same format
    assert rule.recover("SETTLEMENT PYT-0000-1111-2222 CR") == ["PYT000011112222"]
    # but does not fire outside its scope
    assert rule.recover("NEFT HDFCN123456789012 RAZORPAY") == []


def test_induce_returns_none_when_reference_absent():
    # a genuine near-duplicate resolution: the reference simply isn't in the text
    assert induce_rule("RAZORPAY SETTLEMENT 2803 CR", "PYT178456789012") is None


def test_induce_handles_space_grouped_format():
    rule = induce_rule("PAYOUT PYT 1784 5678 9012 DONE", "PYT178456789012")
    assert rule is not None
    assert rule.recover("PAYOUT PYT 0001 0002 0003 DONE") == ["PYT000100020003"]


# --------------------------------------------------------------------------- #
# Application — the verification guard is the point
# --------------------------------------------------------------------------- #


def test_apply_recovers_match_when_amount_agrees():
    lines = [_line("STL-1", "B1", "PYT111122223333", 50000),
             _line("STL-2", "B1", "PYT111122223333", 70000)]
    net = sum(l.net_paise for l in lines)
    narr = "SETTLEMENT PYT-1111-2222-3333 CR"
    credit = BankCredit(bank_txn_id="T1", amount_paise=net, value_date=date(2026, 3, 12),
                        narration=narr, utr=None)
    idx = CandidateIndex(lines)
    cc = idx.for_credit(credit)
    rule = induce_rule(narr, "PYT111122223333")

    r = apply_rules(cc, idx, [rule], set(), set())
    assert r is not None and r.decision == "matched"
    assert r.resolved_by == "learned_rule"
    assert set(r.settlement_ids) == {"STL-1", "STL-2"}
    assert r.evidence["recovered_ref"] == "PYT111122223333"
    idx.close()


def test_apply_refuses_when_amount_disagrees():
    """The rule surfaces a real reference, but the money doesn't add up -> no match.
    This is what keeps a mis-generalised rule from ever inventing a match."""
    lines = [_line("STL-1", "B1", "PYT111122223333", 50000)]
    net = sum(l.net_paise for l in lines)
    narr = "SETTLEMENT PYT-1111-2222-3333 CR"
    credit = BankCredit(bank_txn_id="T2", amount_paise=net + 1000, value_date=date(2026, 3, 12),
                        narration=narr, utr=None)
    idx = CandidateIndex(lines)
    cc = idx.for_credit(credit)
    rule = induce_rule("SETTLEMENT PYT-1111-2222-3333 CR", "PYT111122223333")
    assert apply_rules(cc, idx, [rule], set(), set()) is None
    idx.close()


def test_apply_skips_reference_the_base_extractor_already_has():
    # a normal UTR the extractor parses -> the rule must not "re-resolve" it
    narr = "NEFT HDFCN123456789012 RAZORPAY"
    lines = [_line("STL-1", "B1", "HDFCN123456789012", 50000)]
    credit = BankCredit(bank_txn_id="T3", amount_paise=lines[0].net_paise,
                        value_date=date(2026, 3, 12), narration=narr, utr=None)
    idx = CandidateIndex(lines)
    cc = idx.for_credit(credit)
    assert "HDFCN123456789012" in cc.strong_utrs
    rule = LearnedRule(id="r", pattern=r"(HDFCN\d+)", scope_contains="HDFCN")
    assert apply_rules(cc, idx, [rule], set(), set()) is None
    idx.close()


# --------------------------------------------------------------------------- #
# Persistence — what makes it a *loop*
# --------------------------------------------------------------------------- #


def test_rulestore_roundtrips(tmp_path):
    p = tmp_path / "rules.json"
    store = RuleStore(p)
    store.add(LearnedRule(id="r1", pattern=r"(PYT\d+)", scope_contains="PYT", note="x"))
    store.add(LearnedRule(id="r1", pattern=r"(PYT\d+)"))  # dedup by id
    reloaded = RuleStore(p)
    assert [r.id for r in reloaded.all()] == ["r1"]
    assert reloaded.all()[0].scope_contains == "PYT"


# --------------------------------------------------------------------------- #
# End-to-end learning loop
# --------------------------------------------------------------------------- #


def _induce_from_seed(settlements, credits, gts):
    by_line = {l.settlement_id: l for l in settlements}
    sg = next(g for g in gts if "novel_ref" in g.corruption_tags)
    credit = next(c for c in credits if c.bank_txn_id == sg.bank_txn_id)
    return induce_rule(credit.narration, by_line[sg.settlement_ids[0]].payout_ref,
                       source_txn_id=sg.bank_txn_id)


def test_one_resolution_lifts_the_whole_cohort_at_full_precision():
    settlements, credits, gts = build_demo(n_pairs=4)

    before = score(reconcile([], settlements, credits, rules=None), gts)
    assert before.precision == 1.0
    # the novel-reference cohort is unresolved at baseline
    assert before.auto_resolve_rate < 1.0

    rule = _induce_from_seed(settlements, credits, gts)
    after_results = reconcile([], settlements, credits, rules=[rule])
    after = score(after_results, gts)

    assert after.precision == 1.0
    assert after.hallucinated_matches == 0
    assert after.auto_resolve_rate > before.auto_resolve_rate
    # every novel-ref credit (2 per pair) is now recovered by the learned rule
    assert sum(1 for r in after_results if r.resolved_by == "learned_rule") == 2 * 4


def test_rules_none_and_empty_are_equivalent():
    settlements, credits, gts = build_demo(n_pairs=3)
    a = score(reconcile([], settlements, credits, rules=None), gts)
    b = score(reconcile([], settlements, credits, rules=[]), gts)
    assert a.auto_resolve_rate == b.auto_resolve_rate == a.auto_resolve_rate


def test_overbroad_rule_cannot_break_precision():
    """A recklessly general rule (capture ANY digit run) fires everywhere but still
    cannot create a false match: recovered numbers must resolve to a real batch AND
    agree on amount."""
    settlements, credits, gts = build_demo(n_pairs=4)
    junk = LearnedRule(id="junk", pattern=r"(\d+)", scope_contains=None)
    results = reconcile([], settlements, credits, rules=[junk])
    m = score(results, gts)
    assert m.precision == 1.0
    assert m.hallucinated_matches == 0
