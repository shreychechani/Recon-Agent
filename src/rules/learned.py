"""Phase 7 — the learning loop: analyst resolutions become reusable rules.

When an analyst resolves an exception in the queue (Phase 8), that single decision
often encodes a *general* fact: "this counterparty writes its payout reference as
``PYT-1784-5678-9012`` — a format our extractor doesn't recognise." We capture that
as a **learned rule**, persist it to JSON, and consult it on every future run
*before* the deterministic layer. The next time a credit from that counterparty
arrives, the rule recovers the reference the base extractor missed and the credit
auto-resolves — no analyst, no LLM.

Same discipline as Phase 6: **a rule proposes, deterministic guards dispose.** A
learned rule can only ever surface a *candidate reference* out of a narration; the
match is accepted only if that reference resolves to a real, still-free batch whose
net total equals the credit within tolerance. So an over-broad rule that extracts
garbage resolves to no batch, and a rule that extracts a real-but-wrong reference is
caught by the amount re-check. Learned rules cannot break 100% precision — they can
only recover coverage that was provably there.

Rules are induced conservatively: we only mint a rule when the resolved batch's
reference is actually *present* in the credit's narration (just unparsed). A
resolution with no reference in the narration (a genuine near-duplicate, say) is not
generalisable and yields no rule — honestly, we learn nothing from it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from src.fees import ROUNDING_TOLERANCE_PAISE as TOL
from src.match.candidates import CreditCandidates
from src.models import MatchResult

RuleKind = Literal["narration_ref"]


# --------------------------------------------------------------------------- #
# Rule model + persistent store
# --------------------------------------------------------------------------- #


class LearnedRule(BaseModel):
    """A generalisation of one analyst resolution.

    ``narration_ref``: ``pattern`` is a regex with one capture group that pulls a
    reference token out of a narration; ``normalizer`` reduces it to the canonical
    payout-reference form (strip separators, upper-case) before batch lookup.
    ``scope_contains`` gates the rule to narrations containing an anchor (usually the
    format's alpha prefix), so a rule's blast radius is bounded.
    """

    id: str
    kind: RuleKind = "narration_ref"
    pattern: str
    normalizer: Literal["strip_nonalnum", "upper", "none"] = "strip_nonalnum"
    scope_contains: str | None = None
    note: str = ""
    created_by: str = "analyst"
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_txn_id: str | None = None

    def applies_to(self, narration: str) -> bool:
        if self.scope_contains and self.scope_contains.upper() not in (narration or "").upper():
            return False
        return True

    def recover(self, narration: str) -> list[str]:
        """Normalised reference tokens this rule pulls from ``narration``."""
        if not self.applies_to(narration):
            return []
        out: list[str] = []
        for m in re.finditer(self.pattern, narration or "", re.IGNORECASE):
            token = m.group(1) if m.groups() else m.group(0)
            out.append(_normalize(token, self.normalizer))
        return out


class RuleStore:
    """JSON-backed list of learned rules — the persistence that makes it a *loop*."""

    def __init__(self, path: str | Path = ".cache/learned_rules.json"):
        self.path = Path(path)
        self.rules: list[LearnedRule] = []
        self.load()

    def load(self) -> list[LearnedRule]:
        if self.path.exists():
            raw = json.loads(self.path.read_text() or "[]")
            self.rules = [LearnedRule(**r) for r in raw]
        return self.rules

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([r.model_dump() for r in self.rules], indent=2))

    def add(self, rule: LearnedRule) -> None:
        if not any(r.id == rule.id for r in self.rules):
            self.rules.append(rule)
        self.save()

    def all(self) -> list[LearnedRule]:
        return list(self.rules)


def _normalize(token: str, how: str) -> str:
    if how == "strip_nonalnum":
        return re.sub(r"[^A-Za-z0-9]", "", token).upper()
    if how == "upper":
        return token.upper()
    return token


# --------------------------------------------------------------------------- #
# Rule INDUCTION — turn one analyst resolution into a reusable rule
# --------------------------------------------------------------------------- #

def _find_ref_span(narration: str, target: str) -> str | None:
    """The exact substring of ``narration`` whose alphanumerics equal ``target``.

    Works regardless of the separators the bank used (``PYT-1784-5678-9012``,
    ``PYT 1784 5678 9012`` …) by normalising the narration while tracking original
    offsets, finding ``target`` in the normalised text, then slicing back."""
    norm_chars, idx = [], []
    for i, ch in enumerate(narration):
        if ch.isalnum():
            norm_chars.append(ch.upper())
            idx.append(i)
    norm = "".join(norm_chars)
    p = norm.find(target)
    if p < 0:
        return None
    start, end = idx[p], idx[p + len(target) - 1] + 1
    return narration[start:end]


def induce_rule(
    narration: str,
    resolved_ref: str,
    *,
    created_by: str = "analyst",
    source_txn_id: str | None = None,
    rule_id: str | None = None,
) -> LearnedRule | None:
    """Learn a ``narration_ref`` rule from a resolution, or return None if the
    resolved batch's reference does not appear in the narration (nothing to learn).

    The generalisation: locate the run in the narration that normalises to the
    reference, then abstract its digit groups to ``\\d+`` while keeping the literal
    alpha anchor and the separator shape. So one ``PYT-1784-5678-9012`` teaches
    ``PYT(?:[-\\s/]?\\d+)+`` — every future credit in that format.
    """
    target = _normalize(resolved_ref, "strip_nonalnum")
    if not target or not narration:
        return None

    run = _find_ref_span(narration, target)
    if run is None:
        return None  # the reference isn't in the narration -> not generalisable

    pattern = _generalize(run)
    anchor_match = re.match(r"[A-Za-z]+", run)
    scope = anchor_match.group(0) if anchor_match else None
    return LearnedRule(
        id=rule_id or f"rule-{target[:12]}-{abs(hash(pattern)) % 10_000:04d}",
        kind="narration_ref",
        pattern=pattern,
        normalizer="strip_nonalnum",
        scope_contains=scope,
        note=f"learned from {source_txn_id or 'a resolution'}: reference format '{run}'",
        created_by=created_by,
        source_txn_id=source_txn_id,
    )


def _generalize(run: str) -> str:
    """Abstract a concrete reference run into a regex that captures its whole shape.

    Alpha segments stay literal (the format's fingerprint); digit segments become
    ``\\d+``; separators become an optional ``[-\\s/]?``. The whole match is captured.
    """
    parts = re.findall(r"[A-Za-z]+|\d+|[^A-Za-z0-9]+", run)
    out: list[str] = []
    for p in parts:
        if p.isalpha():
            out.append(re.escape(p))
        elif p.isdigit():
            out.append(r"\d+")
        else:
            out.append(r"[-\s/]?")
    return "(" + "".join(out) + ")"


# --------------------------------------------------------------------------- #
# Rule APPLICATION — with the same guards the rest of the pipeline uses
# --------------------------------------------------------------------------- #


@dataclass
class RuleOutcome:
    results: dict[str, MatchResult] = field(default_factory=dict)
    consumed_batches: set[str] = field(default_factory=set)
    consumed_lines: set[str] = field(default_factory=set)
    hits: dict[str, int] = field(default_factory=dict)  # rule_id -> times it resolved


def apply_rules(
    cc: CreditCandidates,
    index,
    rules: list[LearnedRule],
    consumed_batches: set[str],
    consumed_lines: set[str],
) -> MatchResult | None:
    """Try to resolve one credit via learned rules. Returns a verified match or None.

    A recovered reference must (a) not already be one the base extractor found — rules
    exist to recover what it *missed*; (b) resolve to a real, still-free batch; and
    (c) that batch's free lines must re-sum to the credit within ±TOL. Only then is it
    a match, tagged ``resolved_by='learned_rule'`` with the rule that fired.
    """
    known = {u.upper() for u in cc.strong_utrs}
    for rule in rules:
        for ref in rule.recover(cc.narration):
            if not ref or ref in known:
                continue  # empty, or the base extractor already had it
            bid = index.batch_for_ref(ref)
            if bid is None or bid in consumed_batches:
                continue
            free_lines = [l for l in index.batch_line_ids(bid) if l not in consumed_lines]
            if not free_lines:
                continue
            net = sum(index.line_net[l] for l in free_lines)
            if abs(net - cc.amount_paise) > TOL:
                continue  # rule surfaced a reference, but the money doesn't agree
            return MatchResult(
                bank_txn_id=cc.bank_txn_id,
                decision="matched",
                settlement_ids=list(free_lines),
                confidence=0.97,
                resolved_by="learned_rule",
                evidence={
                    "rule_id": rule.id,
                    "recovered_ref": ref,
                    "batch_id": bid,
                    "amount_delta_paise": net - cc.amount_paise,
                    "note": rule.note,
                },
                latency_ms=cc.latency_ms,
            )
    return None


def run_rules(credits, cc_map: dict[str, CreditCandidates], index,
              rules: list[LearnedRule]) -> RuleOutcome:
    """Apply learned rules across all credits, consumed-tracked. Sorted order keeps
    the outcome independent of hash seeding (same discipline as the matcher)."""
    out = RuleOutcome()
    for tid in sorted(cc_map):
        cc = cc_map[tid]
        r = apply_rules(cc, index, rules, out.consumed_batches, out.consumed_lines)
        if r is not None:
            out.results[tid] = r
            out.consumed_batches.add(r.evidence["batch_id"])
            out.consumed_lines.update(r.settlement_ids)
            rid = r.evidence["rule_id"]
            out.hits[rid] = out.hits.get(rid, 0) + 1
    return out
