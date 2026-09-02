"""Phase 6 — LLM adjudicator (the second and final LLM touchpoint).

Runs ONLY on the credits that deterministic matching (Phase 4) and global
assignment (Phase 5) left abstaining — the genuinely contended remainder
(~10-15% of credits: two indistinguishable batches, or one batch split across
two payouts). This is the single place in the pipeline where a model makes the
judgment call.

Design — **the model proposes, deterministic guards dispose.** The LLM is a
judgment engine, not an arithmetic engine and not a source of truth:

  * It never sees ground truth, and never emits raw settlement ids. It chooses a
    *label* from a short list of concrete OPTIONS this module pre-computes (each
    option is a whole batch or a uniquely-summing line subset, with its ids and
    numbers already worked out), or it returns ``no_match`` / ``abstain``.
  * Every match it proposes is re-verified here before acceptance: the chosen
    lines must still be free, must re-sum to the credit within tolerance, must
    not lose the contended batch to an equally-or-better-dated claimant (the
    BUG-002 date guard), and must not be one of two amount-and-date-tied options
    (the near-duplicate guard). Any failure ⇒ abstain.
  * Self-reported confidence below the accept threshold ⇒ abstain. The threshold
    is tuned on TRAIN only, via ``eval.harness.tradeoff_curve`` /
    ``pick_threshold`` — the highest-coverage point that still holds 100%
    precision — then reported on holdout.

The whole pipeline still runs with no API key: when the model is unavailable the
adjudicator abstains on everything (coverage then comes entirely from the
deterministic + assignment layers). It never raises and never guesses.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Literal

from pydantic import BaseModel, Field

from src.fees import ROUNDING_TOLERANCE_PAISE as TOL
from src.llm import LLMResult, structured_call
from src.match.assignment import _date_proximity
from src.match.candidates import Candidate, CreditCandidates
from src.match.deterministic import AMBIGUOUS, unique_subset
from src.models import MatchResult, RejectedCandidate

# Accept threshold for the model's self-reported confidence. This is the DEFAULT;
# the real value is chosen per-run by sweeping the tradeoff curve on train (see
# eval.harness.pick_threshold) and holding 100% precision. Kept conservative.
DEFAULT_THRESHOLD = 0.70

# Meet-in-the-middle cap for the adjudicator's per-batch subset enumeration. Higher
# than the deterministic pass (26): here we are down to a handful of credits, so we
# can afford to enumerate a few larger batches to see whether a *unique* subset
# exists that the cheaper pass skipped.
SUBSET_CAP = 32

# How many whole-batch options count as "tied" for the near-duplicate guard.
_TIE_EPS_PAISE = 0  # identical net delta
_MODEL = "adjudicator"


@dataclass
class AdjOutcome:
    results: dict[str, MatchResult] = field(default_factory=dict)  # every credit we processed
    consumed_batches: set[str] = field(default_factory=set)
    consumed_lines: set[str] = field(default_factory=set)
    llm_calls: int = 0
    cost_usd: float = 0.0


# --------------------------------------------------------------------------- #
# Concrete options — what the model is allowed to choose from
# --------------------------------------------------------------------------- #


@dataclass
class Option:
    """A concrete, self-verifiable resolution the model may select by ``label``."""

    label: str
    kind: Literal["whole_batch", "subset"]
    batch_id: str
    settlement_ids: list[str]
    net_paise: int
    amount_delta_paise: int
    ref_strength: float
    date_offset_days: int
    n_lines: int


# --------------------------------------------------------------------------- #
# The structured shape the model must return (INTERNAL — not models.Adjudication).
# Constrained to a label so the model can never invent settlement ids.
# --------------------------------------------------------------------------- #


class _Verdict(BaseModel):
    decision: Literal["match", "no_match", "abstain"]
    selected_option: str | None = None  # an Option.label, required when decision == "match"
    confidence: float = 0.0
    reasoning: str = ""
    rejected: list[RejectedCandidate] = Field(default_factory=list)


CallFn = Callable[..., LLMResult]


# --------------------------------------------------------------------------- #
# Option construction
# --------------------------------------------------------------------------- #


def _line_types(index, line_ids: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for lid in line_ids:
        t = getattr(index, "line_type", {}).get(lid, "line")
        out[t] = out.get(t, 0) + 1
    return out


def build_options(
    cc: CreditCandidates,
    index,
    consumed_batches: set[str],
    consumed_lines: set[str],
) -> tuple[list[Option], list[str]]:
    """Enumerate concrete options for one credit, plus human-readable NOTES.

    Options are only ever things we can hand back verbatim and re-verify:
      * a whole candidate batch whose *free* lines net to the credit within ±TOL;
      * a *uniquely*-summing subset of a candidate batch's free lines.
    When a batch's amount can be reached by more than one subset, or the batch is
    too large to enumerate, we emit a NOTE instead of an option — an argument for
    abstaining, never a guess.
    """
    options: list[Option] = []
    notes: list[str] = []
    next_label = iter("ABCDEFGHIJKLMNOP")

    for cand in cc.candidates:
        if cand.batch_id in consumed_batches:
            continue
        free_lines = [l for l in cand.line_ids if l not in consumed_lines]
        if not free_lines:
            continue
        net = sum(index.line_net[l] for l in free_lines)
        delta = net - cc.amount_paise
        offset = (cc.value_date - cand.settled_date).days

        if abs(delta) <= TOL:
            # whole free batch matches the credit outright
            options.append(
                Option(
                    label=next(next_label),
                    kind="whole_batch",
                    batch_id=cand.batch_id,
                    settlement_ids=list(free_lines),
                    net_paise=net,
                    amount_delta_paise=delta,
                    ref_strength=cand.ref_strength,
                    date_offset_days=offset,
                    n_lines=len(free_lines),
                )
            )
            continue

        if net < cc.amount_paise - TOL:
            # batch is smaller than the credit — cannot be a subset target
            continue

        # batch is larger than the credit: is there a unique line-subset that sums
        # to it? (this is where a split-payout could genuinely resolve.)
        pool = [(l, index.line_net[l]) for l in free_lines if index.line_net[l] > 0]
        sub = unique_subset(pool, cc.amount_paise, cap=SUBSET_CAP)
        if isinstance(sub, list):
            snet = sum(index.line_net[l] for l in sub)
            options.append(
                Option(
                    label=next(next_label),
                    kind="subset",
                    batch_id=cand.batch_id,
                    settlement_ids=list(sub),
                    net_paise=snet,
                    amount_delta_paise=snet - cc.amount_paise,
                    ref_strength=cand.ref_strength,
                    date_offset_days=offset,
                    n_lines=len(sub),
                )
            )
        elif sub is AMBIGUOUS:
            notes.append(
                f"batch {cand.batch_id}: multiple distinct line-subsets sum to the "
                f"credit amount — the exact composition is NOT determinable."
            )
        else:  # None: too large to enumerate / no in-tolerance subset
            notes.append(
                f"batch {cand.batch_id}: {len(free_lines)} lines, net "
                f"{net} paise — no unique subset matches the credit within tolerance."
            )

    return options, notes


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

_SYSTEM = """\
You are a reconciliation adjudicator for Indian payment settlements. You see only \
the hardest bank credits — the ones deterministic matching and global assignment \
could not resolve. Your task is JUDGMENT, not arithmetic.

