"""
BidForge AI — Evaluation Criteria Taxonomy (Fix 8)
===================================================
Classifies extracted evaluation-criteria requirements against a 15-entry
taxonomy (app/data/criteria_taxonomy.json) covering common RFP evaluation
criteria across IT services, construction, and logistics.

Classification strategy:
  1. Keyword match (case-insensitive substring) — fast, deterministic.
  2. Fallback: embedding similarity between the requirement text and each
     taxonomy entry's name+keywords, using the same MiniLM model as the
     capability store (single shared embed_texts interface).
  3. Below the similarity floor → unclassified (taxonomy fields stay null).

Public API
----------
    classify_criterion(text) -> {"taxonomy_id", "taxonomy_name"} | None
    classify_requirements(requirements) -> int   # mutates in place, returns mapped count
    get_taxonomy() -> list[dict]
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

_TAXONOMY_FILE = Path(__file__).parent.parent / "data" / "criteria_taxonomy.json"

# Minimum cosine similarity for the embedding fallback to accept a match.
# MiniLM cosines for short paraphrases of these criteria land around
# 0.28-0.35; genuine junk stays well below 0.2.
_SIMILARITY_FLOOR = 0.25

_taxonomy: list[dict[str, Any]] | None = None
_entry_vectors: np.ndarray | None = None


def get_taxonomy() -> list[dict[str, Any]]:
    """Load and cache the 15-entry taxonomy."""
    global _taxonomy
    if _taxonomy is None:
        if not _TAXONOMY_FILE.exists():
            logger.warning("criteria_taxonomy.json not found — classification disabled.")
            _taxonomy = []
        else:
            _taxonomy = json.loads(_TAXONOMY_FILE.read_text(encoding="utf-8"))
            logger.info("Criteria taxonomy loaded: %d entries.", len(_taxonomy))
    return _taxonomy


def _entry_embedding_text(entry: dict[str, Any]) -> str:
    """Name + keywords — richer signal than the bare entry name."""
    return f"{entry['name']}: {', '.join(entry.get('keywords', []))}"


def _get_entry_vectors() -> np.ndarray | None:
    """Lazily embed all taxonomy entries with the shared MiniLM model."""
    global _entry_vectors
    if _entry_vectors is not None:
        return _entry_vectors
    taxonomy = get_taxonomy()
    if not taxonomy:
        return None
    try:
        from app.services.capability_store import embed_texts
        _entry_vectors = embed_texts([_entry_embedding_text(e) for e in taxonomy])
    except Exception as exc:
        logger.warning("Taxonomy embeddings unavailable (%s) — keyword-only mode.", exc)
        return None
    return _entry_vectors


def classify_criterion(text: str) -> dict[str, str] | None:
    """
    Classify one evaluation-criterion text against the taxonomy.
    Returns {"taxonomy_id": ..., "taxonomy_name": ...} or None.
    """
    if not text:
        return None
    taxonomy = get_taxonomy()
    if not taxonomy:
        return None

    lowered = text.lower()

    # ── 1. Keyword match — most keyword hits wins ────────────────────────────
    best_entry, best_hits = None, 0
    for entry in taxonomy:
        hits = sum(1 for kw in entry.get("keywords", []) if kw in lowered)
        if hits > best_hits:
            best_entry, best_hits = entry, hits
    if best_entry is not None:
        return {"taxonomy_id": best_entry["id"], "taxonomy_name": best_entry["name"]}

    # ── 2. Embedding fallback (shared MiniLM model) ──────────────────────────
    vectors = _get_entry_vectors()
    if vectors is None:
        return None
    try:
        from app.services.capability_store import embed_texts
        query = embed_texts([text])[0]
    except Exception as exc:
        logger.warning("Criterion embedding failed: %s", exc)
        return None

    sims = vectors @ query   # all vectors are L2-normalised → cosine
    idx = int(np.argmax(sims))
    if float(sims[idx]) < _SIMILARITY_FLOOR:
        return None
    entry = taxonomy[idx]
    return {"taxonomy_id": entry["id"], "taxonomy_name": entry["name"]}


def classify_requirements(requirements: list[dict[str, Any]]) -> int:
    """
    Classify every type="evaluation_criteria" requirement in place
    (sets taxonomy_id / taxonomy_name). Returns the number mapped.
    """
    mapped = 0
    for req in requirements:
        if req.get("type") != "evaluation_criteria":
            continue
        result = classify_criterion(req.get("requirement", ""))
        req["taxonomy_id"] = result["taxonomy_id"] if result else None
        req["taxonomy_name"] = result["taxonomy_name"] if result else None
        if result:
            mapped += 1
    return mapped
