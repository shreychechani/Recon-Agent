# Engineering Log — bugs found & how we handled them

A running record of the non-trivial bugs we hit while building the reconciliation
agent, what caused them, and how we fixed them. Kept for the Razorpay writeup /
demo — each entry is meant to be tellable on its own.

The theme across all of them: the product's promise is **100% precision, willing to
say "I don't know."** So the bugs that matter most aren't crashes — they're the ones
that let the system *quietly say the wrong thing with confidence*. Those are the ones
we hunt for.

---

## BUG-001 — The duplicate-UTR trap could be hallucinated as a match (and the result wasn't even reproducible)

**Severity:** high — it broke the headline claim (100% precision) *and* made the
metric non-reproducible.

**Symptom.** Running the exact same evaluation on the exact same data gave a
*different* precision each time — anywhere from 99.6% to 100%, with 0 to 3 traps
wrongly matched. A number that changes when nothing changed is worse than a bad
number: you can't trust any of it.

**How we caught it.** We ran the zero-LLM pipeline five times in a row and watched
the "hallucinated-match rate on traps" swing between 0/40 and 3/40. Fixing Python's
hash seed (`PYTHONHASHSEED`) made each run reproducible but *different seeds gave
different answers* — which told us the outcome depended on the iteration order of a
`set`, not on the data.

**Root cause.** One of our four trap types, `trap_dup_utr`, is a fake credit that
copies a **real** credit's UTR *and* its exact amount, dated a day later. Because it
matches the real credit on both reference and amount, our deterministic matcher
correctly *refused* to auto-resolve either one (two credits contending for the same
settlement batch → defer to global assignment). That safety worked.

The leak was one layer down. After the exact/fee-match step, the matcher runs a
**bounded subset-sum** step (strategy "4c") to handle the legitimate case where one
settlement batch is paid out as two smaller bank credits. For the dup-UTR trap, the
"subset" that adds up to its amount is the *entire batch* — so subset-sum happily
claimed all of the batch's lines for whichever credit it processed first. Since it
iterated over a `set` of credit IDs, "first" was decided by hash order: sometimes the
real credit grabbed the batch (fine), sometimes the trap did (a hallucinated match).

**Fix.** Two parts:

1. **Correctness — gate the subset-sum step.** Subset-sum exists only for the
   *strict-subset* case. A credit whose amount equals a whole candidate batch is not
   a subset case; if the earlier step already deferred it (contention or ambiguity),
   it must go to global assignment, never be back-doored through subset-sum grabbing
   the full batch. We added exactly that guard: if a credit is still amount-viable
   for a free candidate batch, skip subset-sum and defer it.

2. **Reproducibility — remove the hidden ordering dependency.** We iterate the
   remaining credits in sorted ID order instead of set order, so the outcome no
   longer depends on `PYTHONHASHSEED` or dict/set internals.

**Result.** Precision is now a stable **100.0%** with **0 hallucinated trap matches**,
identical across repeated runs and across hash seeds, on the train set (800),
the held-out set (800), and the committed sample (61). Coverage held at ~85%.
The handful of *real* credits that were previously matched "by luck" (when they won
the race against their trap twin) now correctly abstain — they'll be recovered
properly by the global-assignment phase, which is designed to award the batch to the
better-scoring real credit and leave the trap unmatched.

**What we'd tell a reviewer.** The interesting part isn't that a trap slipped
through — it's *why*. A safety at one layer (contention deferral) was silently
undone by a convenience at the next layer (subset-sum treating a whole batch as a
subset). And the tell that pointed us straight at it was **non-determinism**: a
correctness property that flickers run-to-run is almost always an ordering
dependency, and ordering dependencies in a "should be provable" pipeline are exactly
where quiet wrong answers hide.

