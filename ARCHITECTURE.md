# Architecture

How a bank credit becomes a decision — the cascade, the guards, and why the traps fail
safely. This is the technical companion to the [README](README.md); read that first for
the thesis (auto-resolve at 100% precision, willing to abstain).

---

## 1. The problem, precisely

Three files describe the same money from three angles:

- **Orders** — the merchant's ledger of captured payments (amount, timestamp, id).
- **Settlements** — the gateway's payout file: individual settlement *lines* (each a
  gross amount, a fee, a tax, sometimes a negative adjustment or refund) grouped into
  **batches**, each batch carrying a payout **reference** (a UTR-like id) and a settled
  date. A batch is what the gateway pays out as one lump.
- **Bank statement** — the **credits** that actually landed in the merchant's account:
  an amount, a value date, and a free-text **narration** that *may* contain the payout
  reference (often mangled, grouped, or absent).

Reconciliation is: **for each bank credit, which settlement batch (or set of lines) is
it?** The hard part is that the narration is unreliable, amounts collide, and the data
contains adversarial look-alikes (§5).

The unit types live in [`src/models.py`](src/models.py): `Order`, `SettlementLine`,
`BankCredit`, `MatchResult` (the decision), and `GroundTruth` (for scoring only — never
read by the matcher).

---

## 2. The decision contract

Every credit gets exactly one `MatchResult` with a `decision`:

| decision   | meaning                                                        |
| ---------- | ------------------------------------------------------------- |
| `matched`  | resolved to a specific batch / set of settlement line ids     |
| `no_match` | confidently *nothing* here (e.g. a trap) — also a resolution  |
| `abstain`  | not confident enough — **routed to a human**                  |

`matched` and `no_match` both count as **decided** (they contribute to coverage);
`abstain` does not. **Precision** is measured over decided records only: of everything
the system committed to, how much was right. The design target is that precision is
*always* 100% — coverage is what we trade, never precision.

---

## 3. The candidate index (evidence, before any decision)

Before matching, [`src/match/candidates.py`](src/match/candidates.py) builds, for each
credit, the set of **candidate batches** it could plausibly be, annotated with the
evidence that will drive every downstream layer:

- **`ref_strength`** — did an id extracted from the narration match this batch's payout
  reference? (1.0 exact, lower/zero otherwise.) References are extracted with tolerant
  parsing; the *learned rules* (§7) extend that extraction.
- **amount delta** — batch net total minus credit amount, in paise.
- **date offset** — credit value date minus batch settled date, in days. Legitimate
  settlements land at **T+2**; the candidate window is `[value_date − 4, value_date]`.
- **line composition** — the batch's settlement lines (needed for subset-sum and for
  the drill-down UI).

Amounts are integer **paise** end-to-end; the only tolerance is **±5 paise**
(`ROUNDING_TOLERANCE_PAISE`), to absorb rounding in the fee model. No floats decide
anything.

---

## 4. The cascade

The pipeline ([`src/pipeline.py`](src/pipeline.py), `reconcile()` /
`reconcile_dataset()`) runs each credit through an ordered set of layers. **The first
layer confident enough to be safe wins; ambiguity falls through; survivors abstain.**
All layers share one mutable ledger — `consumed_batches` and `consumed_lines` — so a
settlement line or batch can be spent **at most once** across the whole run. That single
invariant is what makes the traps fail safely (§5).

Order of application:

### 0 · Learned rules (Phase 7) — *first*
If a rule set is supplied, learned narration rules run **before** everything else, to
recover references the base extractor missed (an analyst taught us the format). They
only *point*; the exact-reference guard below still verifies the amount, so a rule can
never force a wrong match. Details in §7. (With no rules supplied this layer is a no-op,
which is why the headline train/holdout numbers are rules-free.)

### 4a · Exact reference — confidence 1.0
A credit whose extracted reference equals a batch's payout reference **and** whose
amount agrees within ±5 paise is matched.
**Contention guard (the duplicate-UTR trap):** if *two* credits exact-match the *same*
batch, neither is auto-resolved — both are deferred to assignment (§Phase 5), which
enforces one-to-one. Only batches with exactly one reference-contender are matched here.
On match, the batch and its lines are marked consumed.

