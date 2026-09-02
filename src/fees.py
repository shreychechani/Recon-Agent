"""The Indian-payments fee model, in integer paise.

Shared by the generator (which applies it to synthesise settlement lines) and the
deterministic matcher (which recomputes expected net to verify a match). Keeping a
single implementation guarantees the two agree to the paise, so the ONLY source of
net-vs-gross disagreement in the dataset is the deliberately injected rounding
drift — never an accidental mismatch between generation and matching.

    fee_paise = round(gross_paise * 0.02)     # 2% commission
    gst_paise = round(fee_paise  * 0.18)      # 18% GST on the commission
    net_paise = gross_paise - fee_paise - gst_paise

Rounding is ROUND_HALF_UP via Decimal — no floats.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

FEE_RATE = Decimal("0.02")  # 2% commission
GST_RATE = Decimal("0.18")  # 18% GST on the commission

# Tolerance the matcher allows when comparing recomputed net to observed amount.
# Absorbs half-paise rounding and the injected ±1-3 paise drift on hard records.
ROUNDING_TOLERANCE_PAISE = 5


def _round_half_up(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def compute_fee_paise(gross_paise: int) -> int:
    return _round_half_up(Decimal(gross_paise) * FEE_RATE)


def compute_gst_paise(fee_paise: int) -> int:
    return _round_half_up(Decimal(fee_paise) * GST_RATE)


def compute_net_from_gross(gross_paise: int) -> tuple[int, int, int]:
    """Return (fee_paise, gst_paise, net_paise) for a sale line's gross."""
    fee = compute_fee_paise(gross_paise)
    gst = compute_gst_paise(fee)
    net = gross_paise - fee - gst
    return fee, gst, net
