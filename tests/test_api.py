"""Phase 8 API tests — the data contract behind the console, exercised end to end
with FastAPI's TestClient. The learning-loop test (resolve -> induce rule -> re-run
-> coverage lift) is the one that proves the product actually closes the loop.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    # isolate the persistent rule store so tests never touch the real one
    monkeypatch.setattr(main, "RULES_PATH", tmp_path / "rules.json")
    main.RUNS.clear()
    return TestClient(main.app)


def _reconcile_sample(client, name):
    r = client.post("/api/reconcile/sample", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()


def test_reconcile_sample_reports_precision(client):
    body = _reconcile_sample(client, "sample")
    assert body["has_truth"] is True
    assert body["precision"] == 1.0
    assert body["hallucinated_matches"] == 0
    assert 0.0 < body["coverage"] <= 1.0
    assert "tradeoff_curve" in body and body["tradeoff_curve"]
    assert body["chosen_threshold"]["precision"] == 1.0


def test_unknown_sample_404(client):
    assert client.post("/api/reconcile/sample", json={"name": "nope"}).status_code == 404


def test_exceptions_carry_full_drilldown(client):
    run_id = _reconcile_sample(client, "sample")["run_id"]
    r = client.get(f"/api/run/{run_id}/exceptions")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == len(data["exceptions"])
    assert data["exceptions"], "seed sample should have exceptions to triage"
    ex = data["exceptions"][0]
    for key in ("bank_txn_id", "amount_rupees", "narration", "reason", "layer", "candidates"):
        assert key in ex
    # candidates carry the evidence an analyst needs + the ids to resolve with
    for c in ex["candidates"]:
        for key in ("batch_id", "amount_delta_paise", "date_offset_days",
                    "ref_strength", "n_lines", "settlement_ids"):
            assert key in c


def test_learning_loop_lifts_coverage_after_one_resolution(client):
    body = _reconcile_sample(client, "learn_demo")
    run_id = body["run_id"]
    before_cov = body["coverage"]
    assert before_cov < 1.0  # novel-reference cohort abstains at first

    exc = client.get(f"/api/run/{run_id}/exceptions").json()["exceptions"]
    assert exc

    # white-box: find the correct batch for one exception via the run's ground truth
    run = main.RUNS[run_id]
    truth_by = {g.bank_txn_id: g for g in run.truth}
    line_batch = {l.settlement_id: l.payout_batch_id for l in run.settlements}
    target = next(e for e in exc if truth_by[e["bank_txn_id"]].settlement_ids)
    correct_batch = line_batch[truth_by[target["bank_txn_id"]].settlement_ids[0]]

    res = client.post(
        f"/api/run/{run_id}/exception/{target['bank_txn_id']}/resolve",
        json={"batch_id": correct_batch, "analyst": "ops@demo"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["induced_rule"] is not None  # we learned a reusable rule

    rerun = client.post(f"/api/run/{run_id}/rerun").json()
    # BUG-003 regression: the re-run response IS the client's `run` state object, so it
    # must carry the run's identity — a missing run_id made the UI fetch /run/undefined/.
    assert rerun["run_id"] == run_id
    assert rerun["precision"] == 1.0
    assert rerun["hallucinated_matches"] == 0
    assert rerun["coverage"] > before_cov
    assert rerun["coverage_delta_records"] > 0
    # the rule now shows up in the store and drives learned_rule resolutions
    assert client.get("/api/rules").json()
    assert rerun["resolution_sources"].get("learned_rule", 0) > 0


def test_resolve_near_dup_records_but_does_not_generalise(client):
    """A near-duplicate resolution has no reference in the narration to learn from —
    it is recorded as a one-off, honestly not turned into a rule."""
    run_id = _reconcile_sample(client, "sample")["run_id"]
    exc = client.get(f"/api/run/{run_id}/exceptions").json()["exceptions"]
    run = main.RUNS[run_id]
    truth_by = {g.bank_txn_id: g for g in run.truth}
    line_batch = {l.settlement_id: l.payout_batch_id for l in run.settlements}
    # pick an exception whose narration carries no parseable reference (utr_absent)
    target = next(e for e in exc if not e["extracted_utrs"] and truth_by[e["bank_txn_id"]].settlement_ids)
    correct_batch = line_batch[truth_by[target["bank_txn_id"]].settlement_ids[0]]
    res = client.post(
        f"/api/run/{run_id}/exception/{target['bank_txn_id']}/resolve",
        json={"batch_id": correct_batch},
    ).json()
    assert res["resolved"] is True
    assert res["induced_rule"] is None


def test_health(client):
    h = client.get("/api/health").json()
    assert h["ok"] is True
    assert "llm_available" in h
