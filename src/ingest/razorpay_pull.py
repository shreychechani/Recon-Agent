"""CLI: pull Razorpay Settlement-Recon data and write a standard dataset directory.

    # live (needs RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET; use rzp_test_* for test mode)
    python -m src.ingest.razorpay_pull --year 2026 --month 8 --out data/generated/razorpay/

    # offline, from a saved recon payload (no key, no network) — used for the demo/tests
    python -m src.ingest.razorpay_pull --from-json data/razorpay_recon_sample.json \
        --out data/generated/razorpay/

The output dir (orders.xlsx, settlements.csv, bank.json, ground_truth.json) is in the
exact format the file loader reads, so every existing tool — `src.eval.report`, the
console, the tests — consumes live Razorpay data with zero downstream changes.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.generator.generate import (
    write_bank_json,
    write_ground_truth,
    write_orders_xlsx,
    write_settlements_csv,
)
from src.ingest.razorpay_source import fetch_recon, recon_items_to_models


def _load_items(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    if isinstance(payload, dict):
        return payload.get("items", [])
    return payload


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Pull Razorpay settlement-recon data into a dataset dir")
    p.add_argument("--year", type=int, help="settlement year, YYYY (live fetch)")
    p.add_argument("--month", type=int, help="settlement month, 1-12 (live fetch)")
    p.add_argument("--day", type=int, default=None, help="optional settlement day, 1-31")
    p.add_argument("--from-json", type=str, default=None,
                   help="load a saved recon payload instead of calling the API")
    p.add_argument("--out", type=str, required=True, help="output dataset directory")
    args = p.parse_args(argv)

    if args.from_json:
        items = _load_items(Path(args.from_json))
        source = f"file {args.from_json}"
    else:
        if args.year is None or args.month is None:
            p.error("live fetch needs --year and --month (or use --from-json)")
        items = fetch_recon(args.year, args.month, args.day)
        source = f"Razorpay API {args.year}-{args.month:02d}"

    orders, settlements, credits, truth = recon_items_to_models(items)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    write_orders_xlsx(out / "orders.xlsx", orders)
    write_settlements_csv(out / "settlements.csv", settlements)
    write_bank_json(out / "bank.json", credits)
    write_ground_truth(out / "ground_truth.json", truth)

    print(f"Pulled {len(items)} recon items from {source}")
    print(f"  -> {len(orders)} orders, {len(settlements)} settlement lines, "
          f"{len(credits)} bank credits (batches)")
    print(f"  wrote dataset to {out}/  (orders.xlsx, settlements.csv, bank.json, ground_truth.json)")


if __name__ == "__main__":
    main()
