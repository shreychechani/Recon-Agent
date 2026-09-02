"""Column-header -> canonical-field mapping. The FIRST of two LLM touchpoints.

Given unknown headers plus a few sample rows, produce a mapping to our canonical
field names. Order of preference:

1. Deterministic alias table — resolves the known formats with zero cost. This is
   also the fallback if the model is unavailable, so the pipeline never hard-fails.
2. LLM (Sonnet 4.6, structured output) — only invoked when the alias table leaves
   a REQUIRED field unresolved AND a key is present.

The result is cached, keyed by a hash of the header row, so a given file format
costs at most one call ever — not one per run.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from pydantic import BaseModel, create_model

from src import llm

# --------------------------------------------------------------------------- #
# Canonical field specs per file type
# --------------------------------------------------------------------------- #

FIELDS: dict[str, dict[str, bool]] = {  # canonical -> required?
    "orders": {
        "order_id": True,
        "amount": True,
        "created_at": True,
        "status": False,
        "customer_ref": False,
    },
    "settlements": {
        "settlement_id": True,
        "payout_batch_id": True,
        "order_id": False,
        "gross": True,
        "fee": False,
        "gst": False,
        "net": True,
        "line_type": False,
        "settled_at": True,
        "payout_ref": False,
    },
    "bank": {
        "bank_txn_id": True,
        "amount": True,
        "value_date": True,
        "narration": True,
        "utr": False,
    },
}

ALIASES: dict[str, dict[str, list[str]]] = {
    "orders": {
        "order_id": ["order id", "order ref", "orderref", "ord id", "order no"],
        "amount": ["order value inr", "order value", "amount inr", "amount", "gross", "value"],
        "created_at": ["order date", "created at", "created", "date", "txn date"],
        "status": ["order status", "status", "state"],
        "customer_ref": ["customer", "cust", "customer ref", "buyer"],
    },
    "settlements": {
        "settlement_id": ["sett id", "settlement id", "stmt ref", "stl", "settlement ref"],
        "payout_batch_id": ["batch", "payout batch", "batch id", "payout batch id"],
        "order_id": ["order ref", "order id", "orderref", "ord id"],
        "gross": ["txn amt inr", "gross", "txn amt", "txn amount", "gross amount"],
        "fee": ["comm amt", "fee", "commission", "comm", "commission amt"],
        "gst": ["tax on comm", "gst", "tax", "tax on commission"],
        "net": ["net credit", "net", "net amount", "payout", "net payout"],
        "line_type": ["ln type", "type", "line type", "txn type"],
        "settled_at": ["settled on", "settled", "settlement date", "settled at"],
        "payout_ref": ["utr no", "utr", "reference", "ref", "payout ref", "rrn"],
    },
    "bank": {
        "bank_txn_id": ["bank txn id", "txn id", "transaction id", "reference no"],
        "amount": ["amount", "credit", "amount inr", "cr amount"],
        "value_date": ["value date", "date", "txn date"],
        "narration": ["narration", "description", "particulars", "remarks"],
        "utr": ["utr", "utr no", "rrn", "reference"],
    },
}


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s.lower())).strip()


# --------------------------------------------------------------------------- #
# Deterministic alias matching (greedy, highest score first)
# --------------------------------------------------------------------------- #


def _alias_match(file_type: str, headers: list[str]) -> dict[str, str | None]:
    norm_headers = {h: _norm(h) for h in headers}
    scored: list[tuple[int, str, str]] = []  # (score, canonical, header)
    for canonical, aliases in ALIASES[file_type].items():
        na = [_norm(a) for a in aliases]
        for h, nh in norm_headers.items():
            if nh in na:
                score = 3
            elif any(nh == a or nh in a.split() or a in nh.split() for a in na):
                score = 2
            elif any(a in nh or nh in a for a in na):
                score = 1
            else:
                score = 0
            if score:
                scored.append((score, canonical, h))

    scored.sort(key=lambda t: -t[0])
    mapping: dict[str, str | None] = {c: None for c in FIELDS[file_type]}
    used_headers: set[str] = set()
    for score, canonical, header in scored:
        if mapping[canonical] is None and header not in used_headers:
            mapping[canonical] = header
            used_headers.add(header)
    return mapping


def _unresolved_required(file_type: str, mapping: dict[str, str | None]) -> list[str]:
    return [f for f, req in FIELDS[file_type].items() if req and mapping.get(f) is None]


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #


def _cache_path() -> Path:
    return llm.cache_dir() / "schema_map.json"


def _cache_key(file_type: str, headers: list[str]) -> str:
    raw = file_type + "||" + "|".join(headers)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _load_cache() -> dict:
    p = _cache_path()
    if p.exists():
        return json.loads(p.read_text())
    return {}


def _save_cache(cache: dict) -> None:
    _cache_path().write_text(json.dumps(cache, indent=2))


# --------------------------------------------------------------------------- #
# LLM mapping (only when alias table leaves a required field unresolved)
# --------------------------------------------------------------------------- #


def _llm_mapping(file_type: str, headers: list[str], sample_rows: list[dict]) -> dict[str, str | None]:
    fields = list(FIELDS[file_type])
    # Build a dynamic schema: one optional str field per canonical name.
    schema = create_model(  # type: ignore[call-overload]
        f"{file_type.capitalize()}Mapping",
        **{f: (str | None, None) for f in fields},
    )
    system = (
        "You map messy spreadsheet/CSV/JSON column headers to a fixed set of "
        "canonical field names for a payments reconciliation system. For each "
        "canonical field, return the EXACT source header string that best matches, "
        "or null if none applies. Never invent a header that is not in the list."
    )
    user = (
        f"File type: {file_type}\n"
        f"Canonical fields: {fields}\n"
        f"Source headers: {headers}\n"
        f"Sample rows (first few):\n{json.dumps(sample_rows[:3], indent=2, default=str)}"
    )
    result = llm.structured_call(system, user, schema, max_tokens=512)
    if not result.ok or result.parsed is None:
        return {f: None for f in fields}
    data = result.parsed.model_dump()
    # Keep only mappings that name a real header.
    return {f: (data.get(f) if data.get(f) in headers else None) for f in fields}


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #


def map_columns(file_type: str, headers: list[str], sample_rows: list[dict]) -> dict[str, str | None]:
    """Return canonical_field -> source_header (or None). Cached per header row."""
    if file_type not in FIELDS:
        raise ValueError(f"unknown file_type: {file_type}")

    cache = _load_cache()
    key = _cache_key(file_type, headers)
    if key in cache:
        return cache[key]

    mapping = _alias_match(file_type, headers)

    if _unresolved_required(file_type, mapping) and llm.available():
        llm_map = _llm_mapping(file_type, headers, sample_rows)
        # LLM fills only the gaps the alias table missed.
        for f in FIELDS[file_type]:
            if mapping.get(f) is None and llm_map.get(f):
                mapping[f] = llm_map[f]

    cache[key] = mapping
    _save_cache(cache)
    return mapping
