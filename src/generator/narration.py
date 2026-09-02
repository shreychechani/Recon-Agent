"""Bank-narration templates and UTR synthesis.

Bank narration is semi-structured garbage with the reference buried inside it. The
matcher's job is to recover the UTR from this string (or fail gracefully and fall
back to amount+date). To exercise that, we sample from a spread of real-world
formats: some label the UTR, some don't, some truncate it, and some omit it.
"""

from __future__ import annotations

import random

# Bank prefixes that lead a NEFT/RTGS UTR (loosely IFSC-style).
_BANK_PREFIXES = ["UTIB", "HDFC", "ICIC", "SBIN", "KKBK", "PUNB", "YESB", "IDFB"]
_BRANCHES = ["MUM", "BLR", "DEL", "PUN", "HYD", "CHN", "KOL", "AMD"]
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG"]
_EMPLOYERS = ["ACME TECH", "GLOBEX", "INITECH", "UMBRELLA", "STARK IND"]
_VENDORS = ["OFFICEMART", "CLOUDBILL", "LOGISTIQ", "PRINTWORKS", "SAASLY"]


def make_utr(rng: random.Random) -> str:
    """Synthesize a realistic bank UTR: 4-letter bank + 'N' + 12 digits."""
    prefix = rng.choice(_BANK_PREFIXES)
    digits = "".join(str(rng.randint(0, 9)) for _ in range(12))
    return f"{prefix}N{digits}"


# --------------------------------------------------------------------------- #
# Templates that carry the FULL UTR (recoverable exactly)
# --------------------------------------------------------------------------- #
# Each is a callable(rng, utr, ddmm) -> str. Grouped by how they expose the UTR
# so the generator can pick an appropriate one for a difficulty tier.

_CLEAN_TEMPLATES = [
    lambda rng, utr, ddmm: f"NEFT-{utr}-CR-{rng.choice(_BRANCHES)}",
    lambda rng, utr, ddmm: f"IMPS/{utr}/RAZORPAY SOFTWARE PVT",
    lambda rng, utr, ddmm: f"NEFT CR {rng.choice(_BANK_PREFIXES)} {utr} RAZORPAYSOFTW",
    lambda rng, utr, ddmm: f"RTGS {utr} RAZORPAYSOFTWAREPVTLTD",
    lambda rng, utr, ddmm: f"MB:SETTLE {utr}",  # UTR present but unlabelled
]

# UTR present but embedded in noise / odd casing — still fully recoverable.
_NOISY_TEMPLATES = [
    lambda rng, utr, ddmm: f"ACH C/{utr}/RZP SETTLEMENT {ddmm}",
    lambda rng, utr, ddmm: f"bulk payout {utr} ref{rng.randint(1000, 9999)}",
    lambda rng, utr, ddmm: f"NEFT/{rng.choice(_BANK_PREFIXES)}/{utr}/RAZORPAY  {ddmm}",
    lambda rng, utr, ddmm: f"INWARD REMIT {ddmm}   {utr}  cr",
]

# UTR truncated to its trailing characters (leading bank prefix lost).
_TRUNCATED_TEMPLATES = [
    lambda rng, utr, ddmm: f"UPI-SETTLEMENT-{ddmm}-{utr[-8:]}",
    lambda rng, utr, ddmm: f"NEFT CR ...{utr[-6:]} RAZORPAY",
]

# No UTR at all — matcher must reason from amount + date alone.
_ABSENT_TEMPLATES = [
    lambda rng, utr, ddmm: "NEFT-INWARD-CREDIT",
    lambda rng, utr, ddmm: f"RAZORPAY SETTLEMENT {ddmm}",
    lambda rng, utr, ddmm: "IMPS INWARD CR RAZORPAYSOFTW",
]

Exposure = str  # "clean" | "noisy" | "truncated" | "absent"

_BY_EXPOSURE = {
    "clean": _CLEAN_TEMPLATES,
    "noisy": _NOISY_TEMPLATES,
    "truncated": _TRUNCATED_TEMPLATES,
    "absent": _ABSENT_TEMPLATES,
}


def render_narration(rng: random.Random, utr: str, ddmm: str, exposure: Exposure) -> str:
    """Render a narration for a settlement credit at the given UTR exposure."""
    template = rng.choice(_BY_EXPOSURE[exposure])
    return template(rng, utr, ddmm)


# --------------------------------------------------------------------------- #
# Trap narrations — credits that must NOT match anything
# --------------------------------------------------------------------------- #


def render_salary_narration(rng: random.Random, ddmm: str) -> str:
    return f"NEFT SAL {rng.choice(_EMPLOYERS)} {rng.choice(_MONTHS)}2026"


def render_vendor_refund_narration(rng: random.Random, ddmm: str) -> str:
    ref = "".join(str(rng.randint(0, 9)) for _ in range(10))
    return f"IMPS/{ref}/{rng.choice(_VENDORS)} REFUND {ddmm}"
