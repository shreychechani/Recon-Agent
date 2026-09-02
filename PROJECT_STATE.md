# PROJECT_STATE.md — Reconciliation Agent (session handoff)

> **Read this first at the start of every session.** It is the A-to-Z of where the
> build is, how to run it, the design decisions that are locked, and — most
> importantly — the reasoning already worked out for phases not yet coded, so a
> cold start doesn't re-derive (or get wrong) the subtle bits (trap mechanics,
> matcher ordering, consumed-line tracking).
>
> Keep this file updated at the end of each working session.
> Last updated: 2026-09-01 (end of Phase 9 — README rewritten + ARCHITECTURE.md written;
> train/holdout runs captured to reports/ (train 87.6/100/0, holdout 87.9/100/0). An
> end-to-end UI check preceded this and turned up BUG-003 (fixed). **All phases 0–9 DONE
> & tested; 73 tests pass.** Zero-LLM baseline unchanged). See ENGINEERING_LOG.md for the
> write-ups: BUG-001 dup_utr subset-sum + nondeterminism, BUG-002 forged-UTR assignment,
> FINDING-003 (Phase-6 adjudicator = precision guard + explainer), FINDING-004 (Phase-7
> learning loop), FINDING-005 (Phase-8 drill-down re-derived server-side; stale-venv
> shebang gotcha), BUG-003 (Phase-8 re-run response dropped run_id → /run/undefined 404).

---

## 0. What this is

Three-way settlement reconciliation for Indian payments. Ingests an order ledger,
a gateway settlement file, and a bank statement; works out which settlement
batches correspond to which bank credits. **Core thesis: willing to say "I don't
know."** Headline metric = **auto-resolve rate at 100% precision**, not match rate.
Deterministic first (≈80% resolve with zero LLM calls), model last (one decision
point). Razorpay AI Buildathon Track 4. Deadline 5 Sep 2026, target 1 Sep.

Full build spec is the long doc the user pasted in the first message of the
original session — treat that as the source of truth for requirements. This file
is the *implementation state*.

---

## 1. Environment & how to run

- Working dir: `/Users/shreychechani/Desktop/Razorpay/recon-agent` (project moved from
  the old `Desktop/Stripe/...` path — update any stale references you see).
- Python 3.11 venv managed by **uv**. Interpreter: `.venv/bin/python`.
- NOT a git repo yet. `git init` if the user wants version control.
- Deps installed via `uv pip install -e ".[dev]"`. Core: pydantic v2, duckdb, scipy, numpy, anthropic, python-dotenv, openpyxl, fastapi, uvicorn, pytest.

Common commands (run from `recon-agent/`):
```bash
# regenerate data (train seed 42, holdout seed 7, committed seed sample seed 123)
.venv/bin/python -m src.generator.generate --records 800 --seed 42 --out data/generated/train/
.venv/bin/python -m src.generator.generate --records 800 --seed 7  --out data/generated/holdout/
.venv/bin/python -m src.generator.generate --records 60  --seed 123 --out data/seeds/

# score a matcher against ground truth
.venv/bin/python -m src.eval.report --data data/generated/train/ --matcher stub    # abstain-all
.venv/bin/python -m src.eval.report --data data/generated/train/ --matcher pipeline # once pipeline exists

# tests
.venv/bin/python -m pytest tests/ -q
```

**LLM is OFF by default** (no `ANTHROPIC_API_KEY` in env). The whole pipeline runs
without it — schema mapping falls back to the alias table, adjudication will abstain.
To enable: put key in `.env` (see `.env.example`). Model = `claude-sonnet-4-6`.
`RECON_DISABLE_LLM=1` force-disables even with a key (used for the zero-LLM baseline
and CI). All SDK usage is isolated in `src/llm.py`.

---

## 2. Phase status

