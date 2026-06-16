"""
BidForge AI — Capability Store (hybrid retrieval)
==================================================
Single in-memory store for the 50-record Capability Library:

  - Dense search: fastembed (ONNX runtime, ~100 MB — Fix 11; replaces
    sentence-transformers/torch which OOM'd free-tier deploys) running the
    same all-MiniLM-L6-v2 weights, FAISS IndexFlatIP over L2-normalised
    vectors (inner product == cosine similarity).
  - Structured scoring: domain / certification / recency / client-type
    signals computed from the record's metadata fields.
  - Hybrid score = 0.5 * dense + 0.5 * structured. With only 50 records the
    structured signals carry as much information as the embeddings, and they
    make certification-bearing requirements rank certified capabilities first.

Prefers app/data/capability_library_enriched.json (richer LLM-written
summaries, see scripts/enrich_capabilities.py) over the raw file.

Public API
----------
    ensure_loaded()                                  — idempotent init (startup)
    search_capabilities(query, top_k=3)              — dense-only (legacy callers)
    search_capabilities_hybrid(query, top_k=3,
                               issuer_type=None)     — dense → hybrid re-rank
    get_all_capabilities() / get_capability_by_id()
    capability_count()
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import threading
from pathlib import Path
from typing import Any

import faiss
import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_DATA_DIR       = Path(__file__).parent.parent / "data"
_RAW_FILE       = _DATA_DIR / "capability_library.json"
_ENRICHED_FILE  = _DATA_DIR / "capability_library_enriched.json"
_MODEL_NAME     = "sentence-transformers/all-MiniLM-L6-v2"   # same weights, ONNX runtime
_EMBED_DIM      = 384                   # all-MiniLM-L6-v2 output dimension

# Hybrid blend — dense cosine vs structured metadata score
DENSE_WEIGHT      = 0.5
STRUCTURED_WEIGHT = 0.5
DENSE_CANDIDATES  = 10   # retrieve top-10 dense, re-rank by hybrid, return top-k

# Structured sub-score weights (sum to 1.0)
W_DOMAIN    = 0.45
W_CERT      = 0.30
W_RECENCY   = 0.15
W_CLIENT    = 0.10

# Certifications recognised in requirement text
_CERT_PATTERNS: dict[str, str] = {
    "ISO 27001": r"\bISO\s*27001\b",
    "ISO 9001" : r"\bISO\s*9001\b",
    "ISO 14001": r"\bISO\s*14001\b",
    "CMMI L3"  : r"\bCMMI(?:\s*L(?:evel)?\s*\d)?\b",
    "PMP"      : r"\bPMP\b",
    "CE Mark"  : r"\bCE\s*Mark(?:ing)?\b",
}

# client_type values in the library, grouped for issuer matching
_GOVT_CLIENT_TYPES    = {"federal govt", "provincial govt"}
_PRIVATE_CLIENT_TYPES = {"private sector", "international"}


# ---------------------------------------------------------------------------
# Internal state — module-level singleton, lazily initialised (thread-safe)
# ---------------------------------------------------------------------------
_records: list[dict[str, Any]] = []
_index  : faiss.IndexFlatIP | None = None
_model  = None   # fastembed.TextEmbedding — imported lazily
_domains: list[str] = []
_lock = threading.Lock()


def _load_records() -> tuple[list[dict[str, Any]], Path]:
    """Read the enriched library if available, else the raw file."""
    source = _ENRICHED_FILE if _ENRICHED_FILE.exists() else _RAW_FILE
    if not source.exists():
        raise FileNotFoundError(
            f"Capability library not found at: {_RAW_FILE}\n"
            "Create app/data/capability_library.json before starting the server."
        )
    with open(source, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list) or len(data) == 0:
        raise ValueError(f"{source.name} must be a non-empty JSON array.")
    return data, source


def _embed(texts: list[str]) -> np.ndarray:
    """
    Embed texts into L2-normalised float32 vectors of shape (N, 384).
    fastembed returns a generator of numpy arrays; normalisation is explicit
    so inner product == cosine regardless of model behaviour.
    """
    vecs = np.array(list(_model.embed(texts)), dtype="float32")
    faiss.normalize_L2(vecs)
    return vecs


def ensure_loaded() -> None:
    """
    Load records, embed summaries, and build the FAISS index — exactly once.
    Safe to call from startup AND lazily from any search function.
    """
    global _records, _index, _model, _domains
    if _index is not None:
        return
    with _lock:
        if _index is not None:
            return

        records, source = _load_records()
        enriched = source == _ENRICHED_FILE
        logger.info(
            "Loading %s capability library: %s (%d records)",
            "ENRICHED" if enriched else "RAW", source.name, len(records),
        )
        if not enriched:
            logger.warning(
                "Enriched library missing — run scripts/enrich_capabilities.py "
                "for better matching quality."
            )

        logger.info("Loading fastembed model: %s …", _MODEL_NAME)
        from fastembed import TextEmbedding
        _model = TextEmbedding(_MODEL_NAME)

        summaries = [r.get("summary", "") for r in records]
        embeddings = _embed(summaries)
        assert embeddings.shape[1] == _EMBED_DIM, (
            f"Unexpected embedding dim {embeddings.shape[1]} != {_EMBED_DIM}"
        )

        index = faiss.IndexFlatIP(embeddings.shape[1])
        index.add(embeddings)

        _records = records
        _domains = sorted({r.get("domain", "") for r in records if r.get("domain")})
        _index = index
        logger.info(
            "Capability store ready — %d capabilities indexed (dim=%d, source=%s).",
            len(records), _EMBED_DIM, source.name,
        )


def loaded_source() -> str:
    """'enriched', 'raw', or 'not loaded' — for /health and logs."""
    if _index is None:
        return "not loaded"
    return "enriched" if _ENRICHED_FILE.exists() else "raw"


def reload() -> None:
    """
    Drop the records + FAISS index and rebuild from disk — used after a new
    capability library has been uploaded. The embedding model stays loaded,
    so this takes ~1s for 50 records.
    """
    global _records, _index, _domains
    with _lock:
        _records, _index, _domains = [], None, []
    ensure_loaded()


def embed_texts(texts: list[str]) -> np.ndarray:
    """
    Single embedding interface for the whole app (capability search, taxonomy
    classifier — Fix 8/11). Returns L2-normalised float32 vectors (N, 384).
    """
    ensure_loaded()
    return _embed(texts)


# ---------------------------------------------------------------------------
# Structured scoring  (Fix 3)
# ---------------------------------------------------------------------------

def _infer_requirement_domain(text: str) -> str | None:
    """
    Best-matching library domain for a requirement text: direct substring hit
    wins; otherwise the domain sharing the most word tokens with the text.
    Returns None if nothing plausibly matches.
    """
    lowered = text.lower()
    best, best_score = None, 0.0
    for domain in _domains:
        dl = domain.lower()
        if dl in lowered:
            return domain
        tokens = [t for t in re.split(r"\W+", dl) if len(t) > 2]
        if not tokens:
            continue
        hits = sum(1 for t in tokens if t in lowered)
        score = hits / len(tokens)
        if score > best_score:
            best, best_score = domain, score
    return best if best_score >= 0.5 else None


def _extract_requirement_certs(text: str) -> list[str]:
    """Certifications explicitly mentioned in the requirement text."""
    found = []
    for cert, pattern in _CERT_PATTERNS.items():
        if re.search(pattern, text, re.IGNORECASE):
            found.append(cert)
    return found


def _cert_equal(req_cert: str, cap_cert: str) -> bool:
    """'CMMI L3' matches 'CMMI' style variations; otherwise fuzzy-exact."""
    a, b = req_cert.lower().replace(" ", ""), cap_cert.lower().replace(" ", "")
    return a in b or b in a


def score_structured(
    requirement_text: str,
    capability: dict[str, Any],
    issuer_type: str | None = None,
) -> dict[str, Any]:
    """
    Metadata-based relevance of `capability` to `requirement_text`, in [0, 1].

    Sub-scores (weights):
      domain_match (0.45)       — inferred requirement domain vs capability domain
      certification_match (0.30)— requirement-mentioned certs vs capability cert
      recency (0.15)            — linear 0.4 (2019) → 1.0 (2025)
      client_type_match (0.10)  — RFP issuer type (govt/private) vs client group

    Returns {"score": float, "sub_scores": {...}, "inferred_domain": str|None,
             "required_certs": [...]}.
    """
    # ── domain (0.45) ────────────────────────────────────────────────────────
    req_domain = _infer_requirement_domain(requirement_text)
    cap_domain = str(capability.get("domain") or "")
    if req_domain is None:
        domain_score = 0.5   # nothing to compare against → neutral
    else:
        ratio = difflib.SequenceMatcher(
            None, req_domain.lower(), cap_domain.lower()
        ).ratio()
        domain_score = 1.0 if ratio > 0.8 else ratio

    # ── certification (0.30) ─────────────────────────────────────────────────
    req_certs = _extract_requirement_certs(requirement_text)
    cap_cert = str(capability.get("certification") or "")
    if not req_certs:
        cert_score = 1.0          # requirement mentions no cert → neutral
    elif cap_cert and any(_cert_equal(rc, cap_cert) for rc in req_certs):
        cert_score = 1.0          # holds the required certification
    elif cap_cert:
        cert_score = 0.5          # holds a different certification
    else:
        cert_score = 0.3          # requirement wants a cert, capability has none

    # ── recency (0.15) ───────────────────────────────────────────────────────
    try:
        year = int(capability.get("year_completed") or 0)
    except (TypeError, ValueError):
        year = 0
    recency = 0.4 + 0.1 * (year - 2019) if year else 0.4
    recency = max(0.4, min(1.0, recency))

    # ── client type (0.10) ───────────────────────────────────────────────────
    cap_client = str(capability.get("client_type") or "").strip().lower()
    if not issuer_type:
        client_score = 0.8   # issuer unknown → mildly neutral
    else:
        cap_group = (
            "govt" if cap_client in _GOVT_CLIENT_TYPES
            else "private" if cap_client in _PRIVATE_CLIENT_TYPES
            else None
        )
        client_score = 1.0 if cap_group == issuer_type.strip().lower() else 0.6

    sub_scores = {
        "domain_match"       : round(domain_score, 4),
        "certification_match": round(cert_score, 4),
        "recency"            : round(recency, 4),
        "client_type_match"  : round(client_score, 4),
    }
    total = (
        W_DOMAIN * domain_score
        + W_CERT * cert_score
        + W_RECENCY * recency
        + W_CLIENT * client_score
    )
    return {
        "score"          : round(total, 4),
        "sub_scores"     : sub_scores,
        "inferred_domain": req_domain,
        "required_certs" : req_certs,
    }


# ---------------------------------------------------------------------------
# Search — dense and hybrid
# ---------------------------------------------------------------------------

def _dense_search(query: str, k: int) -> list[tuple[int, float]]:
    """Top-k (record_index, cosine) pairs for `query`."""
    q_vec = _embed([query])
    k = min(k, _index.ntotal)
    scores, indices = _index.search(q_vec, k)
    return [
        (int(idx), float(score))
        for idx, score in zip(indices[0], scores[0])
        if idx >= 0
    ]


def search_capabilities(query: str, top_k: int = 3) -> list[dict[str, Any]]:
    """
    Dense-only search (legacy interface — proposal generator etc.).

    Each result is the original record plus:
        _rank        : int   – 1 = closest
        _similarity  : float – cosine similarity clamped to [0, 1]
        _distance    : float – 1 - cosine (kept for backward compatibility)
    """
    ensure_loaded()
    query = query.strip()
    if not query:
        raise ValueError("search_capabilities: query must not be empty.")

    results = []
    for rank, (idx, cosine) in enumerate(_dense_search(query, top_k), start=1):
        record = dict(_records[idx])
        sim = max(0.0, min(1.0, cosine))
        record["_rank"] = rank
        record["_similarity"] = round(sim, 4)
        record["_distance"] = round(1.0 - sim, 6)
        results.append(record)
    return results


def search_capabilities_hybrid(
    query: str,
    top_k: int = 3,
    issuer_type: str | None = None,
) -> list[dict[str, Any]]:
    """
    Hybrid retrieval (Fix 3): retrieve top-10 by dense cosine, re-rank by
        hybrid = 0.5 * dense + 0.5 * structured
    and return the top-k.

    Each result is the original record plus:
        _rank, _dense, _structured, _structured_sub (dict), _hybrid,
        _similarity (== _hybrid, for code that reads the legacy key)
    """
    ensure_loaded()
    query = query.strip()
    if not query:
        raise ValueError("search_capabilities_hybrid: query must not be empty.")

    candidates = []
    for idx, cosine in _dense_search(query, DENSE_CANDIDATES):
        record = dict(_records[idx])
        dense = max(0.0, min(1.0, cosine))
        structured = score_structured(query, record, issuer_type=issuer_type)
        hybrid = DENSE_WEIGHT * dense + STRUCTURED_WEIGHT * structured["score"]
        record["_dense"] = round(dense, 4)
        record["_structured"] = structured["score"]
        record["_structured_sub"] = structured["sub_scores"]
        record["_hybrid"] = round(hybrid, 4)
        record["_similarity"] = round(hybrid, 4)
        candidates.append(record)

    candidates.sort(key=lambda r: r["_hybrid"], reverse=True)
    for rank, record in enumerate(candidates[:top_k], start=1):
        record["_rank"] = rank
    return candidates[:top_k]


# ---------------------------------------------------------------------------
# Record accessors
# ---------------------------------------------------------------------------

def get_all_capabilities() -> list[dict[str, Any]]:
    """Return all raw capability records (no scoring metadata)."""
    ensure_loaded()
    return list(_records)


def get_capability_by_id(cap_id: str) -> dict[str, Any] | None:
    """Look up a capability record by its `id` field. Returns None if not found."""
    ensure_loaded()
    for record in _records:
        if record.get("id") == cap_id:
            return dict(record)
    return None


def capability_count() -> int:
    """Return the number of indexed capabilities (0 if not loaded)."""
    return len(_records)


# ---------------------------------------------------------------------------
# Quick smoke test:  python -m app.services.capability_store
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
    ensure_loaded()
    print(f"\nTotal capabilities indexed: {capability_count()}")

    for q in [
        "Bidder must hold ISO 27001 certification for cybersecurity operations",
        "Construction of 25km dual carriageway road",
    ]:
        print(f"\nQuery: {q}")
        for h in search_capabilities_hybrid(q, top_k=5):
            print(
                f"  #{h['_rank']} {h['id']} {h.get('domain', '?'):<20} "
                f"dense={h['_dense']:.3f} structured={h['_structured']:.3f} "
                f"hybrid={h['_hybrid']:.3f} cert={h.get('certification')}"
            )
