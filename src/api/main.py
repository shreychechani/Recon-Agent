"""Phase 8 — FastAPI backend for the reconciliation console.

Three things the product has to do, and this file exposes each as an endpoint:

  1. Reconcile a set of files and report the headline metrics + tradeoff curve.
  2. Hand the analyst the *exception queue* — every credit the pipeline abstained on,
     with the full drill-down: the candidate batches it considered, why each was
     rejected, which layer gave up, and the model's reasoning where it ran. This is
     the product; the response shape here is deliberately rich.
  3. Let the analyst resolve an exception in two clicks, and turn that one resolution
     into a persisted learned rule (Phase 7) so the next run does it automatically —
     the loop, closed and visible via a re-run that shows the coverage lift.

State is in-process (a demo console, one analyst): runs live in ``RUNS``; learned
rules persist to JSON via the shared ``RuleStore`` so they survive across runs and
restarts. The pipeline runs with no API key (adjudicator abstains); set
``ANTHROPIC_API_KEY`` to light up the LLM layer.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from src.eval.harness import pick_threshold, score, tradeoff_curve
from src.ingest import loader
from src.match.candidates import CandidateIndex
from src.models import BankCredit, GroundTruth, MatchResult, Order, SettlementLine
from src.pipeline import reconcile
from src.rules.learned import RuleStore, induce_rule

RULES_PATH = Path(".cache/learned_rules.json")
SAMPLE_DIRS = {
    "train": "data/generated/train",
    "holdout": "data/generated/holdout",
    "sample": "data/seeds",
    "learn_demo": "data/generated/learn_demo",
}

app = FastAPI(title="Reconciliation Console", version="0.8")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# In-process run registry
# --------------------------------------------------------------------------- #


@dataclass
class Run:
    id: str
    name: str
    created_at: str
    orders: list[Order]
    settlements: list[SettlementLine]
    credits: list[BankCredit]
    truth: list[GroundTruth] | None
    results: list[MatchResult]
    resolutions: dict[str, dict] = field(default_factory=dict)  # txn_id -> resolution

    @property
    def credit_by_id(self) -> dict[str, BankCredit]:
        return {c.bank_txn_id: c for c in self.credits}

    @property
    def result_by_id(self) -> dict[str, MatchResult]:
        return {r.bank_txn_id: r for r in self.results}


RUNS: dict[str, Run] = {}


def _rules() -> RuleStore:
    return RuleStore(RULES_PATH)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Running the pipeline + shaping metrics
# --------------------------------------------------------------------------- #


def _run_pipeline(orders, settlements, credits) -> list[MatchResult]:
    return reconcile(orders, settlements, credits, rules=_rules().all())


def _register(name, orders, settlements, credits, truth) -> Run:
    results = _run_pipeline(orders, settlements, credits)
    run = Run(id=uuid.uuid4().hex[:12], name=name, created_at=_now(), orders=orders,
              settlements=settlements, credits=credits, truth=truth, results=results)
    RUNS[run.id] = run
    return run


def _rupees(paise: int) -> str:
    return f"{paise / 100:,.2f}"


def metrics_payload(run: Run) -> dict:
    results = run.results
    total = len(results)
    decided = sum(1 for r in results if r.decision in ("matched", "no_match"))
    matched = sum(1 for r in results if r.decision == "matched")
    no_match = sum(1 for r in results if r.decision == "no_match")
    abstained = total - decided
    sources: dict[str, int] = {}
    cost = 0.0
    llm_calls = 0
    for r in results:
        sources[r.resolved_by] = sources.get(r.resolved_by, 0) + 1
        cost += r.cost_usd
        llm_calls += r.llm_calls

    out: dict = {
        "total": total,
        "decided": decided,
        "abstained": abstained,
        "matched": matched,
        "no_match": no_match,
        "coverage": decided / total if total else 0.0,
        "resolution_sources": sources,
        "total_cost_usd": round(cost, 6),
        "llm_calls_total": llm_calls,
        "records_with_llm": sum(1 for r in results if r.llm_calls > 0),
        "has_truth": run.truth is not None,
    }
    if run.truth is not None:
        m = score(results, run.truth)
        curve = tradeoff_curve(results, run.truth)
        chosen = pick_threshold(results, run.truth)
        out.update({
            "precision": m.precision,
            "hallucinated_matches": m.hallucinated_matches,
            "traps_total": m.traps_total,
            "hallucinated_match_rate": m.hallucinated_match_rate,
            "per_tier": [t.model_dump() for t in m.per_tier],
            "latency_p50_ms": m.latency_p50_ms,
            "latency_p99_ms": m.latency_p99_ms,
            "cost_per_record_usd": m.cost_per_record_usd,
            "tradeoff_curve": [p.model_dump() for p in curve],
            "chosen_threshold": chosen.model_dump(),
            "errors": m.errors,
        })
    return out


# --------------------------------------------------------------------------- #
# The exception drill-down — the product
# --------------------------------------------------------------------------- #

_LAYER_LABEL = {
    "ambiguous_candidates": "assignment could not disambiguate the candidate batches",
    "window_subset_uncertain": "no unique subset of window lines matched",
    "ambiguous_tie": "adjudicator: two batches tied on amount and date",
    "below_threshold": "adjudicator: confidence below the accept threshold",
    "lost_contention_on_date": "adjudicator: a better-dated credit also claims this batch",
    "llm_abstain": "adjudicator abstained: insufficient evidence",
    "llm_no_match_with_options": "adjudicator declined to match despite options",
    "invalid_option_label": "adjudicator returned an invalid choice",
    "amount_mismatch_on_recompute": "adjudicator pick failed the amount re-check",
    "llm_unavailable": "adjudicator unavailable (no model) — abstained",
}


def _candidate_views(cc, index) -> list[dict]:
    views = []
    for c in cc.candidates:
        lines = index.batch_line_ids(c.batch_id)
        types: dict[str, int] = {}
        for lid in lines:
            t = index.line_type.get(lid, "line")
            types[t] = types.get(t, 0) + 1
        views.append({
            "batch_id": c.batch_id,
            "payout_ref": c.payout_ref,
            "net_total_paise": c.net_total_paise,
            "net_total_rupees": _rupees(c.net_total_paise),
            "amount_delta_paise": c.net_total_paise - cc.amount_paise,
            "date_offset_days": (cc.value_date - c.settled_date).days,
            "ref_strength": c.ref_strength,
            "matched_by": list(c.matched_by),
            "n_lines": len(lines),
            "line_types": types,
            "settlement_ids": lines,
        })
    # strongest evidence first
    views.sort(key=lambda v: (-v["ref_strength"], abs(v["amount_delta_paise"])))
    return views


def exceptions_payload(run: Run) -> list[dict]:
    exceptions = [r for r in run.results if r.decision == "abstain"]
    if not exceptions:
        return []
    index = CandidateIndex(run.settlements)
    try:
        cbid = run.credit_by_id
        items = []
        for r in exceptions:
            credit = cbid[r.bank_txn_id]
            cc = index.for_credit(credit)
            ev = r.evidence or {}
            reason = ev.get("reason", "unresolved")
            item = {
                "bank_txn_id": r.bank_txn_id,
                "amount_paise": credit.amount_paise,
                "amount_rupees": _rupees(credit.amount_paise),
                "value_date": str(credit.value_date),
                "narration": credit.narration,
                "extracted_utrs": cc.strong_utrs,
                "resolved_by": r.resolved_by,
                "confidence": r.confidence,
                "reason": reason,
                "layer": _LAYER_LABEL.get(reason.split(":")[0], reason),
                "candidates": _candidate_views(cc, index),
                "llm": {
                    "reasoning": ev.get("llm_reasoning"),
                    "rejected": ev.get("rejected", []),
                    "notes": ev.get("notes", []),
                } if ("llm_reasoning" in ev or ev.get("notes")) else None,
                "resolution": run.resolutions.get(r.bank_txn_id),
            }
            items.append(item)
        # biggest money first — that's what an analyst triages
        items.sort(key=lambda i: -i["amount_paise"])
        return items
    finally:
        index.close()


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #


class SampleRequest(BaseModel):
    name: str


class ResolveRequest(BaseModel):
    batch_id: str | None = None            # accept this candidate batch as the match
    settlement_ids: list[str] | None = None  # or supply lines explicitly
    decision: str = "matched"              # "matched" | "no_match"
    analyst: str = "analyst"


# --------------------------------------------------------------------------- #
# Endpoints — reconcile
# --------------------------------------------------------------------------- #


def _read_truth(raw: bytes) -> list[GroundTruth]:
    return [GroundTruth(**g) for g in json.loads(raw or b"[]")]


@app.post("/api/reconcile")
async def reconcile_upload(
    settlements: UploadFile = File(...),
    bank: UploadFile = File(...),
    orders: UploadFile | None = File(None),
    ground_truth: UploadFile | None = File(None),
):
    """Upload settlements.csv + bank.json (+ optional orders.xlsx, ground_truth.json)."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        (tdp / "settlements.csv").write_bytes(await settlements.read())
        (tdp / "bank.json").write_bytes(await bank.read())
        s_lines = loader.load_settlements(tdp / "settlements.csv")
        credits = loader.load_bank(tdp / "bank.json")
        o_list: list[Order] = []
        if orders is not None:
            (tdp / "orders.xlsx").write_bytes(await orders.read())
            o_list = loader.load_orders(tdp / "orders.xlsx")
        truth = _read_truth(await ground_truth.read()) if ground_truth is not None else None

    run = _register(settlements.filename or "upload", o_list, s_lines, credits, truth)
    return {"run_id": run.id, "name": run.name, **metrics_payload(run)}


