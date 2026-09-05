"""Razorpay ingestion — pull real settlement data from Razorpay's **Settlement Recon
Report API** and map it into the same canonical models the file loader produces, so
live Razorpay data flows through the identical reconciliation pipeline.

Why this is the right integration point. The recon report
(``GET /v1/settlements/recon/combined``, wrapped by the official SDK as
``client.settlement.report``) returns, per settled transaction, exactly the fields this
project reconciles: a ``settlement_id`` (the payout batch), a ``settlement_utr`` (the
reference echoed in the bank narration), the money (``amount``/``fee``/``tax``/
``credit``/``debit``), the line ``type`` (payment/refund/adjustment) and the linked
``order_id``. One API call yields two of our three inputs — settlements and orders —
straight from the source.

Design. Only :func:`fetch_recon` touches the network; :func:`recon_items_to_models` is a
pure, offline-testable mapping. That is the same injectable-boundary discipline the rest
of the codebase uses (the LLM adjudicator's ``call_fn``): the API is one seam, and
everything downstream stays deterministic and unit-tested. Feed it a canned recon
payload and you exercise the whole path with no key and no network.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from src.models import BankCredit, GroundTruth, Order, SettlementLine

# Razorpay recon `type` -> our SettlementLine.line_type. A batch is money paid out; a
# refund/adjustment shows up as a negative line that reduces the batch's net.
_LINE_TYPE = {
    "payment": "sale",
    "refund": "refund",
    "adjustment": "adjustment",
    "transfer": "adjustment",
}

# Razorpay timestamps are Unix seconds (UTC). Indian settlements read most naturally in
# IST, and our models carry naive local datetimes, so shift +5:30 and drop tzinfo.
_IST = timedelta(hours=5, minutes=30)


def _ist(ts: object) -> datetime | None:
    if ts is None or ts == "":
        return None
    return datetime.utcfromtimestamp(int(ts)) + _IST


def _paise(v: object) -> int:
    """Recon amounts are already in currency subunits (paise). Coerce to int."""
    return int(v or 0)


# --------------------------------------------------------------------------- #
# Pure mapping: recon items -> canonical models  (no network, fully testable)
# --------------------------------------------------------------------------- #


def recon_items_to_models(
    items: list[dict],
    *,
    bank_txn_prefix: str = "BANK",
    value_date_lag_days: int = 0,
) -> tuple[list[Order], list[SettlementLine], list[BankCredit], list[GroundTruth]]:
    """Map raw Settlement-Recon items into ``(orders, settlements, credits, truth)``.

    Each item becomes one :class:`SettlementLine`. Items sharing a ``settlement_id`` form
    one payout batch, which we roll up into one :class:`BankCredit` (net total, value
    date, a narration carrying the UTR) — i.e. the money that actually lands in the
    merchant's account. Because we know which lines compose each batch, we can also emit
    ground truth, so a run on live data is scorable end-to-end.
    """
    settlements: list[SettlementLine] = []
    orders_by_id: dict[str, Order] = {}
    # settlement_id -> accumulator for its bank credit
    batches: dict[str, dict] = {}

    for it in items:
        # The recon "combined" report is settled transactions; guard defensively.
        if it.get("settled") is False:
            continue

        entity_id = str(it.get("entity_id"))
        batch_id = str(it.get("settlement_id"))
        utr = it.get("settlement_utr")
        settled_at = _ist(it.get("settled_at")) or _ist(it.get("created_at"))
        net = _paise(it.get("credit")) - _paise(it.get("debit"))
        rtype = str(it.get("type") or "payment")

        settlements.append(
            SettlementLine(
                settlement_id=entity_id,
                payout_batch_id=batch_id,
                order_id=str(it["order_id"]) if it.get("order_id") else None,
                gross_paise=_paise(it.get("amount")),
                fee_paise=_paise(it.get("fee")),
                gst_paise=_paise(it.get("tax")),
                net_paise=net,
                line_type=_LINE_TYPE.get(rtype, "adjustment"),  # type: ignore[arg-type]
                settled_at=settled_at or datetime(1970, 1, 1),
                payout_ref=str(utr) if utr else None,
            )
        )

        # Order ledger (dedup by order_id; prefer the originating payment row).
        oid = it.get("order_id")
        if oid:
            oid = str(oid)
            if oid not in orders_by_id or rtype == "payment":
                orders_by_id[oid] = Order(
                    order_id=oid,
                    amount_paise=_paise(it.get("amount")),
                    created_at=_ist(it.get("created_at")) or settled_at or datetime(1970, 1, 1),
                    status="refunded" if rtype == "refund" else "captured",
                    customer_ref=str(it.get("order_receipt") or ""),
                )

        # Roll the line into its batch's bank credit.
        b = batches.setdefault(batch_id, {"net": 0, "utr": utr, "when": settled_at, "lines": []})
        b["net"] += net
        b["utr"] = b["utr"] or utr
        if settled_at and (b["when"] is None or settled_at > b["when"]):
            b["when"] = settled_at
        b["lines"].append(entity_id)

    credits: list[BankCredit] = []
    truth: list[GroundTruth] = []
    for i, (batch_id, b) in enumerate(sorted(batches.items()), start=1):
        if b["net"] <= 0:  # a batch that nets to zero/negative isn't a bank credit
            continue
        utr = b["utr"]
        when: datetime = b["when"] or datetime(1970, 1, 1)
        value_date: date = (when + timedelta(days=value_date_lag_days)).date()
        txn_id = f"{bank_txn_prefix}-{i:04d}"
        narration = f"RAZORPAY SETTLEMENT {utr} CR" if utr else f"RAZORPAY SETTLEMENT {batch_id} CR"
        credits.append(
            BankCredit(
                bank_txn_id=txn_id,
                amount_paise=b["net"],
                value_date=value_date,
                narration=narration,
                utr=str(utr) if utr else None,
            )
        )
        truth.append(
            GroundTruth(
                bank_txn_id=txn_id,
                settlement_ids=list(b["lines"]),
                difficulty="easy",
                corruption_tags=["razorpay_live"],
            )
        )

    return list(orders_by_id.values()), settlements, credits, truth


# --------------------------------------------------------------------------- #
# The one networked seam: fetch from the live Razorpay API
# --------------------------------------------------------------------------- #


def available() -> bool:
    """True iff Razorpay API credentials are present in the environment."""
    return bool(os.environ.get("RAZORPAY_KEY_ID") and os.environ.get("RAZORPAY_KEY_SECRET"))


def _client_from_env():
    try:
        import razorpay  # lazy: only needed for live fetch, not for mapping/tests
    except ImportError as e:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "the 'razorpay' package is required for live fetch — `uv pip install razorpay`"
        ) from e
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not (key_id and key_secret):
        raise RuntimeError(
            "set RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET (use rzp_test_* keys for test mode)"
        )
    return razorpay.Client(auth=(key_id, key_secret))


def fetch_recon(
    year: int,
    month: int,
    day: int | None = None,
    *,
    count: int = 1000,
    client=None,
) -> list[dict]:
    """Fetch settled transactions from the Settlement Recon Report API, paginating.

    Wraps ``client.settlement.report`` (``GET /v1/settlements/recon/combined``). Pass a
    pre-built ``client`` in tests; otherwise it is constructed from env credentials.
    """
    client = client or _client_from_env()
    items: list[dict] = []
    skip = 0
    while True:
        params = {"year": year, "month": month, "count": count, "skip": skip}
        if day is not None:
            params["day"] = day
        resp = client.settlement.report(params)
        page = resp.get("items", []) if isinstance(resp, dict) else list(resp)
        items.extend(page)
        if len(page) < count:
            break
        skip += count
    return items
