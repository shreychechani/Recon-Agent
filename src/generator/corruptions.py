"""Messiness functions applied to synthetic records.

Every corruption returns a NEW value and, where relevant, a tag naming what it
did. Tags flow into ``GroundTruth.corruption_tags`` so the eval harness can slice
precision/coverage by exactly which kind of mess a record carried.
"""

from __future__ import annotations

import random

# --------------------------------------------------------------------------- #
# Narration corruptions
# --------------------------------------------------------------------------- #


def add_whitespace_noise(rng: random.Random, text: str) -> str:
    """Inject extra spaces at random word boundaries."""
    parts = text.split(" ")
    return (" " * rng.randint(1, 3)).join(parts)


def lowercase_sometimes(rng: random.Random, text: str, p: float = 0.5) -> str:
    return text.lower() if rng.random() < p else text


def truncate_to(text: str, limit: int = 30) -> str:
    """Bank statements often clip narration to a fixed width."""
    return text[:limit]


def transpose_utr_chars(rng: random.Random, utr: str) -> str:
    """Swap two adjacent characters in the digit tail of a UTR.

    Models a keying/OCR error. Applied to ~2% of records to test that the matcher
    does NOT treat a near-miss reference as an exact match — a transposed UTR must
    fall through to amount+date reasoning, not silently match the wrong batch.
    """
    if len(utr) < 4:
        return utr
    # Only transpose within the digit tail so the token still looks UTR-shaped.
    i = rng.randint(len(utr) - 6, len(utr) - 2)
    chars = list(utr)
    chars[i], chars[i + 1] = chars[i + 1], chars[i]
    return "".join(chars)


# --------------------------------------------------------------------------- #
# Amount / date drift
# --------------------------------------------------------------------------- #


def rounding_drift_paise(rng: random.Random) -> int:
    """A ±1-3 paise perturbation, never zero."""
    magnitude = rng.randint(1, 3)
    return magnitude if rng.random() < 0.5 else -magnitude