@app.post("/api/reconcile/sample")
def reconcile_sample(req: SampleRequest):
    """Reconcile one of the bundled sample datasets (train/holdout/sample/learn_demo)."""
    if req.name not in SAMPLE_DIRS:
        raise HTTPException(404, f"unknown sample '{req.name}'")
    d = Path(SAMPLE_DIRS[req.name])
    if not (d / "bank.json").exists():
        raise HTTPException(404, f"sample '{req.name}' not generated at {d}")
    orders, settlements, credits = loader.load_dataset(d)
    truth = _read_truth((d / "ground_truth.json").read_bytes()) if (d / "ground_truth.json").exists() else None
    run = _register(req.name, orders, settlements, credits, truth)
    return {"run_id": run.id, "name": run.name, **metrics_payload(run)}


# --------------------------------------------------------------------------- #
# Endpoints — read
# --------------------------------------------------------------------------- #


def _get_run(run_id: str) -> Run:
    run = RUNS.get(run_id)
    if run is None:
        raise HTTPException(404, "run not found")
    return run


@app.get("/api/runs")
def list_runs():
    return [{"run_id": r.id, "name": r.name, "created_at": r.created_at,
             "total": len(r.credits),
             "abstained": sum(1 for x in r.results if x.decision == "abstain")}
            for r in RUNS.values()]


