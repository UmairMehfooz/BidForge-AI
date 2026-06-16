"""
BidForge AI — Workspaces Router
================================
All workspace-related endpoints. The 3 CRUD endpoints (create / list / get)
are fully implemented. Endpoints for parse, match, draft, score, and export
are scaffolded and will be wired in Tasks 4-8.

Upload location: {TEMP_DIR}/bidforge/{workspace_id}/rfp.{ext}
  - Uses Python's tempfile.gettempdir() so it works on Windows AND Linux/Mac.
  - On Windows  → C:\\Users\\...\\AppData\\Local\\Temp\\bidforge\\{id}\\rfp.pdf
  - On Linux    → /tmp/bidforge/{id}/rfp.pdf

Endpoints
---------
POST   /api/workspaces                    → upload RFP + create workspace
GET    /api/workspaces                    → list workspaces
GET    /api/workspaces/{id}               → get workspace
POST   /api/workspaces/{id}/parse         → extract requirements (Task 4)
POST   /api/workspaces/{id}/match         → RAG compliance matching (Task 5)
POST   /api/workspaces/{id}/draft         → streaming proposal SSE (Task 6)
POST   /api/workspaces/{id}/score         → win-probability + GO/NO-GO (Task 7)
PATCH  /api/workspaces/{id}/proposal/{s}  → human edits a section (Task 8)
POST   /api/workspaces/{id}/export        → generate + download DOCX (Task 8)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, StreamingResponse

from app.models.schemas import (
    MatchResponse,
    ParseResponse,
    ProposalSectionOut,
    ProposalSectionPatch,
    RequirementPatch,
    ScoreResponse,
    WorkspaceOut,
    WorkspacePatch,
    WorkspaceStatus,
    ComplianceItemOut,
    RequirementOut,
)
from app.services.faiss_store import capability_store
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspaces", tags=["Workspaces"])

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ALLOWED_EXTENSIONS: set[str] = {".pdf", ".docx"}
MAX_FILE_SIZE_MB   : float   = 50.0

# Base temp directory — cross-platform (Windows: %TEMP%\bidforge, Linux: /tmp/bidforge)
_BIDFORGE_TMP = Path(tempfile.gettempdir()) / "bidforge"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workspace_dir(workspace_id: str) -> Path:
    """Return (and create) the per-workspace temp directory."""
    d = _BIDFORGE_TMP / workspace_id
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _get_or_404(workspace_id: str) -> dict:
    """
    Fetch a workspace row from Supabase or raise 404.
    Centralised so every endpoint has consistent error handling.
    """
    sb = get_supabase()
    try:
        result = (
            sb.table("workspaces")
            .select("*")
            .eq("id", workspace_id)
            .single()
            .execute()
        )
    except Exception as exc:
        logger.error("Supabase error fetching workspace %s: %s", workspace_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable. Please try again shortly.",
        )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace '{workspace_id}' not found.",
        )
    return result.data


def _update_workspace_status(workspace_id: str, new_status: str) -> None:
    """Patch the status column of a workspace row in Supabase."""
    sb = get_supabase()
    sb.table("workspaces").update({"status": new_status}).eq("id", workspace_id).execute()


def _record_pipeline_timing(workspace_id: str, stage: str, seconds: float) -> None:
    """
    Record one pipeline stage's wall-clock seconds into the workspace's
    pipeline_timings jsonb (Fix 9), recomputing the running total.
    Logs and continues if the column doesn't exist yet.
    """
    sb = get_supabase()
    try:
        result = (
            sb.table("workspaces")
            .select("pipeline_timings")
            .eq("id", workspace_id)
            .single()
            .execute()
        )
        timings = (result.data or {}).get("pipeline_timings") or {}
        timings[stage] = round(seconds, 1)
        timings["total"] = round(
            sum(v for k, v in timings.items()
                if k != "total" and isinstance(v, (int, float))),
            1,
        )
        sb.table("workspaces").update(
            {"pipeline_timings": timings}
        ).eq("id", workspace_id).execute()
        logger.info("Pipeline timing: workspace=%s %s=%.1fs total=%.1fs",
                    workspace_id, stage, seconds, timings["total"])
    except Exception as exc:
        logger.warning(
            "Could not record pipeline timing (%s). Run: ALTER TABLE workspaces "
            "ADD COLUMN IF NOT EXISTS pipeline_timings JSONB;", exc,
        )


# Detected once per process — avoids paying a guaranteed-failing bid_scores
# round trip on every request until the rename migration has been run.
_score_table_name: str | None = None


def _score_table(sb) -> str:
    """'bid_scores' if it exists, else the legacy 'win_scores' (Fix 12)."""
    global _score_table_name
    if _score_table_name:
        return _score_table_name
    try:
        sb.table("bid_scores").select("id").limit(1).execute()
        _score_table_name = "bid_scores"
        return _score_table_name
    except Exception as exc:
        # Only fall back when bid_scores is CONFIRMED absent (PGRST205).
        # Transient errors (dropped connections etc.) must not sticky-cache
        # the wrong table for the life of the process.
        if "PGRST205" in str(exc) or "Could not find the table" in str(exc):
            logger.warning(
                "bid_scores table missing — using LEGACY win_scores. "
                "Run the rename migration in schema.sql."
            )
            _score_table_name = "win_scores"
            return _score_table_name
        logger.warning("Score-table probe failed transiently (%s) — assuming bid_scores.", exc)
        return "bid_scores"   # not cached — re-probed on the next call


def _fetch_latest_score(sb, workspace_id: str):
    """Latest saved score row for a workspace (canonical or legacy table)."""
    return (
        sb.table(_score_table(sb))
        .select("*")
        .eq("workspace_id", workspace_id)
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )


def _build_effort_metrics(workspace: dict) -> dict:
    """
    effort_metrics for /overview (Fix 9):
        automated_minutes, manual_baseline_hours, reduction_percent
    Baseline comes from MANUAL_BASELINE_HOURS (env, default 6.0) — set it from
    an honestly timed manual exercise on a sample RFP.
    """
    try:
        baseline_hours = float(os.getenv("MANUAL_BASELINE_HOURS", "6.0"))
    except ValueError:
        baseline_hours = 6.0

    timings = workspace.get("pipeline_timings") or {}
    total_seconds = timings.get("total")
    if not isinstance(total_seconds, (int, float)) or total_seconds <= 0:
        return {
            "automated_minutes"    : None,
            "manual_baseline_hours": baseline_hours,
            "reduction_percent"    : None,
        }

    reduction = (1.0 - total_seconds / (baseline_hours * 3600.0)) * 100.0
    return {
        "automated_minutes"    : round(total_seconds / 60.0, 1),
        "manual_baseline_hours": baseline_hours,
        "reduction_percent"    : round(max(0.0, min(100.0, reduction)), 1),
    }


# ---------------------------------------------------------------------------
# POST /api/workspaces
# Upload RFP document + create workspace record
# ---------------------------------------------------------------------------
@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=WorkspaceOut,
    summary="Create a new workspace and upload the RFP document",
    description=(
        "Accepts a multipart form with the workspace `name` and the RFP `file` "
        "(PDF or DOCX, max 50 MB). Saves the file to the server temp directory "
        "and creates a workspace record in Supabase with status='uploaded'."
    ),
)
async def create_workspace(
    name: str       = Form(..., min_length=2, max_length=120, description="Workspace / project name"),
    file: UploadFile = File(..., description="RFP document — PDF or DOCX"),
):
    # ── 1. Validate file extension ───────────────────────────────────────────
    original_filename = file.filename or ""
    ext = Path(original_filename).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported file type '{ext or '(none)'}'. "
                f"Please upload a PDF or DOCX file."
            ),
        )

    # ── 2. Read file bytes + size guard ─────────────────────────────────────
    content = await file.read()
    size_mb = len(content) / (1024 * 1024)

    if size_mb > MAX_FILE_SIZE_MB:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"File '{original_filename}' is {size_mb:.1f} MB — "
                f"maximum allowed size is {MAX_FILE_SIZE_MB:.0f} MB."
            ),
        )

    if len(content) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty.",
        )

    # ── 3. Persist file to tmp/bidforge/{workspace_id}/rfp.{ext} ───────────
    workspace_id = str(uuid.uuid4())
    save_dir     = _workspace_dir(workspace_id)
    save_path    = save_dir / f"rfp{ext}"

    try:
        save_path.write_bytes(content)
        logger.info(
            "RFP saved: workspace=%s  path=%s  size=%.2f MB",
            workspace_id, save_path, size_mb,
        )
    except OSError as exc:
        logger.error("Failed to write RFP to disk: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to save uploaded file: {exc}",
        )

    # ── 4. Insert workspace row into Supabase ────────────────────────────────
    row = {
        "id"           : workspace_id,
        "name"         : name.strip(),
        "status"       : "uploaded",
        "rfp_file_path": str(save_path),
    }

    sb = get_supabase()
    try:
        result = sb.table("workspaces").insert(row).execute()
    except Exception as exc:
        # Roll back the saved file so we don't leave orphaned files on disk
        save_path.unlink(missing_ok=True)
        logger.error("Supabase insert failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error — workspace not created: {exc}",
        )

    if not result.data:
        save_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Workspace insert returned no data. Check Supabase RLS policies.",
        )

    logger.info("Workspace created: id=%s  name='%s'", workspace_id, name)
    return result.data[0]


# ---------------------------------------------------------------------------
# GET /api/workspaces
# List all workspaces, newest first
# ---------------------------------------------------------------------------
@router.get(
    "",
    response_model=list[WorkspaceOut],
    summary="List all workspaces",
    description="Returns all workspaces ordered by creation time (newest first).",
)
async def list_workspaces():
    sb = get_supabase()
    try:
        result = (
            sb.table("workspaces")
            .select("*")
            .order("created_at", desc=True)
            .execute()
        )
    except Exception as exc:
        logger.error("Supabase list_workspaces failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable. Please try again shortly.",
        )

    return result.data or []


# ---------------------------------------------------------------------------
# GET /api/workspaces/dashboard
# Bulk summary for the homepage — 3 parallel queries instead of one
# /overview round-trip storm per workspace.
# NOTE: must be declared BEFORE /{workspace_id} so 'dashboard' isn't
# captured as a workspace id.
# ---------------------------------------------------------------------------
@router.get(
    "/dashboard",
    summary="Bulk dashboard summary for all workspaces",
    description=(
        "Returns every workspace with its compliance percentage, latest score, "
        "and effort metrics — computed server-side from 3 bulk queries. "
        "Replaces the per-workspace /overview fan-out the dashboard used to do."
    ),
)
async def dashboard_summary():
    sb = get_supabase()
    score_table = _score_table(sb)

    def q_workspaces():
        return sb.table("workspaces").select("*").order("created_at", desc=True).execute()

    def q_compliance():
        return sb.table("compliance_items").select("workspace_id, status").execute()

    def q_scores():
        return sb.table(score_table).select("*").order("created_at", desc=True).execute()

    try:
        ws_res, comp_res, score_res = await asyncio.gather(
            asyncio.to_thread(q_workspaces),
            asyncio.to_thread(q_compliance),
            asyncio.to_thread(q_scores),
        )
    except Exception as exc:
        logger.error("Dashboard summary queries failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable while loading the dashboard.",
        )

    # Compliance counts per workspace
    comp_by_ws: dict[str, dict[str, int]] = {}
    for item in comp_res.data or []:
        ws_id = item.get("workspace_id")
        bucket = comp_by_ws.setdefault(ws_id, {"pass": 0, "partial": 0, "total": 0})
        bucket["total"] += 1
        item_status = str(item.get("status") or "").lower()
        if item_status in ("pass", "partial"):
            bucket[item_status] += 1

    # Latest score per workspace (rows are already newest-first)
    latest_score_by_ws: dict[str, dict] = {}
    for row in score_res.data or []:
        latest_score_by_ws.setdefault(row.get("workspace_id"), row)

    summaries = []
    for ws in ws_res.data or []:
        counts = comp_by_ws.get(ws["id"], {"pass": 0, "partial": 0, "total": 0})
        compliance_pct = (
            round((counts["pass"] + counts["partial"] * 0.5) / counts["total"] * 100)
            if counts["total"] else 0
        )
        score_row = latest_score_by_ws.get(ws["id"])
        overall = None
        if score_row:
            overall = score_row.get("overall_score")
            if overall is None:
                overall = score_row.get("score")
        effort = _build_effort_metrics(ws)
        summaries.append({
            "id"               : ws["id"],
            "name"             : ws.get("name"),
            "status"           : ws.get("status"),
            "sector"           : ws.get("sector"),
            "compliance_pct"   : compliance_pct,
            "score_pct"        : round(float(overall) * 100, 1) if overall is not None else None,
            "decision"         : (score_row or {}).get("decision"),
            "reduction_percent": effort["reduction_percent"],
        })

    return {"workspaces": summaries}


# ---------------------------------------------------------------------------
# GET /api/workspaces/{workspace_id}
# Fetch a single workspace with full details
# ---------------------------------------------------------------------------
@router.get(
    "/{workspace_id}",
    response_model=WorkspaceOut,
    summary="Get a workspace by ID",
    description="Returns the full workspace record including status and file path.",
)
async def get_workspace(workspace_id: str):
    return await _get_or_404(workspace_id)


# ---------------------------------------------------------------------------
# PATCH /api/workspaces/{workspace_id}
# Manual workspace inputs — currently competitor_presence (Fix 7)
# ---------------------------------------------------------------------------
@router.patch(
    "/{workspace_id}",
    response_model=WorkspaceOut,
    summary="Update manual workspace inputs (competitor presence)",
    description=(
        "Sets `competitor_presence` (unknown / low / medium / high) — the "
        "manual competitor-intelligence input used as the 6th scoring factor. "
        "Re-run POST /score afterwards to refresh the win probability."
    ),
)
async def patch_workspace(workspace_id: str, body: WorkspacePatch):
    await _get_or_404(workspace_id)

    sb = get_supabase()
    try:
        result = (
            sb.table("workspaces")
            .update({"competitor_presence": body.competitor_presence.value})
            .eq("id", workspace_id)
            .execute()
        )
    except Exception as exc:
        logger.error("Supabase patch_workspace failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Database error while updating workspace. If the column is "
                "missing, run: ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS "
                "competitor_presence TEXT DEFAULT 'unknown';"
            ),
        )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace '{workspace_id}' not found.",
        )

    logger.info(
        "Workspace %s competitor_presence set to '%s'.",
        workspace_id, body.competitor_presence.value,
    )
    return result.data[0]


# ---------------------------------------------------------------------------
# DELETE /api/workspaces/{workspace_id}
# Delete a bid workspace and everything attached to it
# ---------------------------------------------------------------------------
@router.delete(
    "/{workspace_id}",
    summary="Delete a workspace (bid) and all its data",
    description=(
        "Deletes the workspace row; requirements, compliance items, proposal "
        "sections, and scores are removed via ON DELETE CASCADE. The uploaded "
        "RFP file directory is also cleaned up."
    ),
)
async def delete_workspace(workspace_id: str):
    await _get_or_404(workspace_id)

    sb = get_supabase()

    # Children cascade from the FK constraints, but older databases created
    # before schema.sql may lack them — delete children explicitly first.
    for table in ("compliance_items", "proposal_sections", "requirements", "bid_scores"):
        try:
            sb.table(table).delete().eq("workspace_id", workspace_id).execute()
        except Exception as exc:
            logger.warning("Cleanup of %s for workspace %s skipped: %s",
                           table, workspace_id, exc)

    try:
        result = sb.table("workspaces").delete().eq("id", workspace_id).execute()
    except Exception as exc:
        logger.error("Supabase delete_workspace failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while deleting workspace.",
        )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workspace '{workspace_id}' not found.",
        )

    # Remove uploaded RFP + generated exports from the temp dir
    try:
        ws_dir = Path(tempfile.gettempdir()) / "bidforge" / workspace_id
        if ws_dir.exists():
            shutil.rmtree(ws_dir, ignore_errors=True)
    except Exception as exc:
        logger.warning("Could not remove files for workspace %s: %s", workspace_id, exc)

    logger.info("Workspace deleted: %s", workspace_id)
    return {"deleted": True, "workspace_id": workspace_id}


# ---------------------------------------------------------------------------
# PATCH /api/workspaces/{workspace_id}/requirements/{requirement_id}
# Bid-manager note + done mark on a single requirement
# ---------------------------------------------------------------------------
@router.patch(
    "/{workspace_id}/requirements/{requirement_id}",
    summary="Save a note and/or done-mark on a requirement",
    description=(
        "Updates the bid manager's `note` and/or `is_done` flag on one "
        "requirement. Send only the fields you want to change."
    ),
)
async def patch_requirement(
    workspace_id: str,
    requirement_id: str,
    body: RequirementPatch,
):
    await _get_or_404(workspace_id)

    update_payload: dict[str, Any] = {}
    if body.note is not None:
        update_payload["note"] = body.note
    if body.is_done is not None:
        update_payload["is_done"] = body.is_done
    if not update_payload:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Provide at least one of: note, is_done.",
        )

    sb = get_supabase()
    try:
        result = (
            sb.table("requirements")
            .update(update_payload)
            .eq("id", requirement_id)
            .eq("workspace_id", workspace_id)
            .execute()
        )
    except Exception as exc:
        if "note" in str(exc) or "is_done" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    "Database columns missing — run in the Supabase SQL editor: "
                    "ALTER TABLE requirements ADD COLUMN IF NOT EXISTS note TEXT; "
                    "ALTER TABLE requirements ADD COLUMN IF NOT EXISTS is_done "
                    "BOOLEAN DEFAULT FALSE;"
                ),
            )
        logger.error("Supabase patch_requirement failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while saving requirement note.",
        )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Requirement '{requirement_id}' not found in workspace '{workspace_id}'.",
        )

    logger.info("Requirement %s updated: %s", requirement_id, list(update_payload))
    return result.data[0]


# ---------------------------------------------------------------------------
# GET /api/workspaces/{id}/overview
# Returns workspace + requirements + compliance + latest score for the UI
# ---------------------------------------------------------------------------
@router.get(
    "/{workspace_id}/overview",
    summary="Get the full workspace overview for the dashboard",
    description="Returns the workspace record together with requirements, compliance items, and the latest saved score.",
)
async def get_workspace_overview(workspace_id: str):
    sb = get_supabase()
    workspace = await _get_or_404(workspace_id)

    def _table_rows(table: str):
        return (
            sb.table(table)
            .select("*")
            .eq("workspace_id", workspace_id)
            .order("created_at", desc=False)
            .execute()
        )

    async def _fetch_all():
        # The Supabase client is synchronous — run the 4 queries in worker
        # threads concurrently instead of serially blocking the event loop.
        return await asyncio.gather(
            asyncio.to_thread(_table_rows, "requirements"),
            asyncio.to_thread(_table_rows, "compliance_items"),
            asyncio.to_thread(_table_rows, "proposal_sections"),
            asyncio.to_thread(_fetch_latest_score, sb, workspace_id),
        )

    try:
        try:
            req_result, comp_result, prop_result, score_result = await _fetch_all()
        except Exception as first_exc:
            # Supabase's pooler occasionally drops one connection under a
            # concurrent burst ("Server disconnected") — retry once.
            logger.warning("Overview fetch retry for %s: %s", workspace_id, first_exc)
            await asyncio.sleep(0.3)
            req_result, comp_result, prop_result, score_result = await _fetch_all()
    except Exception as exc:
        logger.error("Supabase overview fetch failed for workspace %s: %s", workspace_id, exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database unavailable while loading workspace overview.",
        )

    latest_score = score_result.data[0] if score_result.data else None

    return {
        "workspace": workspace,
        "requirements": req_result.data or [],
        "compliance_items": comp_result.data or [],
        "proposal_sections": prop_result.data or [],
        "latest_score": latest_score,
        "effort_metrics": _build_effort_metrics(workspace),
    }


# ---------------------------------------------------------------------------
# POST /api/workspaces/{id}/parse
# Full pipeline: document_parser → GROQ extraction → Supabase insert
# ---------------------------------------------------------------------------
@router.post(
    "/{workspace_id}/parse",
    response_model=ParseResponse,
    summary="Parse the RFP and extract structured requirements",
    description=(
        "Reads the uploaded RFP with PyMuPDF/python-docx, sends the text to "
        "GROQ (llama-3.3-70b-versatile) for requirement extraction, stores all "
        "requirements in the `requirements` table, and updates the workspace "
        "status to **'parsed'**."
    ),
)
async def parse_workspace(workspace_id: str):
    ws = await _get_or_404(workspace_id)
    pipeline_started = time.perf_counter()

    # ── 1. Validate the RFP file is still on disk ────────────────────────────
    rfp_path = ws.get("rfp_file_path")
    if not rfp_path or not Path(rfp_path).exists():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                "RFP file not found on disk. "
                "Please re-upload the document via POST /api/workspaces."
            ),
        )

    # ── 2. Extract raw text from PDF or DOCX ────────────────────────────────
    from app.services.document_parser import parse_document
    raw_text = parse_document(rfp_path)   # raises HTTPException on failure

    logger.info(
        "RFP text extracted: workspace=%s  chars=%d",
        workspace_id, len(raw_text),
    )

    # ── 3. Send to GROQ → parse JSON → validate → save to Supabase ──────────
    from app.services.requirement_extractor import extract_and_save
    inserted_count = await extract_and_save(workspace_id, raw_text)

    # ── 4. Update workspace status ───────────────────────────────────────────
    _update_workspace_status(workspace_id, "parsed")
    _record_pipeline_timing(workspace_id, "parse", time.perf_counter() - pipeline_started)
    logger.info(
        "Workspace parsed: id=%s  requirements=%d",
        workspace_id, inserted_count,
    )

    return ParseResponse(
        workspace_id=workspace_id,
        requirements_extracted=inserted_count,
        message=(
            f"Successfully extracted and saved {inserted_count} requirements. "
            f"Workspace status updated to 'parsed'."
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/workspaces/{id}/match
# Full pipeline: load requirements → FAISS + GROQ → save compliance_items
# ---------------------------------------------------------------------------
@router.post(
    "/{workspace_id}/match",
    response_model=MatchResponse,
    summary="Run RAG compliance matching against the Capability Library",
    description=(
        "For each extracted requirement, searches the FAISS Capability Store "
        "(top-3) and asks GROQ to decide pass / fail / partial. Results saved to "
        "`compliance_items`. **Status updated to 'matched' on success.** "
        "Run /parse first."
    ),
)
async def match_workspace(workspace_id: str):
    await _get_or_404(workspace_id)
    pipeline_started = time.perf_counter()

    # ── Run full compliance matching pipeline ──────────────────────────────────
    # Raises HTTPException 422 if no requirements exist yet
    from app.services.compliance_matcher import run_compliance_match
    summary = await run_compliance_match(workspace_id)

    # ── Update workspace status ──────────────────────────────────────────
    _update_workspace_status(workspace_id, "matched")
    _record_pipeline_timing(workspace_id, "match", time.perf_counter() - pipeline_started)
    logger.info(
        "Workspace matched: id=%s  total=%d  pass=%d  fail=%d  partial=%d  rate=%.1f%%",
        workspace_id,
        summary["total"],
        summary["pass_count"],
        summary["fail_count"],
        summary["partial_count"],
        summary["compliance_rate"] * 100,
    )

    return MatchResponse(
        workspace_id    = workspace_id,
        total           = summary["total"],
        items_evaluated = summary["total"],
        pass_count      = summary["pass_count"],
        fail_count      = summary["fail_count"],
        partial_count   = summary["partial_count"],
        compliance_rate = summary["compliance_rate"],
        message=(
            f"Compliance matching complete: "
            f"{summary['pass_count']} pass, "
            f"{summary['fail_count']} fail, "
            f"{summary['partial_count']} partial. "
            f"Compliance rate: {summary['compliance_rate']*100:.1f}%."
        ),
    )


# ---------------------------------------------------------------------------
# POST /api/workspaces/{id}/draft
# Full pipeline: stream GROQ proposal drafts via SSE, save to Supabase
# ---------------------------------------------------------------------------
@router.post(
    "/{workspace_id}/draft",
    summary="Stream AI-generated proposal sections via Server-Sent Events",
    description=(
        "For each draftable requirement (mandatory / question), retrieves "
        "top-3 FAISS capabilities and streams a GROQ-generated proposal "
        "section token-by-token using SSE.\n\n"
        "**SSE event format:**\n"
        '```\ndata: {"section": "4.3", "title": "...", "token": "word", "done": false}\n```\n'
        "Final event: `data: [DONE]`\n\n"
        "After streaming completes, all sections are batch-saved to "
        "`proposal_sections` and workspace status is updated to **'drafted'**."
    ),
)
async def draft_workspace(workspace_id: str, restart: bool = False):
    # Validate workspace exists before opening the stream
    await _get_or_404(workspace_id)

    from app.services.proposal_generator import stream_proposal_sections

    logger.info(
        "Starting SSE proposal stream for workspace=%s (restart=%s)",
        workspace_id, restart,
    )

    async def timed_stream():
        # Time the full stream (Fix 9) — recorded when the generator finishes
        stream_started = time.perf_counter()
        try:
            async for chunk in stream_proposal_sections(workspace_id, restart=restart):
                yield chunk
        finally:
            _record_pipeline_timing(
                workspace_id, "draft", time.perf_counter() - stream_started
            )

    return StreamingResponse(
        timed_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # disable Nginx buffering on Railway/Render
        },
    )


# ---------------------------------------------------------------------------
# POST /api/workspaces/{id}/score
# Full pipeline: load data → 5-criteria scoring → GO/CONDITIONAL/NO-GO
# ---------------------------------------------------------------------------
@router.post(
    "/{workspace_id}/score",
    response_model=ScoreResponse,
    summary="Compute win-probability score and GO/NO-GO decision",
    description=(
        "Runs 5 weighted heuristics:\n\n"
        "1. **Compliance rate** (30%) — pass/total from compliance items\n"
        "2. **Domain match** (25%) — RFP domain vs capability library\n"
        "3. **Budget alignment** (20%) — RFP budget vs matched capability values\n"
        "4. **Past win rate** (15%) — historical data from same domain\n"
        "5. **Capability depth** (10%) — avg confidence from compliance\n\n"
        "Returns overall score, decision (GO/CONDITIONAL/NO-GO), gap list, "
        "and a human-readable recommendation. "
        "**Status updated to 'scored' on success.** Run /parse and /match first."
    ),
)
async def score_workspace(workspace_id: str):
    await _get_or_404(workspace_id)
    pipeline_started = time.perf_counter()

    from app.services.scoring_engine import compute_bid_score
    result = await compute_bid_score(workspace_id)

    _update_workspace_status(workspace_id, "scored")
    _record_pipeline_timing(workspace_id, "score", time.perf_counter() - pipeline_started)
    logger.info(
        "Workspace scored: id=%s  score=%.1f%%  decision=%s",
        workspace_id,
        result["overall_score_pct"],
        result["decision"],
    )

    return result


# ---------------------------------------------------------------------------
# PATCH /api/workspaces/{id}/proposal/{section_id}
# Human editor saves edits to a proposal section
# ---------------------------------------------------------------------------
@router.patch(
    "/{workspace_id}/proposal/{section_id}",
    response_model=ProposalSectionOut,
    summary="Update a proposal section with human-edited content",
    description=(
        "Saves the bid manager's edits to `edited_draft` and updates the section "
        "status to 'edited' (or 'approved' if the client sends status=approved)."
    ),
)
async def patch_proposal_section(
    workspace_id: str,
    section_id  : str,
    body        : ProposalSectionPatch,
):
    await _get_or_404(workspace_id)   # verify workspace exists first

    sb = get_supabase()
    update_payload = {
        "edited_draft": body.edited_draft,
        "status"      : body.status.value,
    }

    try:
        result = (
            sb.table("proposal_sections")
            .update(update_payload)
            .eq("id", section_id)
            .eq("workspace_id", workspace_id)
            .execute()
        )
    except Exception as exc:
        logger.error("Supabase patch_proposal_section failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database error while saving edits.",
        )

    if not result.data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Proposal section '{section_id}' not found in workspace '{workspace_id}'.",
        )

    logger.info(
        "Proposal section updated: workspace=%s  section=%s  status=%s",
        workspace_id, section_id, body.status.value,
    )
    return result.data[0]


# ---------------------------------------------------------------------------
# POST /api/workspaces/{id}/export
# Generate and download the final DOCX proposal  [Task 8]
# ---------------------------------------------------------------------------
@router.post(
    "/{workspace_id}/export",
    summary="Generate and download the final proposal as a DOCX file",
    description=(
        "Assembles all approved proposal sections, compliance checklist, and "
        "score page into a formatted DOCX file for download. "
        "**Status updated to 'exported' on success.**"
    ),
)
async def export_workspace(workspace_id: str):
    await _get_or_404(workspace_id)
    pipeline_started = time.perf_counter()

    from app.services.docx_exporter import generate_docx_export

    docx_path = await generate_docx_export(workspace_id)
    _update_workspace_status(workspace_id, "exported")
    _record_pipeline_timing(workspace_id, "export", time.perf_counter() - pipeline_started)
    
    return FileResponse(
        path=docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"BidForge_Proposal_{workspace_id[:8]}.docx",
    )


# ---------------------------------------------------------------------------
# POST /api/workspaces/{id}/export-full
# Generate and download the complete 8-section proposal document
# ---------------------------------------------------------------------------
@router.post(
    "/{workspace_id}/export-full",
    summary="Generate and download the complete 8-section proposal as DOCX",
    description=(
        "Builds the full proposal: Cover Page, Executive Summary, Understanding "
        "of Requirements, Technical Proposal (approved/edited drafts per "
        "requirement with evidence citations and explicit gap flags), Company "
        "Profile, 3 Past-Project case studies, Compliance Matrix, Appendices. "
        "**Status updated to 'exported' on success.**"
    ),
)
async def export_workspace_full(workspace_id: str):
    await _get_or_404(workspace_id)
    pipeline_started = time.perf_counter()

    from app.services.docx_exporter import generate_full_proposal_docx

    docx_path = await generate_full_proposal_docx(workspace_id)
    _update_workspace_status(workspace_id, "exported")
    _record_pipeline_timing(workspace_id, "export", time.perf_counter() - pipeline_started)

    return FileResponse(
        path=docx_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=f"BidForge_Full_Proposal_{workspace_id[:8]}.docx",
    )
