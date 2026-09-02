# recon-agent

Three-way settlement reconciliation for Indian payment flows. It ingests an **order
ledger**, a **payment-gateway settlement file**, and a **bank statement**, and works
out which settlement batches correspond to which bank credits — **while being willing
to say "I don't know."**

> **Deterministic first, model last.** ~88% of records resolve with zero LLM calls;
> the model is invoked at exactly one decision point, on the ambiguous remainder.
> The headline metric is **auto-resolve rate at 100% precision**, not match rate.

Built for the Razorpay AI Buildathon (Track 4).

---

## The thesis: precision over coverage

A reconciliation tool that is 98% accurate is not 98% useful — it is a tool an analyst
can never trust, because they still have to re-check every line to find the 2% it got
wrong. The only number that lets a human stop checking is **precision = 100%**. So this
system optimises for auto-resolve rate *subject to* never being wrong, and it earns that
by **abstaining** whenever the evidence is ambiguous. An abstention is not a failure — it
is a correct, honest "route this to a human." A wrong auto-match is the only real
failure, and the design spends everything to drive it to zero.

Concretely that means:

- **Deterministic layers do the bulk of the work** (exact-reference, fee-adjusted
  amount, bounded subset-sum) with no model and no cost.
- **A global assignment step** resolves contention (several credits eyeing one batch)
  with a one-to-one optimum — and refuses to be fooled by a forged reference.
- **The LLM is the last touchpoint, not the first.** It only ever sees the contended
  remainder, it proposes a *label* from pre-enumerated options, and **every proposal is
  re-verified by the same deterministic guards** before it counts. The model can
  propose; only arithmetic disposes.
- **A learning loop** turns each analyst resolution into a reusable, *verified* rule —
  the system gets better as it is used, without ever letting "learned" mean "trusted."

---

## Results

Two 800-record datasets: **train** (seed 42, the set thresholds were tuned on) and
**holdout** (seed 7, never tuned on). Both run with the LLM **off** — these are the
zero-cost, zero-LLM baseline numbers.

| Metric                              |     Train |   Holdout |
| ----------------------------------- | --------: | --------: |
| Records                             |       800 |       800 |
| **Auto-resolve rate (coverage)**    | **87.6%** | **87.9%** |
| **Precision on decided records**    |  **100%** |  **100%** |
| **Hallucinated trap matches**       |  **0/40** |  **0/40** |
| Resolved with zero LLM calls        |      100% |      100% |
| Cost per record                     |     $0.00 |     $0.00 |
| Latency per record (p50 / p99)      | 0.74 / 0.85 ms | 0.75 / 0.85 ms |

**Coverage holds 100% precision across the entire confidence sweep** (τ = 0.00 → 0.90),
because the deterministic layers only ever emit high-confidence matches. The
highest-coverage threshold that still holds 100% precision is τ = 0.00.

Per-difficulty (holdout): `easy` 360/360, `medium` 240/240 — both 100% coverage;
`hard` 66/160 (41.2%) — the deliberately-ambiguous splits and near-duplicates the
system *correctly* declines to guess; `trap` 37/40 decided, **all correct** (the traps
are finalised as confident `no_match`, not mis-matched).

Where the resolutions come from (holdout): exact reference 71.6%, fee-adjusted amount
10.6%, global assignment 1.0%, abstained 16.8%.

Captured reports live in [`reports/`](reports/) (`holdout_report.txt`,
`holdout_metrics.json`, `train_metrics.json`). Regenerate any time — the pipeline is
deterministic across hash seeds, so the numbers reproduce byte-for-byte.

---

## Quick start

```bash
# 1. environment (Python 3.11, uv)
uv venv && uv pip install -e ".[dev]"

# 2. generate the datasets (deterministic; seeds are the dataset identity)
python -m src.generator.generate --records 800 --seed 42 --out data/generated/train/
python -m src.generator.generate --records 800 --seed 7  --out data/generated/holdout/

# 3. score the pipeline against ground truth
python -m src.eval.report --data data/generated/holdout/ --matcher pipeline

# 4. run the tests
python -m pytest tests/ -q
```

The whole pipeline runs **without an API key** — schema mapping falls back to an alias
table and the adjudicator simply abstains, so you get the full zero-LLM baseline above.
To enable the LLM adjudicator, put a key in `.env` (see `.env.example`) and add
`--adjudicate`. Model: `claude-sonnet-4-6`. Force the zero-LLM path explicitly with
`RECON_DISABLE_LLM=1`.

### The console (Phase 8)

A FastAPI backend serves a React/Vite/Tailwind exception-queue console. The product is
the drill-down: an analyst inspects a parked credit (narration, extracted references,
every candidate batch with its amount/date deltas), resolves it in two clicks, and the
resolution mints a learned rule that a re-run applies across the whole batch — the
coverage lift is shown live.

```bash
# build the frontend once, then serve everything (SPA + /api) from one origin
cd frontend && npm install && npm run build && cd ..
python -m uvicorn src.api.main:app --port 8000
# open http://localhost:8000  → try the "Learning-loop demo" dataset
```

For live frontend development, run `npm run dev` (Vite on :5173, proxying `/api` to the
backend on :8000) alongside the uvicorn command above.

---

## How it works (one paragraph)

Each bank credit is matched through an ordered cascade, and **the first layer confident
enough to be safe wins**; anything ambiguous falls through to the next layer, and
whatever survives the whole cascade is abstained (routed to a human). Layers share a
global *consumed-line / consumed-batch* ledger, so a settlement line can only be spent
once — which is what makes the traps fail safely. See **[ARCHITECTURE.md](ARCHITECTURE.md)**
for the full walkthrough of the cascade, the trap taxonomy, and the guards.

---

## Project layout

```
src/
  generator/      synthetic data + ground truth (orders, settlements, bank, traps)
  ingest/         schema mapping (alias table, LLM fallback) + dataset loader
  fees.py         the settlement fee model (gross → net)
  models.py       pydantic types: Order, SettlementLine, BankCredit, MatchResult, …
  match/
    candidates.py    per-credit candidate index (UTR/amount/date evidence)
    deterministic.py Phase 4 — exact-ref, fee-adjusted, bounded subset-sum
    assignment.py    Phase 5 — global max-weight one-to-one assignment
    adjudicator.py   Phase 6 — LLM adjudicator (propose-a-label, guards dispose)
  rules/
    learned.py       Phase 7 — induce & apply verified narration rules
    experiment.py    the learning-loop lift experiment
  pipeline.py     orchestration: reconcile() / reconcile_dataset()
  eval/           report.py (scoring + tradeoff curve) + harness.py (threshold sweep)
  llm.py          thin Anthropic client wrapper (off by default)
  api/main.py     FastAPI backend for the console
frontend/         React + Vite + Tailwind exception-queue console
tests/            73 tests; the whole suite needs no API key
```

---

## Engineering log

Every non-trivial bug and design finding is written up in
**[ENGINEERING_LOG.md](ENGINEERING_LOG.md)** — including the two precision bugs found en
route (a hallucinated duplicate-UTR match, and a forged-reference assignment), the
finding that the LLM's real job is the *opposite* of what we first assumed, the design
of the learning loop's safety guard, and the client/server contract bug the end-to-end
UI check turned up. It is the honest account of what broke and why the fixes hold.