@app.get("/api/run/{run_id}/metrics")
def get_metrics(run_id: str):
    return metrics_payload(_get_run(run_id))


@app.get("/api/run/{run_id}/exceptions")
def get_exceptions(run_id: str):
    run = _get_run(run_id)
    return {"run_id": run.id, "count": sum(1 for r in run.results if r.decision == "abstain"),
            "exceptions": exceptions_payload(run)}


# --------------------------------------------------------------------------- #
# Endpoints — resolve + the learning loop
# --------------------------------------------------------------------------- #


@app.post("/api/run/{run_id}/exception/{txn_id}/resolve")
def resolve_exception(run_id: str, txn_id: str, req: ResolveRequest):
    """Record an analyst resolution and, when possible, induce a persistent learned
    rule from it so future runs resolve the same pattern automatically."""
    run = _get_run(run_id)
    credit = run.credit_by_id.get(txn_id)
    if credit is None:
        raise HTTPException(404, "credit not found in run")

    index = CandidateIndex(run.settlements)
    induced = None
    try:
        if req.decision == "no_match":
            settlement_ids: list[str] = []
            ref = None
        elif req.batch_id:
            settlement_ids = index.batch_line_ids(req.batch_id)
            if not settlement_ids:
                raise HTTPException(400, f"batch {req.batch_id} has no lines")
            ref = index._batch_ref.get(req.batch_id)  # noqa: SLF001 — internal lookup
        elif req.settlement_ids:
            settlement_ids = req.settlement_ids
            ref = None
        else:
            raise HTTPException(400, "provide batch_id, settlement_ids, or decision=no_match")

        # Try to LEARN from this resolution: if the resolved batch's reference is
        # present (just unparsed) in the narration, mint a reusable rule.
        if ref:
            rule = induce_rule(credit.narration, ref, created_by=req.analyst, source_txn_id=txn_id)
            if rule is not None:
                _rules().add(rule)
                induced = rule.model_dump()
    finally:
        index.close()

    # record the resolution + reflect it in this run's results
    run.resolutions[txn_id] = {
        "decision": req.decision, "settlement_ids": settlement_ids,
        "by": req.analyst, "at": _now(), "rule_id": induced["id"] if induced else None,
    }
    for i, r in enumerate(run.results):
        if r.bank_txn_id == txn_id:
            run.results[i] = MatchResult(
                bank_txn_id=txn_id, decision=req.decision, settlement_ids=settlement_ids,
                confidence=1.0, resolved_by="learned_rule" if induced else "analyst",
                evidence={"analyst": req.analyst, "rule_id": induced["id"] if induced else None},
            )
            break
    return {"resolved": True, "induced_rule": induced,
            "message": ("learned a reusable rule from this resolution"
                        if induced else "resolution recorded (not generalisable to a rule)")}