**Regression guard.** `tests/test_deterministic.py` now pins this down:
a constructed dup-UTR fixture asserts the trap is never matched and the contended
batch is never consumed; a constructed split fixture asserts real subset splits still
resolve (so the gate didn't over-block); and a determinism test asserts the decisions
are identical across repeated runs.

---

## BUG-002 — Global assignment could be fooled into matching a trap by a *forged* reference

**Severity:** high — a fresh way to break the 100% precision claim, uncovered the
moment we turned on the new assignment layer (Phase 5).

**Context.** The deterministic layer refuses to guess on *contended* credits (several
credits eyeing the same settlement batch). Phase 5 resolves that contention with a
**maximum-weight one-to-one assignment**: score every (credit, batch) edge, then take
the matching that maximises total score, so one batch goes to exactly one credit. The
edge score mixed three signals — reference match, amount closeness, date proximity —
with reference weighted highest.

**Symptom.** Precision dropped below 100% again (down to 99.3% on the held-out set),
and the wrong answers were all the *reused-UTR* trap being confidently matched — the
exact trap this layer was supposed to refuse.

**Root cause — a collision of two trap types.** One batch was claimed by *three*
credits at once:

  * two **near-duplicate** credits — real settlements for one of two batches with
    identical totals, no UTR in the narration, both landing on the ideal value date;
  * the **reused-UTR trap** — which had copied that batch's reference (so it *looked*
    like a perfect reference match) but landed one day late.

Maximum-weight assignment did exactly what it was told: it maximised score, and the
trap's **forged reference** gave it the single highest-scoring edge — higher than the
real owners, who had no UTR to show. So the optimiser handed the batch to the trap.
The one-to-one property we were relying on didn't save us: it guarantees *a* winner,
not the *right* winner. **A forged piece of evidence beat genuine evidence, because
we let the forgeable signal dominate the score.**

**Fix — decide contention on evidence that can't be forged.** A trap can copy a
reference; it cannot copy *when the money actually arrives*. Genuine settlements land
on their value date (T+2); the duplicate trap is deliberately a day or more later. So
we added a hard rule to the acceptance step: **the winner of a contended batch must be
its strictly best-dated claimant.** A credit can only be awarded a batch if no other
credit that also matches that batch landed as-early-or-earlier. Reference is now only
a tie-breaker among equally-well-dated candidates — it can never override date.

That single rule resolves both halves of the collision:
  * the trap (a day late) can never out-rank the real, on-time owner, so it is
    refused;
  * the two near-duplicate owners are tied on date *and* amount, so neither is
    strictly best — the layer abstains instead of guessing between them.

**Result.** Precision back to a stable **100.0%** with **0 hallucinated trap matches**
on train, held-out, and sample sets. And the layer still pays for itself: it correctly
resolves the *simple* duplicate-UTR case (real credit wins its batch, the trap is
finalized as a confident `no_match`), lifting coverage from ~85% to ~88% at no
precision cost.

**Bonus finding (the greedy-vs-global experiment).** With the date guard in place we
compared naive greedy assignment (first credit to ask claims the batch) against the
global optimum. Both hold 100% precision now — but the global assignment resolves
roughly **twice** as many contended credits (train 10 vs 6, held-out 8 vs 3), because
greedy lets one credit grab a batch a different credit needed more. Global one-to-one
buys real coverage at equal precision.

**What we'd tell a reviewer.** Two lessons worth stating out loud. (1) "Enforce
one-to-one" is *not* the same as "get it right" — an optimiser will faithfully
maximise whatever you score, so if a forgeable signal (a copyable reference) carries
the most weight, a forgery wins. (2) The fix wasn't a bigger model or more features;
it was choosing to arbitrate on the one signal an adversary *can't* fake — the arrival
date. In reconciliation, physics (when the money moved) beats paperwork (what the
narration claims).

**Regression guard.** `tests/test_assignment.py` reconstructs the exact collision
(near-dup pair + reused-UTR trap on one batch) and asserts nobody is matched; plus the
simple dup-UTR case (real wins, trap refused), the near-dup abstain, and a
greedy-vs-global invariant (both 100% precise, global covers at least as much).

---