### 4b · Fee-adjusted amount — confidence 0.95
For a credit with exactly **one** free candidate batch whose recomputed **net** (from
gross via the fee model [`src/fees.py`](src/fees.py), including negative/refund lines)
equals the credit amount within ±5 paise → match. This resolves the common
"reference absent, but the arithmetic is unambiguous" case. Consumed batches are skipped.

### 4c · Bounded subset-sum — confidence ~0.9
For **N:1** splits (one batch paid out across several credits) and any leftover: take the
pool of still-free candidate lines and search for a subset summing to the credit amount
within ±5 paise. **Uniqueness is the safety property** — if more than one distinct
subset hits the target, the layer declares *ambiguous* and passes downstream rather than
guessing. A single unique subset → match.
The pool is capped at **26 lines** (`SUBSET_MAX_POOL`, a meet-in-the-middle bound);
larger splits deliberately **abstain** rather than risk an exponential/ambiguous search
— a correct "I don't know," not a failure.

Everything unresolved or ambiguous after 4a–4c carries to Phase 5 with its evidence.

### Phase 5 · Global assignment — [`src/match/assignment.py`](src/match/assignment.py)
The deterministic layers refuse to guess on **contended** credits (several credits eyeing
one batch). Phase 5 resolves contention *globally*: build a bipartite graph of
unresolved credits × their amount-viable, still-free candidate batches; weight each edge

```
weight = 0.5 · ref_strength  +  0.3 · amount_closeness  +  0.2 · date_proximity
```

and take the **maximum-weight one-to-one matching** (`scipy.optimize.linear_sum_assignment`,
padded to square). An assignment is *accepted* only if it is (a) one-to-one, (b) above an
accept threshold, and (c) unambiguously clear of the credit's next-best option.

**The forged-reference guard (BUG-002).** Max-weight assignment alone can be fooled: a
trap that *copies* a real batch's reference gets the highest-scoring edge and steals the
batch from its genuine owner. A forged reference beats genuine evidence because we let a
forgeable signal dominate. The fix: **contention is decided on evidence that can't be
forged — the value date.** The winner of a contended batch must be its strictly
best-dated claimant (genuine settlements land on T+2; the duplicate trap is a day late).
Reference is only a tie-break among equally-well-dated candidates; it can never override
date. Two identical-amount, identical-date near-duplicates therefore tie and the layer
**abstains** — correctly.

Accepted assignments become matches (confidence from score); the rest go to the
adjudicator with their scores as evidence. `assignment_mode` selects `"global"` (default),
`"greedy"` (first-come, the baseline for the experiment below), or `"off"`.

### Phase 6 · LLM adjudicator — [`src/match/adjudicator.py`](src/match/adjudicator.py)
The last touchpoint, and **off by default**. It sees only the Phase-5 survivors. Its role
is the *opposite* of what one might assume (see FINDING-003 in the log): it is **not** a
coverage lever, it is a precision-preserving explainer. Mechanics:

- The model **proposes a label** chosen from *pre-enumerated concrete options*; it never
  emits raw settlement ids.
- **Every proposal is re-verified by the same deterministic guards**: re-sum the amount,
  confirm the lines are still free, apply the near-dup tie and the contended-date guard,
  require confidence ≥ τ (`DEFAULT_THRESHOLD = 0.70`, tunable on train via
  `eval/harness.pick_threshold`), and require a valid label.
- The model proposes; **arithmetic disposes.** An adversarial model that tries to match
  everything *still* yields 100% precision / 0 hallucinations — which is the decisive
  test in the suite. A scripted `call_fn` makes the whole thing testable with no API key.

Whatever remains unresolved is `abstain` — routed to the human queue.

---

## 5. The trap taxonomy (and why each fails safely)

The generator ([`src/generator/`](src/generator)) plants adversarial cases whose whole
purpose is to tempt a wrong match. Each is defeated by a *structural* property, not a
heuristic:

| trap / hard case         | the temptation                                   | why it fails safely                                                            |
| ------------------------ | ------------------------------------------------ | ------------------------------------------------------------------------------ |
| `trap_dup_utr`           | copies a real batch's reference → looks perfect  | 4a contention guard defers; Phase-5 date guard gives the batch to the real, on-time owner; the trap finalises as `no_match` |
| `trap_out_of_window`     | right-ish amount, wrong date                     | outside the `[T−4, T]` candidate window → never a candidate                     |
| `trap_subset_of_matched` | its lines sum to a real credit                   | those lines are already **consumed** by their batch's 4a/4b match before 4c runs |
| `trap_unrelated`         | plausible amount, no real counterpart            | no reference, no unique subset, no viable assignment → confident `no_match`      |
| `near_dup` (hard)        | two batches, identical amount & date             | tie on every forgeable signal → assignment abstains rather than pick            |
| `split_1n` (hard)        | one batch across N credits; huge line pools      | resolves via unique subset-sum when tractable; **abstains** past the 26-line cap |

The residue the system abstains on is, by construction, *correct to abstain on*: the
near-dups are genuinely indistinguishable from evidence, and the large splits' exact line
partition isn't recoverable. Abstaining is the right answer, not a miss.

---

## 6. The consumed-line ledger — the load-bearing invariant

Almost every safety property reduces to one rule: **a settlement line is spent at most
once.** Processing is global and ordered — all exact/fee matches for *all* credits are
committed (and their lines consumed) *before* subset-sum runs — so `trap_subset_of_matched`
finds its lines already gone, and no two credits can claim the same money. The ledger is
threaded through every layer in `pipeline.reconcile()`; it is the difference between "a
pile of matchers" and a system.

---

## 7. The learning loop — [`src/rules/learned.py`](src/rules/learned.py)

The part that makes the system improve with use, and the part most able to quietly
regress precision. When an analyst resolves an exception in the console, if the resolved
batch's reference is *present but unparsed* in the narration (a novel grouping like
`PYT-7261-6751-3613` vs `PYT726167513613`), `induce_rule()` mints a reusable
`narration_ref` rule: a scoped regex + normaliser that recovers that format in future.

The safety move: **a learned rule earns nothing on its own authority.** It only extends
*reference extraction* — it *points* at a candidate. The **same amount guard** the rest
of the pipeline uses then decides whether the pointer is right. So an over-broad or
reckless rule cannot manufacture a wrong match; the worst it can do is point at something
the arithmetic then rejects. Rules persist via `RuleStore`; the learning-loop lift is
measured by [`src/rules/experiment.py`](src/rules/experiment.py). In the demo dataset a
*single* resolution induces a rule that recovers **16/16** novel-reference credits,
lifting auto-resolve from 27.3% → 100% at 100% precision, 0 hallucinations.

---

## 8. Ingestion & fees

- **Schema mapping** ([`src/ingest/schema_map.py`](src/ingest/schema_map.py)) maps
  arbitrary column headers to the canonical fields. It tries an **alias table** first
  (deterministic, no cost); an LLM fallback exists for unseen headers but is **not**
  required — with no key, the alias table carries it. `loader.py` reads a dataset
  directory (`orders.xlsx`, `settlements.csv`, `bank.json`, optional `ground_truth.json`).
- **Fee model** ([`src/fees.py`](src/fees.py)) recomputes a batch's **net** from its
  gross lines, fees, taxes, and negative adjustments — this is what 4b checks against,
  and the source of the ±5 paise rounding tolerance.

---

## 9. Evaluation — [`src/eval/`](src/eval)

`report.py` scores a matcher against ground truth and prints the metrics block, the
per-tier breakdown, the resolution-source distribution, and the **coverage/precision
tradeoff curve** (a threshold sweep). `harness.py` picks the highest-coverage threshold
that still holds 100% precision on *train*, which is then applied to holdout — so no
threshold is ever tuned on the holdout set.

Key properties the suite (73 tests, no API key needed) pins down: the pipeline is
**deterministic across hash seeds** (byte-for-byte reproducible); precision stays 100%
even under an adversarial adjudicator; the learning loop lifts coverage without breaking
precision; and each trap is refused for the structural reason above.

The captured train/holdout runs are in [`reports/`](reports/). See
[ENGINEERING_LOG.md](ENGINEERING_LOG.md) for the war stories behind the guards.