@app.post("/api/run/{run_id}/rerun")
def rerun(run_id: str):
    """Re-run the pipeline with the current learned rules — shows the coverage lift
    that the analyst's resolutions bought."""
    run = _get_run(run_id)
    before = sum(1 for r in run.results if r.decision in ("matched", "no_match"))
    run.results = _run_pipeline(run.orders, run.settlements, run.credits)
    after = sum(1 for r in run.results if r.decision in ("matched", "no_match"))
    # Carry run_id/name so the client's `run` state object stays complete — the UI
    # feeds this response straight into setRun(), and a missing run_id makes the next
    # exceptions fetch hit /api/run/undefined/... (404). Same shape as reconcile_*.
    payload = {"run_id": run.id, "name": run.name, **metrics_payload(run)}
    payload["coverage_delta_records"] = after - before
    return payload


@app.get("/api/rules")
def list_rules():
    return [r.model_dump() for r in _rules().all()]


@app.delete("/api/rules")
def clear_rules():
    """Reset the learned-rule store (demo convenience)."""
    if RULES_PATH.exists():
        RULES_PATH.unlink()
    return {"cleared": True}


@app.get("/api/health")
def health():
    from src import llm
    return {"ok": True, "llm_available": llm.available(), "runs": len(RUNS)}


# --------------------------------------------------------------------------- #
# Serve the built frontend (frontend/dist), if present
# --------------------------------------------------------------------------- #

_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.exists():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/")
    def _index():
        return FileResponse(_DIST / "index.html")

    @app.get("/{path:path}")
    def _spa(path: str):
        f = _DIST / path
        return FileResponse(f if f.exists() and f.is_file() else _DIST / "index.html")
