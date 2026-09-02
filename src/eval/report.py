"""Render the metrics table and the coverage/precision tradeoff curve.

    python -m src.eval.report --data data/generated/train/            # runs the pipeline
    python -m src.eval.report --data data/generated/train/ --matcher stub

The stub path exists to prove the harness works (0% coverage, 100% precision)
before any real matcher exists.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.eval.harness import (
    CurvePoint,
    Metrics,
    abstain_all,
    load_bank_txn_ids,
    load_ground_truth,
    pick_threshold,
    score,
    tradeoff_curve,
)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def format_metrics_table(m: Metrics) -> str:
    rows = [
        ("Records processed", str(m.total)),
        ("Auto-resolve rate (coverage)", _pct(m.auto_resolve_rate)),
        ("Precision on decided records", _pct(m.precision)),
        ("Hallucinated-match rate on traps", f"{_pct(m.hallucinated_match_rate)} ({m.hallucinated_matches}/{m.traps_total})"),
        ("Resolved without any LLM call", f"{_pct(m.records_deterministic / m.total)} ({m.records_deterministic}/{m.total})"),
        ("Records using the LLM", str(m.records_with_llm)),
        ("Total LLM calls", str(m.llm_calls_total)),
        ("Cost per record (USD)", f"${m.cost_per_record_usd:.6f}"),
        ("Total cost (USD)", f"${m.total_cost_usd:.4f}"),
        ("Latency per record p50 / p99 (ms)", f"{m.latency_p50_ms:.2f} / {m.latency_p99_ms:.2f}"),
        ("  deterministic p50 / p99 (ms)", f"{m.det_latency_p50_ms:.2f} / {m.det_latency_p99_ms:.2f}"),
        ("  LLM p50 / p99 (ms)", f"{m.llm_latency_p50_ms:.2f} / {m.llm_latency_p99_ms:.2f}"),
    ]
    width = max(len(k) for k, _ in rows)
    lines = ["", "METRICS", "=" * (width + 26)]
    for k, v in rows:
        lines.append(f"{k:<{width}}   {v}")
    return "\n".join(lines)


def format_tier_table(m: Metrics) -> str:
    lines = ["", "PER-TIER BREAKDOWN", "-" * 58]
    lines.append(f"{'tier':<8}{'total':>8}{'decided':>9}{'coverage':>11}{'precision':>12}")
    for t in m.per_tier:
        lines.append(
            f"{t.tier:<8}{t.total:>8}{t.decided:>9}{_pct(t.coverage):>11}{_pct(t.precision):>12}"
        )
    return "\n".join(lines)


def format_sources(m: Metrics) -> str:
    lines = ["", "RESOLUTION SOURCE DISTRIBUTION", "-" * 40]
    order = ["exact_ref", "fee_adjusted", "subset_sum", "assignment", "llm", "learned_rule", "none"]
    for src in order:
        n = m.resolution_sources.get(src, 0)
        if n:
            lines.append(f"  {src:<16}{n:>6}  ({_pct(n / m.total)})")
    return "\n".join(lines)


def format_curve(points: list[CurvePoint], height: int = 9) -> str:
    """A compact ASCII plot of precision (y) vs coverage (x) across thresholds."""
    lines = ["", "COVERAGE / PRECISION TRADEOFF (threshold sweep)", "-" * 58]
    lines.append(f"{'thr':>5}{'coverage':>11}{'precision':>12}{'decided':>10}")
    for p in points:
        bar = "#" * int(round(p.coverage * 30))
        lines.append(f"{p.threshold:>5.2f}{_pct(p.coverage):>11}{_pct(p.precision):>12}{p.decided:>10}  {bar}")
    return "\n".join(lines)


def run_matcher(data_dir: Path, matcher: str, *, adjudicate: bool | None = None):
    """Return a list[MatchResult] for the dataset using the chosen matcher."""
    if matcher == "stub":
        return abstain_all(load_bank_txn_ids(data_dir))
    if matcher == "pipeline":
        # Imported lazily so the harness has no hard dependency on later phases.
        from src.pipeline import reconcile_dataset

        return reconcile_dataset(data_dir, adjudicate=adjudicate)
    raise SystemExit(f"unknown matcher: {matcher}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Score a matcher against ground truth")
    ap.add_argument("--data", type=Path, required=True)
    ap.add_argument("--matcher", default="pipeline", choices=["pipeline", "stub"])
    ap.add_argument("--out", type=Path, help="optional: write metrics + curve as JSON")
    ap.add_argument("--no-curve", action="store_true")
    adj = ap.add_mutually_exclusive_group()
    adj.add_argument("--adjudicate", dest="adjudicate", action="store_true",
                     help="force the Phase-6 LLM adjudicator on (needs ANTHROPIC_API_KEY)")
    adj.add_argument("--no-adjudicate", dest="adjudicate", action="store_false",
                     help="force the LLM adjudicator off (deterministic + assignment only)")
    ap.set_defaults(adjudicate=None)  # None = auto (on iff a model is available)
    args = ap.parse_args()

    truth = load_ground_truth(args.data)
    results = run_matcher(args.data, args.matcher, adjudicate=args.adjudicate)
    m = score(results, truth)

    print(format_metrics_table(m))
    print(format_tier_table(m))
    print(format_sources(m))

    curve = []
    if not args.no_curve:
        curve = tradeoff_curve(results, truth)
        print(format_curve(curve))
        chosen = pick_threshold(results, truth)
        print(f"\nHighest-coverage threshold holding 100% precision: "
              f"τ={chosen.threshold:.2f} → coverage {_pct(chosen.coverage)} "
              f"({chosen.decided}/{m.total}), precision {_pct(chosen.precision)}")

    if args.out:
        args.out.write_text(
            json.dumps(
                {"metrics": m.model_dump(), "curve": [p.model_dump() for p in curve]},
                indent=2,
            )
        )
        print(f"\nWrote metrics JSON to {args.out}")


if __name__ == "__main__":
    main()
