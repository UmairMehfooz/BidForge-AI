"""
BidForge AI — Bid History Router
=================================
Serves the historical bid outcomes dataset (app/data/bid_history.csv) that
powers the sector win rates and the LogisticRegression win model, and lets
the user replace it by uploading a new CSV.

Endpoints
---------
GET  /api/bid-history          → rows + aggregate stats + model insights
POST /api/bid-history/upload   → replace the CSV, reload stats, retrain model
"""

from __future__ import annotations

import io
import logging
import shutil
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.services.bid_history import (
    get_overall_stats,
    load_bid_history,
    reload_bid_history,
)
from app.services.win_model import get_model_insights, is_trained, train_win_model

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bid-history", tags=["Bid History"])

_CSV_PATH = Path(__file__).parent.parent / "data" / "bid_history.csv"
_BACKUP_PATH = _CSV_PATH.with_suffix(".backup.csv")

MAX_UPLOAD_MB = 5.0

# Columns the scoring engine / win model actually depend on
REQUIRED_COLUMNS = {
    "Sector", "Budget", "Score (%)", "Outcome", "Compliance %", "Gaps Found",
}


def _dataset_payload() -> dict:
    """Rows + stats + model insights for the UI."""
    df = load_bid_history()
    rows = []
    if df is not None:
        display = df.drop(columns=[c for c in ("budget_m", "won") if c in df.columns])
        rows = display.fillna("").to_dict("records")
    return {
        "rows"   : rows,
        "stats"  : get_overall_stats(),
        "model"  : get_model_insights() if is_trained() else None,
        "source" : _CSV_PATH.name,
    }


# ---------------------------------------------------------------------------
# GET /api/bid-history
# ---------------------------------------------------------------------------
@router.get(
    "",
    summary="Get the historical bid outcomes dataset",
    description=(
        "Returns every row of the bid history CSV together with aggregate "
        "statistics (overall and per-sector win rates) and the trained "
        "win-model insights."
    ),
)
async def get_bid_history():
    return _dataset_payload()


# ---------------------------------------------------------------------------
# POST /api/bid-history/upload
# ---------------------------------------------------------------------------
@router.post(
    "/upload",
    summary="Replace the bid history dataset with an uploaded CSV",
    description=(
        "Validates the CSV (must contain the columns the win model trains "
        "on: Sector, Budget, Score (%), Outcome, Compliance %, Gaps Found), "
        "backs up the current file, replaces it, reloads the sector win "
        "rates, and retrains the LogisticRegression win model — no restart "
        "needed. The previous file is restored if the new one fails."
    ),
)
async def upload_bid_history(file: UploadFile = File(..., description="Bid history CSV")):
    # ── 1. Basic file validation ─────────────────────────────────────────────
    filename = file.filename or ""
    if not filename.lower().endswith(".csv"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{filename}' is not a CSV file. Please upload a .csv file.",
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

    # ── 2. Parse + schema validation ─────────────────────────────────────────
    try:
        df = pd.read_csv(io.BytesIO(content))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Could not parse CSV: {exc}",
        )

    missing = REQUIRED_COLUMNS - set(df.columns)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"CSV is missing required columns: {sorted(missing)}. "
                f"Required: {sorted(REQUIRED_COLUMNS)}."
            ),
        )

    df = df.dropna(how="all")
    if len(df) < 10:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Dataset has only {len(df)} rows — at least 10 are needed to train the win model.",
        )

    outcomes = set(df["Outcome"].astype(str).str.strip().str.lower().unique())
    if not outcomes <= {"win", "loss"}:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Outcome column must contain only 'Win'/'Loss' (got {sorted(outcomes)}).",
        )
    if len(outcomes) < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Dataset needs BOTH Win and Loss outcomes to train the model.",
        )

    # ── 3. Back up current file, write the new one ───────────────────────────
    if _CSV_PATH.exists():
        shutil.copy2(_CSV_PATH, _BACKUP_PATH)
    _CSV_PATH.write_bytes(content)

    # ── 4. Reload + retrain; restore the backup on failure ──────────────────
    try:
        reloaded = reload_bid_history()
        if reloaded is None or reloaded.empty:
            raise RuntimeError("Reload produced no rows.")
        if not train_win_model():
            raise RuntimeError("Win model retraining failed.")
    except Exception as exc:
        if _BACKUP_PATH.exists():
            shutil.copy2(_BACKUP_PATH, _CSV_PATH)
            reload_bid_history()
            train_win_model()
        logger.error("Bid history upload failed, previous dataset restored: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"New dataset rejected ({exc}) — previous dataset restored.",
        )

    logger.info("Bid history replaced: %d rows uploaded from '%s', model retrained.",
                len(df), filename)
    payload = _dataset_payload()
    payload["message"] = f"Dataset replaced with {len(df)} rows — win model retrained."
    return payload