| Phase | What | Status |
|---|---|---|
| 0 | models + synthetic generator | ✅ DONE, tested |
| 1 | eval harness + report | ✅ DONE, tested (stub verified 0% cov / 100% prec) |
| 2 | ingest (loader) + schema mapping | ✅ DONE, tested (round-trips exactly) |
| 3 | candidate generation (DuckDB) | ✅ DONE, tested (624× reduction, 100% recall) |
| 4 | deterministic matcher (exact/fee/subset) | ✅ DONE, tested (100% prec; fixed BUG-001) |
| 5 | global bipartite assignment (scipy) | ✅ DONE, tested (100% prec; fixed BUG-002; greedy<global) |
| 6 | LLM adjudicator + threshold sweep | ✅ DONE, tested (propose/guard; adversarial model can't break precision; see FINDING-003) |
| 7 | learned-rule learning loop | ✅ DONE, tested (induce→verify→persist; 1 resolution lifts a cohort 27→100% @ 100% prec; see FINDING-004) |
| 8 | FastAPI + React UI | ✅ DONE, tested (exception-queue drill-down + 2-click resolve→learn→re-run; fixed BUG-003 client/server contract) |
| 9 | README, ARCHITECTURE.md, holdout run | ✅ DONE (README + ARCHITECTURE.md written; train/holdout captured to reports/) |

**Phase 7 note.** Analyst resolutions → reusable narration rules (`src/rules/learned.py`),
persisted to JSON (`RuleStore`), consulted BEFORE deterministic (`resolved_by="learned_rule"`).
Precision-safe by the same discipline as Phase 6: a rule only *surfaces a candidate
reference*; the match is accepted only if it resolves to a real free batch whose lines
re-sum to the credit within ±TOL. Rules OFF by default (empty/no rules file) → train/holdout
unchanged. Demo: `python -m src.rules.experiment` (novel `PYT-####-####-####` reference the
extractor can't parse; one resolution recovers the whole cohort). `reconcile(orders,
settlements, credits, rules=[...])` is the in-memory entry point (no file IO) used by the
experiment + tests. Phase 7 is the *engine*; the analyst UI that mints rules is Phase 8.

**Phase 6 note (important, non-obvious).** The residue Phase 6 runs on is, on this
synthetic data, almost entirely *correct-to-abstain* (46 near_dup + 52 split_1n, both
genuinely ambiguous — see FINDING-003). So the adjudicator is precision-first, not a
coverage lever: the model picks among pre-enumerated concrete OPTIONS (never emits
settlement ids) and every proposed match is re-verified by deterministic guards
(re-sum; lines free; near-dup tie; contended-date/BUG-002; confidence threshold;
valid label). Zero-LLM run is unchanged (87.6/100/0). To run WITH the model: set
`ANTHROPIC_API_KEY`, then `report ... --adjudicate`; tune τ on train via
`harness.pick_threshold`, report it on holdout. `reconcile_dataset(adj_call_fn=...)`
injects a scripted model for tests.

Tasks are tracked in the harness Task list (TaskList tool). Task IDs 1–10 map to
phases 0–9. As of now tasks 1–4 completed (phase 0,1,2,3), task 5 = Phase 4 is next.
(Note: task numbering is offset by one — task #N is "Phase N-1".)

**Checkpoint rule (from spec §18):** after Phase 4, deterministic layer must resolve
≥70% or stop and fix. Given candidate recall is 100% and most credits are easy/medium
with clean/uncovered UTR, we expect ~75–85%.

---

## 3. Repo map (what each file does)

```
src/
  models.py         Pydantic: Order, SettlementLine (+payout_ref), BankCredit,
                    GroundTruth, MatchResult, Adjudication. ALL MONEY = int paise.
  fees.py           Fee model (2% fee, 18% GST), ROUND_HALF_UP via Decimal.
                    Shared by generator + matcher so they agree to the paise.
                    ROUNDING_TOLERANCE_PAISE = 5.
  llm.py            ONLY place the Anthropic SDK is touched. structured_call()
                    uses client.messages.parse(output_format=PydanticModel) ->
                    .parsed_output. available() gates on key + RECON_DISABLE_LLM.
                    Never raises (returns ok=False). Computes cost from usage
                    (sonnet 4.6: $3/$15 per MTok). cache_dir() -> .cache/.
  generator/
    generate.py     CLI + all tier builders + file writers. --records = # credits.
    corruptions.py  whitespace/lowercase/truncate/UTR-transpose/rounding-drift.
    narration.py    8-12 templates by UTR exposure: clean/noisy/truncated/absent,
                    plus salary/vendor trap narrations. make_utr().
  ingest/
    loader.py       Reads xlsx/csv/json -> canonical models. RUPEES->PAISE ONLY
                    HERE (Decimal). parse_dt/parse_date handle 3 formats.
                    _find_header_row skips junk xlsx rows generically.
    schema_map.py   LLM touchpoint #1. Deterministic alias table (covers all our
                    formats -> no LLM needed) + LLM fallback + hash-keyed cache
                    in .cache/schema_map.json. FIELDS/ALIASES per file type.
  match/
    candidates.py   DuckDB blocking. CandidateIndex(settlements): aggregates
                    lines->batches, indexes on settled_date + net. for_credit()
                    returns CreditCandidates{candidates, pool_line_ids, latency}.
                    extract_utrs() strong+weak tokens. _ref_strength 1.0/0.6/0.0.
                    Also carries line_type + raw narration for the adjudicator.
    deterministic.py  ✅ 4a/4b batch-total (fixpoint, contention-aware) + 4c bounded
                    subset-sum (meet-in-middle, unique/AMBIGUOUS/None). unique_subset
                    now takes a `cap` (Phase 6 raises it to 32).
    assignment.py     ✅ Phase 5 scipy max-weight one-to-one. score_edge, date guard.
    adjudicator.py    ✅ Phase 6. build_options() -> concrete OPTIONS (whole_batch /
                    unique subset) + NOTES; adjudicate() calls the model (injectable
                    call_fn) per credit; _finalize_verdict() runs the precision guards.
                    Model picks a LABEL, never emits ids. DEFAULT_THRESHOLD=0.70.
  rules/learned.py    ✅ Phase 7. LearnedRule + RuleStore (JSON). induce_rule()
                    generalises one resolution (alpha anchor literal, digits->\d+,
                    scoped by prefix); apply_rules() recovers a ref + re-verifies
                    (batch free, lines free, re-sum within TOL). run_rules() = the
                    consumed-tracked pass. experiment.py = the before/after demo.
  eval/
    harness.py      score(results, truth) -> Metrics. is_correct(). tradeoff_curve().
                    pick_threshold() (max coverage @ 100% precision). abstain_all()
                    stub. load_ground_truth/load_bank_txn_ids.
    report.py       formats metrics table, per-tier, sources, ASCII tradeoff curve;
                    prints the swept threshold. --adjudicate / --no-adjudicate flags.
                    run_matcher(dir, "stub"|"pipeline"). --out writes JSON.
  api/main.py         ⬜ TODO Phase 8
  pipeline.py         ⬜ TODO — reconcile_dataset(dir) -> list[MatchResult].
                    report.py's "pipeline" matcher imports this lazily.
