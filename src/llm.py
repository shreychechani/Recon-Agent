"""Shared Anthropic client wrapper — the ONLY place the SDK is touched.

Both LLM touchpoints (schema mapping in Phase 2, adjudication in Phase 6) go
through ``structured_call``. It returns validated Pydantic output plus the token
usage and dollar cost of the call, so the eval harness can report cost per record.

The whole pipeline must run with no API key: ``available()`` returns False when
the key is absent or ``RECON_DISABLE_LLM=1``, and callers fall back to
deterministic behaviour. We never crash on model unavailability.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

# claude-sonnet-4-6 pricing, USD per token (see claude-api reference: $3 / $15 per MTok).
_PRICE_IN = 3.0 / 1_000_000
_PRICE_OUT = 15.0 / 1_000_000
_PRICE_CACHE_WRITE = _PRICE_IN * 1.25
_PRICE_CACHE_READ = _PRICE_IN * 0.10

DEFAULT_MODEL = os.environ.get("RECON_LLM_MODEL", "claude-sonnet-4-6")

T = TypeVar("T", bound=BaseModel)


@dataclass
class LLMResult:
    parsed: BaseModel | None
    cost_usd: float
    input_tokens: int
    output_tokens: int
    ok: bool
    error: str | None = None


def available() -> bool:
    if os.environ.get("RECON_DISABLE_LLM", "0") == "1":
        return False
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _cost(usage) -> float:
    ci = getattr(usage, "cache_creation_input_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    return (
        usage.input_tokens * _PRICE_IN
        + ci * _PRICE_CACHE_WRITE
        + cr * _PRICE_CACHE_READ
        + usage.output_tokens * _PRICE_OUT
    )


def structured_call(
    system: str,
    user: str,
    schema: type[T],
    *,
    max_tokens: int = 1024,
    thinking: bool = False,
    max_retries: int = 3,
) -> LLMResult:
    """One structured Anthropic call, validated against ``schema``.

    Never raises on API failure — returns ``ok=False`` so callers can abstain.
    """
    if not available():
        return LLMResult(None, 0.0, 0, 0, ok=False, error="llm_unavailable")

    try:
        import anthropic
    except ImportError:
        return LLMResult(None, 0.0, 0, 0, ok=False, error="anthropic_not_installed")

    client = anthropic.Anthropic(max_retries=max_retries)
    kwargs: dict = {
        "model": DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_format": schema,
    }
    if thinking:
        # Adaptive thinking helps the adjudication judgment call (Sonnet 4.6).
        kwargs["thinking"] = {"type": "adaptive"}

    try:
        resp = client.messages.parse(**kwargs)
    except Exception as e:  # noqa: BLE001 — deliberately broad: never crash the pipeline
        return LLMResult(None, 0.0, 0, 0, ok=False, error=f"{type(e).__name__}: {e}")

    usage = resp.usage
    return LLMResult(
        parsed=resp.parsed_output,
        cost_usd=_cost(usage),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        ok=resp.parsed_output is not None,
        error=None if resp.parsed_output is not None else "no_parsed_output",
    )


def cache_dir() -> Path:
    d = Path(os.environ.get("RECON_CACHE_DIR", ".cache"))
    d.mkdir(parents=True, exist_ok=True)
    return d
