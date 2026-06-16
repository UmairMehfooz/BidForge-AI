"""
BidForge AI — Capability Library Router
========================================
Serves the Company Capability Library (50 past-project records) that powers
RAG compliance matching, and lets the user replace it by uploading a new
library (JSON in the native schema, or CSV in the hackathon sheet layout).

Endpoints
---------
GET  /api/capabilities          → records + aggregate stats
POST /api/capabilities/upload   → replace the library, rebuild the FAISS index
"""

from __future__ import annotations

import io
import json
import logging
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.services import capability_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/capabilities", tags=["Capability Library"])

_DATA_DIR = Path(__file__).parent.parent / "data"
_ACTIVE_FILE = _DATA_DIR / "capability_library_enriched.json"   # store prefers this
_BACKUP_FILE = _DATA_DIR / "capability_library_enriched.backup.json"

MAX_UPLOAD_MB = 5.0

# Hackathon xlsx sheet column layout → native JSON schema
_CSV_COLUMN_MAP = {
    "Cap ID"           : "id",
    "Domain"           : "domain",
    "Project Summary"  : "summary",
    "Certification"    : "certification",
    "Year Completed"   : "year_completed",
    "Contract Value"   : "contract_value",
    "Duration (months)": "duration_months",
    "Client Type"      : "client_type",
}

REQUIRED_FIELDS = {"id", "domain", "summary"}


def _stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    certified = sum(1 for r in records if r.get("certification"))
    return {
        "total"         : len(records),
        "domains"       : dict(Counter(str(r.get("domain") or "Unknown") for r in records)),
        "certifications": dict(Counter(str(r["certification"]) for r in records if r.get("certification"))),
        "client_types"  : dict(Counter(str(r.get("client_type") or "Unknown") for r in records)),
        "certified_pct" : round(certified / len(records) * 100) if records else 0,
        "year_range"    : (
            [min(int(r["year_completed"]) for r in records if r.get("year_completed")),
             max(int(r["year_completed"]) for r in records if r.get("year_completed"))]
            if any(r.get("year_completed") for r in records) else None
        ),
    }


def _payload() -> dict[str, Any]:
    records = capability_store.get_all_capabilities()
    return {
        "records": records,
        "stats"  : _stats(records),
        "source" : capability_store.loaded_source(),
    }


# ---------------------------------------------------------------------------
# GET /api/capabilities
# ---------------------------------------------------------------------------
@router.get(
    "",
    summary="Get the Company Capability Library",
    description=(
        "Returns every capability record currently indexed for RAG matching, "
        "with aggregate stats (domains, certifications, client types)."
    ),
)
async def get_capabilities():
    return _payload()


# ---------------------------------------------------------------------------
# POST /api/capabilities/upload
# ---------------------------------------------------------------------------

def _parse_upload(filename: str, content: bytes) -> list[dict[str, Any]]:
    """Parse a .json (native schema) or .csv (hackathon sheet layout) upload."""
    if filename.lower().endswith(".json"):
        try:
            data = json.loads(content.decode("utf-8"))
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Could not parse JSON: {exc}",
            )
        if not isinstance(data, list):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="JSON must be an array of capability records.",
            )
        return data

    # CSV — map the hackathon sheet columns onto the native schema
    try:
        df = pd.read_csv(io.BytesIO(content)).dropna(how="all")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not parse CSV: {exc}",
        )

    missing = {"Cap ID", "Domain", "Project Summary"} - set(df.columns)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"CSV is missing required columns: {sorted(missing)}. "
                f"Expected the sample-sheet layout: {sorted(_CSV_COLUMN_MAP)}."
            ),
        )

    records = []
    for _, row in df.iterrows():
        record: dict[str, Any] = {}
        for csv_col, field in _CSV_COLUMN_MAP.items():
            if csv_col in df.columns and pd.notna(row.get(csv_col)):
                record[field] = row[csv_col]
        # Normalise numeric fields that pandas reads as floats
        for int_field in ("year_completed", "duration_months"):
            if int_field in record:
                try:
                    record[int_field] = int(record[int_field])
                except (TypeError, ValueError):
                    pass
        record.setdefault("project_title", str(record.get("summary", ""))[:60])
        records.append(record)
    return records


@router.post(
    "/upload",
    summary="Replace the Capability Library with an uploaded file",
    description=(
        "Accepts JSON (native schema: id, domain, summary, certification, "
        "year_completed, contract_value, duration_months, client_type) or "
        "CSV in the hackathon sample-sheet layout. Backs up the current "
        "library, replaces it, and rebuilds the FAISS embedding index — no "
        "restart needed. The previous library is restored if the new one fails."
    ),
)
async def upload_capabilities(file: UploadFile = File(..., description="Capability library JSON or CSV")):
    filename = file.filename or ""
    if not filename.lower().endswith((".json", ".csv")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{filename}' must be a .json or .csv file.",
        )

    content = await file.read()
    size_mb = len(content) / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File is {size_mb:.1f} MB — maximum is {MAX_UPLOAD_MB:.0f} MB.",
        )
    if not content.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    records = _parse_upload(filename, content)

    # ── Validation ────────────────────────────────────────────────────────────
    if len(records) < 5:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Library has only {len(records)} records — at least 5 are needed for useful matching.",
        )
    for i, record in enumerate(records):
        if not isinstance(record, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Record {i + 1} is not an object.",
            )
        missing = REQUIRED_FIELDS - {k for k, v in record.items() if v not in (None, "")}
        if missing:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Record {i + 1} ({record.get('id', '?')}) is missing: {sorted(missing)}.",
            )
    ids = [str(r["id"]) for r in records]
    if len(set(ids)) != len(ids):
        duplicate = next(x for x in ids if ids.count(x) > 1)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Duplicate capability id: '{duplicate}'. Ids must be unique.",
        )

    # ── Back up, replace, rebuild index; restore on failure ─────────────────
    if _ACTIVE_FILE.exists():
        shutil.copy2(_ACTIVE_FILE, _BACKUP_FILE)
    _ACTIVE_FILE.write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    try:
        capability_store.reload()
        if capability_store.capability_count() != len(records):
            raise RuntimeError("Index rebuild produced an unexpected record count.")
    except Exception as exc:
        if _BACKUP_FILE.exists():
            shutil.copy2(_BACKUP_FILE, _ACTIVE_FILE)
            capability_store.reload()
        logger.error("Capability upload failed, previous library restored: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"New library rejected ({exc}) — previous library restored.",
        )

    logger.info("Capability library replaced: %d records from '%s', index rebuilt.",
                len(records), filename)
    payload = _payload()
    payload["message"] = f"Library replaced with {len(records)} records — embedding index rebuilt."
    return payload