data/
  generated/{train,holdout}/  gitignored, regenerable. seed 42 / seed 7.
  seeds/                      committed 60-credit sample (seed 123).
  each dir has: orders.xlsx, settlements.csv, bank.json, ground_truth.json
tests/  test_generator, test_harness, test_ingest, test_candidates (32 tests, all pass)
```

---

## 4. Locked design decisions (do NOT revisit — put verbatim in README §3)

- **No vector DB** — matching needs provable equality + bounded arithmetic tolerance, not semantic similarity.
- **No graph DB** — candidate narrowing is constraint satisfaction over a flat set (range predicates), not multi-hop traversal; indexed DuckDB scan is faster, zero infra.
- **No agent framework / multi-agent** — fixed pipeline, one model call at one point.
- **Global bipartite assignment, not greedy** — enforces one-record-one-match invariant.
- **LLM only for schema mapping + final adjudication** — the two genuine semantic-judgment tasks.

Extra shared module added beyond spec's file list: `src/fees.py` and `src/llm.py`
(justified: DRY fee model, single SDK boundary). Mention in README.

---

## 5. Data model & invariants (verified by tests)

- Money is `int` paise everywhere in memory. Rupee strings only on disk.
- Fee: `fee = round(gross*0.02)`, `gst = round(fee*0.18)`, `net = gross-fee-gst`
  (ROUND_HALF_UP). Refund/chargeback lines: negative gross, fee=gst=0, net=gross.
- A **batch** (payout_batch_id) = 15–60 lines, shares one `payout_ref` (the UTR).
  Batch net total = sum of line net_paise = the bank credit amount.
- **Invariant (tested):** each non-trap credit amount == sum(net of its GT
  settlement_ids) within ±5 paise. No settlement line is claimed by two credits.
- Ground truth `settlement_ids == []` ⟺ trap (unmatchable). Matcher must NEVER read
  ground_truth.json.

### Difficulty tiers & counts (records=800): easy 360 / medium 240 / hard 160 / trap 40
- **easy**: clean UTR, single batch, no negatives, value_date = T+2.
- **medium**: 1–2 of {refund_netted, date_drift t1/t3, utr_in_noise, partial_refund}.
- **hard** scenarios (each 2+ corruptions):
  - `chargeback_drift`: chargeback line + ±1-3 paise rounding drift + truncated UTR.
  - `split_1n`: ONE batch → TWO credits (subsets), clean UTR kept + rounding drift.
  - `near_dup`: TWO batches same date with IDENTICAL net totals, UTR absent → genuinely ambiguous → correct behavior is ABSTAIN.
  - `utr_absent_cb`: UTR absent + chargeback → amount+date only.
  - `truncated_refund`: truncated UTR + refund + rounding drift.
- **trap** types (GT empty):
  - `trap_dup_utr`: reuses a real credit's UTR + amount, value_date+1. Batch IS a candidate → only one-to-one assignment refuses it.
  - `trap_unrelated`: salary/vendor, ROUND rupee amount (won't equal any fee-adjusted net), in-window date. Usually 0 candidates.
  - `trap_subset_of_matched`: amount = subset-sum of a MATCHED batch's lines, UTR absent. The sharpest trap — lines get consumed by their batch's exact match, so no free subset remains. Catches greedy subset-sum.
  - `trap_out_of_window`: value_date 15–40d after settlement → no candidates.

---

## 6. Numbers achieved so far

- Generator: tiers exactly on target; all 4 trap types present; invariants hold.
- Ingest: schema mapping 100% deterministic on our formats; money round-trips with
  0 mismatches on all 760 non-trap credits.
- Candidate gen (train, 734 batches): **~1.2 candidates/credit (624× reduction, max 4)**,
  **100% recall** (true batch retained for all 760 matchable credits),
  latency p50 0.9ms / p99 1.4ms. 27/40 traps already get 0 candidates.
- Harness verified: abstain-all stub → 0% coverage, 100% precision, 0 hallucinations.

---

## 7. Reasoning already worked out for REMAINING phases (implement per this)

### Phase 4 — deterministic.py (zero LLM). THREE strategies, tried in order.
Critical: process is **global with consumed-line/consumed-batch tracking**, because
the traps depend on it.

- **4a Exact reference:** a credit whose extracted UTR == a batch payout_ref AND
  amount agrees within ±5 paise → match, confidence 1.0.
  **Contention rule (catches trap_dup_utr):** if TWO credits exact-match the SAME
  batch, do NOT auto-resolve either — defer both to assignment (Phase 5), which
  enforces one-to-one and gives the batch to the better-scoring credit (the real
  one, T+2, higher date proximity); the dup ends up no_match.
  Implement: build map batch_id -> [credits whose ref matches & amount ok]; only
  auto-match batches with exactly one contender. Mark batch + its lines consumed.
- **4b Fee-adjusted amount:** for a credit with exactly ONE candidate batch whose
  recomputed net (from gross via fees.py, incl. negative lines) == amount within
  ±5 paise → match, confidence 0.95. (Handles UTR-absent single-candidate cases.)
  Skip batches already consumed. Mark consumed on match.
- **4c Bounded subset-sum:** for N:1 (split_1n) and any leftover. Pool = candidate
  batch lines (UTR-matched) OR `pool_line_ids` (window lines) MINUS consumed lines.
  Cap 60. DP over paise with ±5 tolerance band. **Early-exit if >1 distinct subset
  hits target** → ambiguous, pass downstream (do NOT pick). Single unique subset →
  match, confidence ~0.9. This is where split_1n resolves (UTR present → pool =
  that batch's lines; each credit finds its subset). The subset trap fails here
  because its lines are consumed by their batch's 4a/4b match (process exact/fee
  for ALL credits before subset-sum).
- Everything unresolved / ambiguous → carry to Phase 5 with evidence.
- Report the zero-LLM resolution rate here (headline). Expect 75–85%.

### Phase 5 — assignment.py (scipy.optimize.linear_sum_assignment)
- Build bipartite graph: unresolved credits × their candidate batches (free, not consumed).
- Edge weight = 0.5*ref_strength + 0.3*amount_closeness + 0.2*date_proximity.
  - amount_closeness: 1.0 at exact, decay across the ±band to 0.
  - date_proximity: 1.0 at T+2, decay either side.
- linear_sum_assignment (pad to square with dummy/zero edges). Enforce one-to-one:
  no batch claimed twice. This is what refuses trap_dup_utr (real credit wins the batch).
- Accept assignments scoring above a HIGH threshold directly (confidence from score).
  Rest → adjudicator with scores as evidence.
- **Required experiment:** run whole pipeline with greedy vs global assignment on
  holdout; report Δ precision/coverage. Greedy = process credits in order, first
  come claims batch. Show global wins (esp. dup_utr / near_dup).

### Phase 6 — adjudicator.py (LLM touchpoint #2, via src/llm.py)
- Runs only on Phase-5 survivors (~10–20%). Input: credit (amount/date/raw narration),
  each candidate batch (composition + computed score), fee model. Output = Adjudication
  (already in models.py): decision match/no_match/abstain + settlement_ids + confidence
  + reasoning + rejected_candidates.
- Prompt MUST say: abstain when evidence insufficient; abstaining is correct, not
  failure. Include a worked abstain example (near_dup: two identical-total batches,
  no UTR → abstain).
- confidence < threshold → force abstain. Tune threshold on TRAIN only via
  tradeoff_curve(); pick highest-coverage point holding 100% precision on train;
  report what that threshold gives on holdout.
- Batch requests where possible; retry/backoff handled by SDK max_retries; on
  persistent failure ABSTAIN, never guess. Record tokens/cost (llm.py does this).
- Since no key by default, adjudicator abstains on everything in the zero-LLM run —
  that's fine; coverage comes from deterministic + assignment.

### Phase 7 — rules/learned.py
- Analyst resolution in UI -> reusable rule (e.g. "narration prefix MB:SETTLE -> UTR
  at offset 10"; "counterparty X settles T+3"). Persist to a JSON. Consulted at
  Phase 4 BEFORE the LLM (as resolved_by="learned_rule").
- Experiment: resolve 20 exceptions, re-run, report auto-resolve lift.

### Phase 8 — api/main.py (FastAPI) + frontend/ (React+Vite+Tailwind)
- POST /reconcile (3 files) ; GET /run/{id}/metrics ; GET /run/{id}/exceptions ;
  POST /exception/{id}/resolve.
- Screens: Upload; Dashboard (metrics, tradeoff curve, per-tier table, source
  distribution, cost/latency); **Exception queue with drill-down** (candidates
  considered + rejection reasons + confidence + which layer gave up) + 2-click resolve.
  Spend UI effort on the drill-down — it's the product.

### Phase 9 — README (spec §19 order), ARCHITECTURE.md, final holdout metrics table (spec §16).

---

## 8. `src/pipeline.py` (to build alongside Phase 4)
Orchestrates: load_dataset(dir) -> CandidateIndex -> for each credit run learned
rules -> deterministic (4a/4b/4c) -> assignment -> adjudicator, producing a
`list[MatchResult]` (one per credit, decision matched/no_match/abstain, resolved_by,
confidence, evidence, llm_calls, cost_usd, latency_ms). `report.py --matcher pipeline`
imports `reconcile_dataset` from here. Must set decision="no_match" when a credit has
zero candidates AND no subset (that's how out_of_window / unrelated traps resolve
correctly as no_match — good for coverage AND precision).

Design note on no_match vs abstain: a credit with no viable candidate at all →
**no_match** (confident it doesn't reconcile) — this is correct for traps and counts
as coverage. A credit with candidates but insufficient evidence to choose → **abstain**
(exception queue). Getting this split right is what makes trap handling score well.

---

## 9. Gotchas / non-obvious

- `for -m` runs: cwd must be `recon-agent/`; imports are `src.xxx`.
- openpyxl amounts were written as STRINGS ("499.00") on purpose to force Decimal path.
- settlements.csv dates are `DD-MMM-YY` (e.g. `29-Jan-26`); orders `DD/MM/YYYY`;
  bank `YYYY-MM-DD`. loader handles all + lowercase month via `.title()`.
- Batch settled_date in candidates = min(line settled_at date). value_date window is
  [value_date-4, value_date]; all legit tiers (T+1..T+3) fall inside.
- Rounding drift is injected on ONE line only (≤3 paise), so batch total stays within
  ±5 of recomputed — do not raise tolerance.
- near_dup batches have EXACTLY equal net totals (shared gross list) — every line stays
  fee-consistent (do not reintroduce the old net-nudge that broke test_fee_model...).
- Data is shuffled before writing bank.json/ground_truth.json (order isn't a leak).

---

## 10. Test status: 67 passing
`test_generator.py`, `test_harness.py`, `test_ingest.py`, `test_candidates.py`,
`test_deterministic.py`, `test_assignment.py`, `test_adjudicator.py` (12 — scripted
model: unique-subset recovery, each precision guard, adversarial + passive end-to-end),
and `test_learned.py` (10 — induction/generalisation, verified application + amount
guard, base-ref not re-stolen, RuleStore round-trip, end-to-end loop lift, reckless-rule
precision). Run `pytest tests/ -q`. The whole suite needs no API key (scripted
`call_fn` for the model; learned rules are pure code).