Rules you must follow:
- Precision over coverage. A wrong match is far worse than abstaining. Abstaining \
("I don't know") is the CORRECT answer whenever the evidence does not single out \
exactly one option. It is not a failure.
- You may ONLY choose an option by its LABEL from the OPTIONS list, or return \
no_match or abstain. Never invent settlement ids or amounts.
- Match only when one option is clearly best on evidence you can name: an exact \
UTR/reference match, an exact amount, and a settlement date consistent with the \
value date (legit payouts land T+1..T+3, i.e. date_offset_days of 1-3).
- Two options that are indistinguishable on amount and date (e.g. two batches with \
identical totals and no UTR) => abstain. A forged/copied reference must not beat a \
genuine earlier settlement date.
- If a NOTE says a batch's amount can be reached by more than one line-subset, its \
composition is not determinable => abstain.

Worked example (abstain): a credit for an amount, no UTR in the narration, with two \
candidate batches that BOTH sum to exactly that amount on the same date. Nothing \
distinguishes them. Correct output: decision="abstain".
"""


def _fmt_rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def build_prompt(cc: CreditCandidates, options: list[Option], notes: list[str], index) -> str:
    lines = [
        "CREDIT",
        f"  amount: {_fmt_rupees(cc.amount_paise)} ({cc.amount_paise} paise)",
        f"  value_date: {cc.value_date}",
        f"  narration: {cc.narration!r}",
        f"  extracted_utrs: {cc.strong_utrs or 'none'}",
        "",
        "OPTIONS (choose one label, or abstain / no_match)",
    ]
    if not options:
        lines.append("  (none — no candidate resolves this credit)")
    for o in options:
        comp = _line_types(index, o.settlement_ids)
        comp_s = ", ".join(f"{k}×{v}" for k, v in sorted(comp.items())) or f"{o.n_lines} lines"
        lines.append(
            f"  [{o.label}] {o.kind} of batch {o.batch_id}: "
            f"{_fmt_rupees(o.net_paise)}, amount_delta={o.amount_delta_paise} paise, "
            f"date_offset_days={o.date_offset_days}, ref_strength={o.ref_strength:.1f}, "
            f"lines={o.n_lines} ({comp_s})"
        )
    if notes:
        lines.append("")
        lines.append("NOTES (arguments against a confident match)")
        for n in notes:
            lines.append(f"  - {n}")
    lines += [
        "",
        "FEE MODEL: gateway net = gross − 2% fee − 18% GST on the fee. All amounts "
        "above are already net, in paise. Matching tolerance is ±5 paise.",
        "",
        "Return: decision (match/no_match/abstain); selected_option (the label, only "
        "when decision=match); confidence 0..1; reasoning citing the evidence; and "
        "rejected (the options you ruled out, each with a one-line reason).",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Adjudication
# --------------------------------------------------------------------------- #


def _abstain(cc, reason: str, *, options, notes, verdict=None, llm=None) -> MatchResult:
    ev: dict = {
        "reason": reason,
        "options": [
            {"label": o.label, "kind": o.kind, "batch_id": o.batch_id,
             "amount_delta_paise": o.amount_delta_paise, "date_offset_days": o.date_offset_days,
             "ref_strength": o.ref_strength}
            for o in options
        ],
        "notes": notes,
    }
    if verdict is not None:
        ev["llm_decision"] = verdict.decision
        ev["llm_reasoning"] = verdict.reasoning
        ev["rejected"] = [r.model_dump() for r in verdict.rejected]
    return MatchResult(
        bank_txn_id=cc.bank_txn_id,
        decision="abstain",
        confidence=round(verdict.confidence, 4) if verdict else 0.0,
        resolved_by="none",
        evidence=ev,
        llm_calls=1 if llm and llm.ok else 0,
        cost_usd=llm.cost_usd if llm else 0.0,
        latency_ms=cc.latency_ms,
    )


def _claimants(unresolved, cc_map, consumed_batches):
    """batch_id -> [(credit_id, date_proximity)] over unresolved credits that match
    the WHOLE batch. The un-forgeable arbiter for contended batches (see BUG-002)."""
    from collections import defaultdict

    out: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for c in unresolved:
        cc = cc_map[c.bank_txn_id]
        for cand in cc.candidates:
            if cand.batch_id in consumed_batches:
                continue
            if abs(cand.net_total_paise - cc.amount_paise) <= TOL:
                dp = _date_proximity((cc.value_date - cand.settled_date).days)
                out[cand.batch_id].append((cc.bank_txn_id, dp))
    return out


def adjudicate(
    unresolved,
    cc_map: dict[str, CreditCandidates],
    index,
    consumed_batches: set[str],
    consumed_lines: set[str],
    *,
    call_fn: CallFn = structured_call,
    threshold: float = DEFAULT_THRESHOLD,
) -> AdjOutcome:
    """Adjudicate every still-unresolved credit that has at least one candidate.

    ``call_fn`` is injected so tests can drive the decision path with a scripted
    model. In production it is ``llm.structured_call``; when the model is
    unavailable that returns ``ok=False`` and we abstain.
    """
    out = AdjOutcome()
    # working copies so a match consumed mid-pass blocks later claimants
    cb = set(consumed_batches)
    cl = set(consumed_lines)
    claimants = _claimants(unresolved, cc_map, cb)

    for c in unresolved:
        cc = cc_map[c.bank_txn_id]
        options, notes = build_options(cc, index, cb, cl)
        if not options and not notes:
            continue  # nothing to say — leave for pipeline finalize (no_match)

        t0 = time.perf_counter()
        llm = call_fn(_SYSTEM, build_prompt(cc, options, notes, index), _Verdict, thinking=True)
        cc.latency_ms += (time.perf_counter() - t0) * 1000
        out.cost_usd += llm.cost_usd
        if llm.ok:
            out.llm_calls += 1

        if not llm.ok or llm.parsed is None:
            out.results[cc.bank_txn_id] = _abstain(
                cc, f"llm_unavailable:{llm.error}", options=options, notes=notes, llm=llm
            )
            continue

        verdict: _Verdict = llm.parsed
        out.results[cc.bank_txn_id] = _finalize_verdict(
            cc, verdict, options, notes, index, cb, cl, claimants, threshold, llm
        )
        r = out.results[cc.bank_txn_id]
        if r.decision == "matched":
            opt = next(o for o in options if o.label == verdict.selected_option)
            cb.add(opt.batch_id)
            cl.update(opt.settlement_ids)
            out.consumed_batches.add(opt.batch_id)
            out.consumed_lines.update(opt.settlement_ids)

    return out


def _finalize_verdict(cc, verdict, options, notes, index, cb, cl, claimants, threshold, llm) -> MatchResult:
    """Turn a model verdict into a MatchResult, applying every hard guard.

    Precision lives here: the model can only ever *propose*; a proposal becomes a
    match only if it survives all of the deterministic checks below."""
    by_label = {o.label: o for o in options}

    # no_match is only trustworthy when there was genuinely nothing to match to.
    if verdict.decision == "no_match":
        if options:  # there WAS a viable option — don't let the model wave it away
            return _abstain(cc, "llm_no_match_with_options", options=options, notes=notes,
                            verdict=verdict, llm=llm)
        if verdict.confidence < threshold:
            return _abstain(cc, "below_threshold", options=options, notes=notes,
                            verdict=verdict, llm=llm)
        return MatchResult(
            bank_txn_id=cc.bank_txn_id, decision="no_match",
            confidence=round(verdict.confidence, 4), resolved_by="llm",
            evidence={"reason": "llm_no_match", "llm_reasoning": verdict.reasoning, "notes": notes},
            llm_calls=1 if llm.ok else 0, cost_usd=llm.cost_usd, latency_ms=cc.latency_ms,
        )

    if verdict.decision != "match":
        return _abstain(cc, "llm_abstain", options=options, notes=notes, verdict=verdict, llm=llm)

    # ---- decision == "match": run the gauntlet -----------------------------
    opt = by_label.get(verdict.selected_option or "")
    if opt is None:
        return _abstain(cc, "invalid_option_label", options=options, notes=notes,
                        verdict=verdict, llm=llm)
    if verdict.confidence < threshold:
        return _abstain(cc, "below_threshold", options=options, notes=notes,
                        verdict=verdict, llm=llm)

    # (a) every chosen line must still be free
    if any(l in cl for l in opt.settlement_ids):
        return _abstain(cc, "lines_already_consumed", options=options, notes=notes,
                        verdict=verdict, llm=llm)
    # (b) re-sum the chosen lines ourselves — never trust the model's arithmetic
    resum = sum(index.line_net[l] for l in opt.settlement_ids)
    if abs(resum - cc.amount_paise) > TOL:
        return _abstain(cc, "amount_mismatch_on_recompute", options=options, notes=notes,
                        verdict=verdict, llm=llm)
    # (c) near-duplicate guard: another whole-batch option tied on amount AND date
    if opt.kind == "whole_batch":
        for other in options:
            if other.label == opt.label or other.kind != "whole_batch":
                continue
            if (abs(other.amount_delta_paise) <= abs(opt.amount_delta_paise) + _TIE_EPS_PAISE
                    and other.date_offset_days == opt.date_offset_days):
                return _abstain(cc, "ambiguous_tie", options=options, notes=notes,
                                verdict=verdict, llm=llm)
    # (d) contended-batch date guard (BUG-002): the winner of a batch must be its
    # strictly best-dated claimant — a forged reference cannot beat arrival date.
    my_dp = _date_proximity(opt.date_offset_days)
    if any(oc != cc.bank_txn_id and odp >= my_dp - 1e-9 for oc, odp in claimants.get(opt.batch_id, [])):
        return _abstain(cc, "lost_contention_on_date", options=options, notes=notes,
                        verdict=verdict, llm=llm)

    # survived every guard -> accept
    return MatchResult(
        bank_txn_id=cc.bank_txn_id,
        decision="matched",
        settlement_ids=list(opt.settlement_ids),
        confidence=round(min(1.0, verdict.confidence), 4),
        resolved_by="llm",
        evidence={
            "batch_id": opt.batch_id,
            "option": opt.label,
            "kind": opt.kind,
            "amount_delta_paise": opt.amount_delta_paise,
            "date_offset_days": opt.date_offset_days,
            "ref_strength": opt.ref_strength,
            "llm_reasoning": verdict.reasoning,
            "rejected": [r.model_dump() for r in verdict.rejected],
        },
        llm_calls=1 if llm.ok else 0,
        cost_usd=llm.cost_usd,
        latency_ms=cc.latency_ms,
    )
