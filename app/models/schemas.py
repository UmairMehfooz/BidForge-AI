"""
BidForge AI — Pydantic models (request / response schemas).
All database-facing IDs are UUIDs (str) to match Supabase default.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class WorkspaceStatus(str, Enum):
    UPLOADED  = "uploaded"
    CREATED   = "created"
    PARSED    = "parsed"
    MATCHED   = "matched"
    DRAFTED   = "drafted"
    SCORED    = "scored"
    EXPORTED  = "exported"


class ComplianceStatus(str, Enum):
    PASS    = "pass"
    FAIL    = "fail"
    PARTIAL = "partial"


class GoNoGo(str, Enum):
    GO          = "GO"
    NO_GO       = "NO-GO"
    CONDITIONAL = "CONDITIONAL"
    HOLD        = "HOLD"


class RequirementType(str, Enum):
    MANDATORY   = "mandatory"
    EVALUATION  = "evaluation_criteria"
    DEADLINE    = "submission_deadline"
    BUDGET      = "budget"
    QA          = "question"
    OTHER       = "other"


class ProposalSectionStatus(str, Enum):
    DRAFT    = "draft"
    APPROVED = "approved"
    EDITED   = "edited"


class CompetitorPresence(str, Enum):
    """Manual competitor-intelligence input (Fix 7)."""
    UNKNOWN = "unknown"   # not provided — scored as neutral, flagged in gaps
    LOW     = "low"       # 0-1 known competitors
    MEDIUM  = "medium"    # 2-3
    HIGH    = "high"      # 4+


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------

class WorkspaceCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120, description="Human-readable workspace name")


class WorkspaceOut(BaseModel):
    id: str
    name: str
    status: WorkspaceStatus
    rfp_file_path: Optional[str] = None
    sector: Optional[str] = None        # inferred during /parse (Fix 1)
    issuer_type: Optional[str] = None   # 'govt'/'private', inferred during /parse (Fix 3)
    warning: Optional[str] = None       # pipeline warnings, e.g. truncation (Fix 5)
    competitor_presence: Optional[str] = None   # unknown/low/medium/high (Fix 7)
    pipeline_timings: Optional[dict] = None     # per-stage wall-clock seconds (Fix 9)
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# Requirements
# ---------------------------------------------------------------------------

class RequirementOut(BaseModel):
    id: str
    workspace_id: str
    section_ref: Optional[str] = None
    requirement: str
    type: RequirementType
    deadline: Optional[str] = None
    budget_ref: Optional[str] = None
    extraction_source: Optional[str] = None   # 'llm' | 'ner' | 'both' (Fix 4)
    taxonomy_id: Optional[str] = None         # evaluation-criteria taxonomy (Fix 8)
    taxonomy_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------

class ComplianceItemOut(BaseModel):
    id: str
    requirement_id: str
    status: ComplianceStatus
    matched_capability_id: Optional[str] = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    gap_note: Optional[str] = None


# ---------------------------------------------------------------------------
# Proposal
# ---------------------------------------------------------------------------

class ProposalSectionOut(BaseModel):
    id: str
    workspace_id: str
    # Optional — legacy rows saved before the requirement_id/section_title
    # migration have NULLs here; a required field made the PATCH endpoint
    # 500 on response serialization AFTER the DB write had already succeeded.
    section_title: Optional[str] = None
    ai_draft: Optional[str] = None
    edited_draft: Optional[str] = None
    status: ProposalSectionStatus


class ProposalSectionPatch(BaseModel):
    edited_draft: str = Field(..., min_length=1, description="Human-edited content for this section")
    status: ProposalSectionStatus = ProposalSectionStatus.EDITED


class RequirementPatch(BaseModel):
    """PATCH /api/workspaces/{id}/requirements/{req_id} — bid-manager notes."""
    note: Optional[str] = None
    is_done: Optional[bool] = None


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class GapItem(BaseModel):
    requirement_id: str
    section_ref: str
    requirement: str
    status: str
    confidence: float
    gap_note: Optional[str] = None


class ScoreStats(BaseModel):
    total_requirements: int
    total_compliance: int
    pass_count: int
    fail_count: int
    partial_count: int


class ScoreResponse(BaseModel):
    """Full response from the scoring engine."""
    id               : str
    workspace_id     : str
    overall_score    : float = Field(..., ge=0.0, le=1.0)
    overall_score_pct: float = Field(..., ge=0.0, le=100.0)
    decision         : GoNoGo
    breakdown        : dict[str, float]
    weights          : dict[str, float]
    gaps             : list[GapItem]
    recommendation   : str
    stats            : ScoreStats
    # Additive (Fix 1): model_probability, heuristic_score, model_insights,
    # sector, sector_win_rate, blend_weights — same jsonb stored in bid_scores.
    score_breakdown  : Optional[dict] = None


# ---------------------------------------------------------------------------
# Generic responses
# ---------------------------------------------------------------------------

class MessageResponse(BaseModel):
    message: str


class ParseResponse(BaseModel):
    workspace_id: str
    requirements_extracted: int
    message: str


class WorkspacePatch(BaseModel):
    """PATCH /api/workspaces/{id} — manual workspace inputs (Fix 7)."""
    competitor_presence: CompetitorPresence


class MatchResponse(BaseModel):
    workspace_id    : str
    total           : int
    items_evaluated : int
    pass_count      : int
    fail_count      : int
    partial_count   : int
    compliance_rate : float = Field(..., ge=0.0, le=1.0, description="Pass + 0.5×Partial / Total")
    message         : str