## FINDING-003 — Phase 6: the LLM's job turned out to be the *opposite* of what we assumed, and the real risk was letting it lie

**Severity:** design-level — got the value proposition of the whole LLM phase wrong
at first, and the naive version of it would have been the single easiest place to
break the 100%-precision promise.

**What we assumed.** Phase 6 was scoped as "LLM adjudicator to recover coverage on
the ~12% the deterministic + assignment layers abstain on." The mental model: the
hard residue is *matchable*, the model is smarter than our rules, so pointing it at
the residue buys us coverage.

**What we found when we actually looked at the residue.** We instrumented the 99
train abstentions before writing a line of the adjudicator. They are not a pile of
recoverable matches — they are, almost entirely, cases where **abstaining is the
correct answer**:

  * **46 `near_dup`** — each credit has *exactly two* candidate batches with
    identical net totals, the same settlement date, and no UTR in the narration.
    There is no evidence, even in principle, to prefer one over the other.
  * **52 `split_1n`** — one settlement batch paid out as two bank credits that share
    the *same* UTR, differing only in amount. We tried the obvious "just raise the
    subset-sum cap" fix and it fails on its own terms: with the cap lifted, **29/52
    become AMBIGUOUS** (several distinct line-subsets sum to the credit within ±5
    paise) and **23/52 have no in-tolerance subset at all**. The tell: one example
    credit was for ₹1,88,144.07 against a 26-line batch whose two children sum
    exactly to the batch total — and the narration
    (`RTGS YESBN425218810242 RAZORPAYSOFTWAREPVTLTD`) carries **nothing** that says
    which 14 of the 26 lines are this credit's. The exact line partition is simply
    not recoverable from the evidence available.

So the deterministic + assignment layers had already reached the *precision-capped
coverage ceiling* on this data. A model asked to "recover" these can't — it can only
guess. And a guessing LLM in the last mile is exactly the "quietly says the wrong
thing with confidence" failure this whole project is built to avoid.

**How we handled it — re-scope the phase, and make the model structurally unable to
lie.** Two moves:

1. **Re-scoped the adjudicator's value** from *coverage* to *precision-preservation
   + auditability*. Its jobs are now: (a) never convert a correct abstention into a
   wrong match; (b) attach a human-readable reason to every exception for the
   drill-down queue (Phase 8's actual product); (c) recover *only* the sliver that
   has a genuine, checkable tell. On this synthetic data that sliver is ~0, and we
   decided that reporting that honestly is worth more than a fabricated lift.

2. **"The model proposes, deterministic guards dispose."** The model never sees
   ground truth and **never emits settlement ids**. It picks a *label* from a short
   list of concrete options we pre-compute (each a whole batch or a *uniquely*-summing
   subset, ids already worked out), or it says abstain/no_match. Every match it
   proposes then has to survive guards that are pure code, independent of the model:
   - **re-sum** the chosen lines ourselves — never trust the model's arithmetic
     (`amount_mismatch_on_recompute`);
   - the chosen lines must still be **free** (`lines_already_consumed`);
   - the **near-duplicate guard**: if two whole-batch options are tied on amount and
     date, abstain no matter what the model picked (`ambiguous_tie`);
   - the **contended-date guard** (the BUG-002 rule, reused here): the winner of a
     contended batch must be its strictly best-dated claimant, so a forged UTR can't
     let a late trap steal a batch (`lost_contention_on_date`);
   - self-reported confidence below the swept threshold ⇒ abstain (`below_threshold`);
   - an invalid / hallucinated option label ⇒ abstain (`invalid_option_label`);
   - the model saying `no_match` while a viable option exists ⇒ downgraded to abstain.

**How we proved it without an API key.** The adjudicator takes an injected
`call_fn`, so the tests drive it with a scripted model. The decisive test is
adversarial: a model that **matches the first option of every single hard case**.
Run end-to-end on the seed sample it still scores **100.0% precision, 0 hallucinated
traps** — of 10 credits it touched, 6 were blocked by the near-duplicate guard and 4
had no offerable option. Precision is owned by the guards, not the model, and the
test proves it. A second test (a model that abstains on everything) proves coverage
falls back exactly to the deterministic baseline and every touched credit still gets
a reasoning string for the queue. The zero-LLM pipeline is byte-for-byte unchanged
at 87.6% / 100% / 0.

**What we'd tell a reviewer.** The instinct on an "add an LLM" task is to measure the
coverage it buys. The more important measurement was the one that made us change the
design: *is the thing we're asking the model to do actually determinable from the
evidence?* Here it wasn't, so the honest, defensible build is an LLM that is
structurally prevented from turning a guess into a decision — and whose real output
is an **explained abstention**, which is precisely the product thesis ("willing to
say I don't know") made auditable. Physics still beats paperwork; we just moved the
same principle up to the model layer and made the model prove its work to a check it
can't talk its way past.

**Regression guard.** `tests/test_adjudicator.py` (12 tests): the happy path
(a genuine unique subset is recovered), every guard in isolation, and the two
end-to-end sanity checks (adversarial model can't break precision; passive model
preserves the baseline and annotates the queue).

**Open follow-up (deliberately not done this phase).** A *joint two-credit partition*
solver for `split_1n` (assign a batch's lines to its two sibling credits at once)
would be the principled way to recover some of these — but it is the same
subset-sum-ambiguity problem underneath, and it belongs in the deterministic layer
(Phase 4/5), not smuggled into the LLM step. Noted for later; not built now to avoid
scope creep and to keep the adjudicator honest.

---

## FINDING-004 — Phase 7: the learning loop, and the guard that lets us trust a "learned" match

**Severity:** design-level + one real bug fixed en route. The whole idea of Phase 7
— take an analyst's one-off resolution and *generalise* it into a rule that fires on
future data — is a precision hazard by construction: a generalisation is a guess about
data you haven't seen. So the interesting work was making a learned rule incapable of
lying, and the interesting bug was in the *learning* step itself.

**What Phase 7 does.** When an analyst resolves an exception, we induce a reusable
rule and persist it to JSON (`RuleStore`). On every later run the rules are consulted
*before* the deterministic layer; a credit they resolve is tagged
`resolved_by="learned_rule"`. The canonical case: a counterparty writes its payout
reference in a format our extractor can't read (`PYT-1784-5678-9012` — grouped digits
the UTR regex misses), so its credits look like near-duplicates and abstain. One
resolution teaches the format; the cohort auto-resolves.

**The precision design — same as Phase 6: rule proposes, guards dispose.** A learned
rule can only ever *surface a candidate reference* out of a narration. That reference
is accepted only if it (a) isn't one the base extractor already had, (b) resolves to a
real, still-free batch, and (c) that batch's free lines **re-sum to the credit within
±5 paise**. So an over-broad rule that scrapes arbitrary digits resolves to no batch,
and a rule that recovers a real-but-wrong reference is killed by the amount check. We
proved it with a deliberately reckless rule (`pattern=(\d+)`, capture *any* number,
no scope): it fires on every credit and still cannot move precision off 100% or
produce a single hallucination, because the money never agrees.

**The bug — the rule inducer swallowed the whole narration.** The first version of
`induce_rule` found "the run in the narration that equals the resolved reference" with
a regex that allowed whitespace as an intra-reference separator
(`[A-Za-z0-9]+(?:[-\s/][A-Za-z0-9]+)*`). On `"SETTLEMENT PYT-1784-5678-9012 CR"` that
regex greedily matched the **entire string** as one "run" (spaces included), which
normalised to `SETTLEMENTPYT178456789012CR` — not equal to the target
`PYT178456789012` — so no run matched and induction returned `None`. Symptom: the
experiment crashed with `'NoneType' has no attribute 'recover'`, i.e. *we learned
nothing from a resolution that plainly contained the reference*.

**Fix.** Stop trying to guess reference boundaries with a separator-tolerant regex.
Instead normalise the narration to alphanumerics while tracking each kept character's
original offset, find the target substring in that normalised text, and slice the
*exact* original span back out (`_find_ref_span`). That recovers `PYT-1784-5678-9012`
regardless of whether the bank used dashes, spaces, or slashes — and it generalises:
the induced pattern keeps the alpha anchor literal, abstracts digit groups to `\d+`,
and scopes the rule to narrations containing the prefix. One
`PYT-1784-5678-9012` now teaches `(PYT[-\s/]?\d+[-\s/]?\d+[-\s/]?\d+)`.

**Result (the loop, measured).** On the demo (`python -m src.rules.experiment`):
a single analyst resolution induces one rule that recovers **16/16** novel-reference
credits, lifting auto-resolve from **27.3% → 100.0%** at **100% precision, 0
hallucinations**. The main train/holdout runs are byte-for-byte unchanged (rules are
off unless a rules file is supplied): train 87.6 / 100 / 0, holdout 87.9 / 100 / 0.

**What we'd tell a reviewer.** The learning loop is the part everyone wants (the
system gets better as analysts use it) and the part most likely to quietly regress
precision (yesterday's fix becomes tomorrow's false match). The move that makes it
safe is refusing to let "learned" mean "trusted": a rule earns nothing on its own
authority — it only points, and the same arithmetic guard the rest of the pipeline
uses decides whether the pointer is right. The bug is a good reminder in the other
direction: be as suspicious of your *induction* as of your inference — a learner that
silently learns nothing is its own kind of failure.

**Regression guard.** `tests/test_learned.py` (10 tests): induction generalises and
scopes correctly, handles space- and dash-grouped formats, and returns `None` when the
reference is genuinely absent; application recovers a match when the amount agrees and
refuses when it doesn't; the base-extractor's own references aren't re-stolen;
`RuleStore` round-trips; the end-to-end loop lifts the cohort at 100% precision; and
the reckless-rule test proves an over-broad rule can't break precision.

---

## Note — two test-authoring corrections (not product bugs, but worth being honest about)

While writing the regression tests above we tripped twice on our own tests, and it's
worth recording because both are easy traps for anyone extending the suite:

- **Comparing timing in a determinism check.** Our first determinism test compared
  the *entire* result object across two runs and failed — because each result carries
  a measured `latency_ms`, which of course differs run-to-run. The decisions were
  identical; only the stopwatch moved. Fix: compare the decision fields, not the
  measured latency.

- **Assuming subset-sum resolves every split.** A test assumed the sample's
  `split_1n` credits would resolve via subset-sum. On that particular 61-record
  sample the split batches have 28–38 lines, over our subset-sum size cap (26), so
  they correctly *abstain* instead. The system was right; the test's assumption was
  wrong. Fix: prove the subset-sum path with a small constructed split, and only
  assert the *safety* property (never wrongly matched) on the sample.

---

## BUG-003 — After "Re-run with learned rules", the console fetched `/api/run/undefined/...`

**Severity:** low (cosmetic — the visible flow still worked), but a real
client/server contract break, and exactly the kind of thing that puts red errors in
the browser console during the money-shot of a live demo. Found during the Phase-8
end-to-end UI check, not by a test.

**Symptom.** Driving the full learning-loop demo in the browser (load `learn_demo` →
open a parked credit → pick a batch → **Match** → **Re-run with learned rules**), the
dashboard and queue updated correctly to 100% coverage — but the browser console
carried two `404 Not Found` errors: `GET /api/run/undefined/exceptions`,
`{"detail":"run not found"}`. The lift still showed, so it was easy to miss.

**Root cause — a run-scoped response that dropped its own id.** The single React
`run` state object is the app's identity for the current run; the exception queue
renders as `<Exceptions runId={run.run_id} />` and re-fetches whenever `runId`
changes (`useEffect([runId])`). The `reconcile_*` endpoints return
`{run_id, name, **metrics_payload}`, so `run.run_id` is populated on load. But
`POST /rerun` returned **only** `metrics_payload(run) + coverage_delta_records` — no
`run_id`. The client feeds that response straight into `setRun(m)`, so after a re-run
the `run` object lost its id. React then re-rendered `<Exceptions runId={undefined}>`,
whose effect refired and fetched `/api/run/undefined/exceptions` → 404. The on-screen
numbers survived only because the re-run handler's own `await load()` ran first, while
the stale (still-valid) `runId` was captured in its closure — the broken fetch came
*after*, from the prop change. A latent bug masked by call ordering.

**Fix — make the run-scoped response carry the run's identity.** One line in
`rerun()`: return `{"run_id": run.id, "name": run.name, **metrics_payload(run)}` plus
`coverage_delta_records`, the same shape `reconcile_*` already returns. Now `setRun`
keeps a complete object, `runId` never goes `undefined`, and the post-re-run effect
fetches the real id.

**Result.** Re-ran the whole browser flow after the fix: identical lift
(27.3% → 100.0% coverage at 100% precision, +15 auto-resolved), and **0 console
errors**; the server log shows every request carrying the real run id and no
`undefined` path. No frontend rebuild needed — the bug and fix were both server-side.

**What we'd tell a reviewer.** When one response object *is* a client's identity for a
resource, every endpoint that returns "the new state of that resource" has to return
the identity too — a partial payload silently amputates it on the next `setState`.
And a bug that hides behind closure-capture ordering is worth fixing precisely because
it's invisible until the render sequence shifts: it works until it doesn't, for
reasons unrelated to the change that finally exposes it. The broader lesson: the UI
check earns its keep — this never fires in the API tests, only in a real browser doing
the state transitions in order.

---

## FINDING-006 — Phase 10: integrating the real Razorpay API without touching the engine

**Context.** The Buildathon requires integrating a Razorpay API. The temptation with a
"required integration" is to bolt on a token API call that doesn't really belong. We
looked for the integration that was *already* the thesis, and Razorpay's **Settlement
Recon Report API** turned out to be an uncanny fit: per settled transaction it returns a
`settlement_id` (the payout batch), a `settlement_utr` (the reference echoed in the bank
narration), the money (`amount`/`fee`/`tax`/`credit`/`debit`), the line `type`, and the
linked `order_id`. That is two of our three inputs — settlements and orders — straight
from the source.

**What we built.** `src/ingest/razorpay_source.py`: `fetch_recon()` calls
`client.settlement.report(...)` (the official SDK wrapping
`GET /v1/settlements/recon/combined`), and `recon_items_to_models()` maps the items into
the exact same `Order` / `SettlementLine` / `BankCredit` models the file loader produces.
Items sharing a `settlement_id` roll up into one `BankCredit` (net = Σ(credit − debit),
UTR-bearing narration), and since we know each batch's composition we emit ground truth
too, so a live run is scorable. A CLI (`razorpay_pull.py`) writes a standard dataset
directory, so `report`, the console, and the tests all consume live data **with zero
downstream changes**.

**The design choice that mattered.** Only `fetch_recon` touches the network;
`recon_items_to_models` is a pure function. That is the same injectable-seam discipline
the adjudicator uses (`call_fn`): the API is one boundary, everything else stays
deterministic and unit-tested. The payoff is concrete — the repo ships a real-shaped
recon payload (`data/razorpay_recon_sample.json`), so the whole path (map → reconcile →
100% precision, including a refund line that makes a batch net to less than its gross)
runs in CI with **no key and no network**, and lights up against live `rzp_test_*` keys
by swapping the one seam. The engine never learned that its data now comes from an API.

**Regression guard.** `tests/test_razorpay_source.py` (5 tests): the mapping's shapes and
money (incl. the negative refund line), ground truth matching batch composition,
`settled: false` items skipped, the paginating fetch driven by an injected fake client,
and an end-to-end reconcile of live-shaped data at 100% precision via `exact_ref`.
