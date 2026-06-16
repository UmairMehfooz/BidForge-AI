"""
BidForge AI — Scoring Engine (Win-Probability + GO/NO-GO)
==========================================================
Blends a trained win-probability model with a weighted heuristic score:

    final = 0.6 * LogisticRegression probability (app/services/win_model.py,
                  trained on the 120-row historical bid dataset)
          + 0.4 * weighted heuristic score (5 criteria below)

If the model/dataset is unavailable, falls back to the pure heuristic score.

Heuristic Criteria & Weights (kept for UI explainability)
---------------------------------------------------------
  1. compliance_rate     (25%)  — pass/(total) from compliance_items
  2. domain_match        (22%)  — RFP domain vs capability library domains
  3. budget_alignment    (18%)  — RFP budget vs avg matched capability contract value
  4. past_win_rate       (15%)  — sector win rate from app/data/bid_history.csv
                                  (sector inferred during /parse, stored on workspace)
  5. capability_depth    (10%)  — average confidence score from compliance_items
  6. competitor_presence (10%)  — manual input on the workspace (Fix 7):
                                  low=1.0, medium=0.6, high=0.3, unknown=0.6+flag

Decision Thresholds
-------------------
  >= 0.70  →  GO
  >= 0.50  →  CONDITIONAL  (top 3 gaps listed)
  <  0.50  →  NO-GO

Public API
----------
    result = await compute_bid_score(workspace_id)
    # → dict with overall_score, decision, breakdown, gaps, recommendation

Design decisions
----------------
- All data is loaded from Supabase (requirements + compliance_items) so
  the scorer can run independently — no in-memory state from previous steps.
- Historical bids are loaded from a static JSON file so no GROQ calls are
  needed — the scorer is instant and deterministic.
- Domain matching uses a fuzzy alias map (e.g. "Road Construction" →
  "Construction") for broad coverage.
- Budget parsing handles PKR strings ("PKR 50M" → 50_000_000).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, status

from app.services.bid_history import (
    get_sector_win_rate,
    infer_sector_from_text,
    parse_budget_millions,
)
from app.services.capability_store import get_all_capabilities
from app.services.win_model import get_model_insights, is_trained, predict_win_probability
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------
_DATA_DIR          = Path(__file__).parent.parent / "data"
_HISTORICAL_JSON   = _DATA_DIR / "historical_bids.json"

WEIGHTS = {
    "compliance_rate"    : 0.25,
    "domain_match"       : 0.22,
    "budget_alignment"   : 0.18,
    "past_win_rate"      : 0.15,
    "capability_depth"   : 0.10,
    "competitor_presence": 0.10,   # manual input (Fix 7) — neutral when unknown
}

# competitor_presence → sub-score (low = few competitors = better odds)
COMPETITOR_SCORES = {
    "low"    : 1.0,
    "medium" : 0.6,
    "high"   : 0.3,
    "unknown": 0.6,   # neutral, flagged in score_breakdown.gaps
}

GO_THRESHOLD          = 0.70
CONDITIONAL_THRESHOLD = 0.50

# Final probability blend: trained model vs heuristic weighted score.
MODEL_BLEND_WEIGHT     = 0.60
HEURISTIC_BLEND_WEIGHT = 0.40


# ---------------------------------------------------------------------------
# Historical bids loader (cached at module level)
# ---------------------------------------------------------------------------
_historical_data: dict[str, Any] | None = None


def _load_historical_bids() -> dict[str, Any]:
    global _historical_data
    if _historical_data is not None:
        return _historical_data

    if not _HISTORICAL_JSON.exists():
        logger.warning("historical_bids.json not found — using empty history.")
        _historical_data = {"domains": {}, "domain_aliases": {}}
        return _historical_data

    with open(_HISTORICAL_JSON, "r", encoding="utf-8") as f:
        _historical_data = json.load(f)

    logger.info(
        "Loaded historical bids: %d domains, %d aliases",
        len(_historical_data.get("domains", {})),
        len(_historical_data.get("domain_aliases", {})),
    )
    return _historical_data


# ---------------------------------------------------------------------------
# Budget parsing — "PKR 50M" → 50_000_000
# ---------------------------------------------------------------------------

def _parse_budget(budget_str: str | None) -> float | None:
    """
    Parse a PKR budget string into a numeric value.
    Handles: "PKR 50M", "PKR 1.5B", "50 Million PKR", "Rs. 300M", plain numbers.
    Returns None if unparseable.
    """
    if not budget_str:
        return None

    text = str(budget_str).strip().upper()
    # Remove currency prefixes
    text = re.sub(r"(PKR|RS\.?|RUPEES?)\s*", "", text, flags=re.IGNORECASE).strip()

    # Try to extract number + optional multiplier
    match = re.search(r"([\d,.]+)\s*(B|BILLION|M|MILLION|K|THOUSAND|CR|CRORE|L|LAKH)?", text, re.IGNORECASE)
    if not match:
        return None

    try:
        number = float(match.group(1).replace(",", ""))
    except ValueError:
        return None

    multiplier_str = (match.group(2) or "").upper()
    multiplier_map = {
        "B": 1_000_000_000, "BILLION": 1_000_000_000,
        "M": 1_000_000, "MILLION": 1_000_000,
        "K": 1_000, "THOUSAND": 1_000,
        "CR": 10_000_000, "CRORE": 10_000_000,
        "L": 100_000, "LAKH": 100_000,
    }
    multiplier = multiplier_map.get(multiplier_str, 1)
    return number * multiplier


# ---------------------------------------------------------------------------
# 1. Compliance Rate (30%)
# ---------------------------------------------------------------------------

def _score_compliance_rate(compliance_items: list[dict]) -> float:
    """
    pass_count / total.  Partial counts as 0.5.
    Returns 0.0 if no items.
    """
    if not compliance_items:
        return 0.0

    total    = len(compliance_items)
    passes   = sum(1 for c in compliance_items if c.get("status") == "pass")
    partials = sum(1 for c in compliance_items if c.get("status") == "partial")

    score = (passes + partials * 0.5) / total
    return round(min(1.0, max(0.0, score)), 4)


# ---------------------------------------------------------------------------
# 2. Domain Match (25%)
# ---------------------------------------------------------------------------

def _score_domain_match(requirements: list[dict], capabilities: list[dict]) -> float:
    """
    Determine how well the RFP domain matches the company's capability domains.
    - Exact match  → 1.0
    - Related      → 0.6  (via alias mapping)
    - No match     → 0.2
    """
    history = _load_historical_bids()
    aliases: dict[str, str] = history.get("domain_aliases", {})

    # Infer RFP domain from requirements — look for sector/domain keywords
    rfp_text = " ".join(r.get("requirement", "") for r in requirements).lower()

    # Build set of all capability domains + their aliases
    cap_domains: set[str] = set()
    for c in capabilities:
        domain = c.get("domain", "")
        cap_domains.add(domain.lower())
        # Also add the canonical alias if it exists
        canonical = aliases.get(domain, domain)
        cap_domains.add(canonical.lower())

    # Also check all known domains from historical data
    all_known_domains = set(d.lower() for d in history.get("domains", {}).keys())
    all_known_domains.update(d.lower() for d in aliases.keys())

    # Score: how many known domains appear in the RFP text?
    exact_matches = 0
    related_matches = 0

    for domain in cap_domains:
        if domain and domain in rfp_text:
            exact_matches += 1

    for alias, canonical in aliases.items():
        if alias.lower() in rfp_text or canonical.lower() in rfp_text:
            related_matches += 1

    if exact_matches >= 2:
        return 1.0
    elif exact_matches >= 1:
        return 0.85
    elif related_matches >= 1:
        return 0.6
    else:
        return 0.2


# ---------------------------------------------------------------------------
# 3. Budget Alignment (20%)
# ---------------------------------------------------------------------------

def _score_budget_alignment(
    requirements: list[dict],
    compliance_items: list[dict],
    capabilities: list[dict],
) -> float:
    """
    Compare RFP budget (from requirements with budget_ref) against
    average contract_value of matched capabilities.
    - Within 20% → 1.0
    - Within 50% → 0.6
    - Outside    → 0.3
    - No data    → 0.5 (neutral)
    """
    # Extract RFP budget from requirements
    rfp_budget = None
    for req in requirements:
        budget_ref = req.get("budget_ref")
        if budget_ref:
            parsed = _parse_budget(budget_ref)
            if parsed and parsed > 0:
                rfp_budget = parsed
                break

    if rfp_budget is None:
        logger.info("  Budget: no RFP budget found → neutral 0.5")
        return 0.5

    # Get matched capability IDs from compliance items
    matched_cap_ids = set()
    for ci in compliance_items:
        cap_id = ci.get("matched_capability_id")
        if cap_id:
            matched_cap_ids.add(cap_id)

    # Calculate average contract value of matched capabilities
    matched_values: list[float] = []
    for cap in capabilities:
        cap_id = cap.get("id", "")
        if cap_id in matched_cap_ids or not matched_cap_ids:
            val = _parse_budget(cap.get("contract_value"))
            if val and val > 0:
                matched_values.append(val)

    if not matched_values:
        logger.info("  Budget: no matched capability values → neutral 0.5")
        return 0.5

    avg_value = sum(matched_values) / len(matched_values)

    # Compare
    ratio = rfp_budget / avg_value if avg_value > 0 else 0
    deviation = abs(1.0 - ratio)

    if deviation <= 0.20:
        score = 1.0
    elif deviation <= 0.50:
        score = 0.6
    else:
        score = 0.3

    logger.info(
        "  Budget: RFP=%.0f  avg_cap=%.0f  ratio=%.2f  deviation=%.2f  score=%.1f",
        rfp_budget, avg_value, ratio, deviation, score,
    )
    return score


# ---------------------------------------------------------------------------
# 4. Past Win Rate (15%)
# ---------------------------------------------------------------------------

def _resolve_sector(workspace: dict, requirements: list[dict]) -> str | None:
    """
    The RFP sector is inferred during /parse and stored on the workspace.
    If absent (older workspaces, missing column), fall back to keyword
    inference over the requirement texts.
    """
    sector = (workspace or {}).get("sector")
    if sector:
        return sector

    rfp_text = " ".join(r.get("requirement", "") for r in requirements)
    return infer_sector_from_text(rfp_text)


def _score_past_win_rate(sector: str | None) -> float:
    """
    Historical win rate for the RFP's sector from the 120-row bid history
    dataset (falls back to the overall win rate for unseen sectors).
    """
    rate = get_sector_win_rate(sector)
    logger.info("  Past win rate: sector='%s'  rate=%.3f", sector or "unknown", rate)
    return round(rate, 4)


def _taxonomy_coverage(requirements: list[dict]) -> str:
    """'8/10' — evaluation criteria mapped to the taxonomy (Fix 8)."""
    evaluation = [r for r in requirements if r.get("type") == "evaluation_criteria"]
    mapped = sum(1 for r in evaluation if r.get("taxonomy_id"))
    return f"{mapped}/{len(evaluation)}"


def _extract_rfp_budget_millions(requirements: list[dict]) -> float | None:
    """First parseable budget_ref across requirements, in numeric millions."""
    for req in requirements:
        budget_m = parse_budget_millions(req.get("budget_ref"))
        if budget_m and budget_m > 0:
            return budget_m
    return None


# ---------------------------------------------------------------------------
# 5. Capability Depth (10%)
# ---------------------------------------------------------------------------

def _score_capability_depth(compliance_items: list[dict]) -> float:
    """
    Average confidence score across all compliance items.
    """
    if not compliance_items:
        return 0.0

    confidences = []
    for ci in compliance_items:
        conf = ci.get("confidence", 0.0)
        try:
            conf = float(conf)
        except (ValueError, TypeError):
            conf = 0.0
        confidences.append(max(0.0, min(1.0, conf)))

    avg = sum(confidences) / len(confidences)
    return round(avg, 4)


# ---------------------------------------------------------------------------
# Gap extraction — top compliance failures for CONDITIONAL recommendation
# ---------------------------------------------------------------------------

def _extract_gaps(
    compliance_items: list[dict],
    requirements: list[dict],
    max_gaps: int = 5,
) -> list[dict[str, Any]]:
    """
    Return the top `max_gaps` compliance items that are fail or partial,
    ordered by lowest confidence first, enriched with requirement text.
    """
    # Build requirement lookup
    req_map = {r.get("id", ""): r for r in requirements}

    gaps: list[dict] = []
    for ci in compliance_items:
        if ci.get("status") in ("fail", "partial"):
            req_id = ci.get("requirement_id", "")
            req = req_map.get(req_id, {})
            gaps.append({
                "requirement_id" : req_id,
                "section_ref"    : req.get("section_ref", "N/A"),
                "requirement"    : req.get("requirement", "N/A"),
                "status"         : ci.get("status"),
                "confidence"     : ci.get("confidence", 0.0),
                "gap_note"       : ci.get("gap_note", ""),
            })

    # Sort: fail before partial, then lowest confidence first
    status_order = {"fail": 0, "partial": 1}
    gaps.sort(key=lambda g: (status_order.get(g["status"], 2), g["confidence"]))

    return gaps[:max_gaps]


# ---------------------------------------------------------------------------
# Recommendation text generator
# ---------------------------------------------------------------------------

def _build_recommendation(
    decision: str,
    overall_score: float,
    breakdown: dict[str, float],
    gaps: list[dict],
) -> str:
    """
    Generate a human-readable recommendation string.
    """
    pct = overall_score * 100

    if decision == "GO":
        text = (
            f"RECOMMENDED: GO — Win probability is {pct:.0f}%. "
            f"The company demonstrates strong alignment across all scoring criteria. "
            f"Compliance rate ({breakdown['compliance_rate']*100:.0f}%) and domain match "
            f"({breakdown['domain_match']*100:.0f}%) are both strong. "
            f"Proceed with full proposal development and allocate senior bid team."
        )
    elif decision == "CONDITIONAL":
        gap_summary = "; ".join(
            f"[{g['section_ref']}] {g['gap_note'] or g['requirement'][:60]}"
            for g in gaps[:3]
        ) or "See compliance report for details."

        text = (
            f"CONDITIONAL: Win probability is {pct:.0f}%. "
            f"The bid is viable but has notable gaps that must be addressed before submission. "
            f"Top gaps: {gap_summary}. "
            f"Recommendation: proceed only if gaps can be mitigated through partnerships, "
            f"additional certifications, or capability enhancement."
        )
    else:
        weakest = min(breakdown, key=breakdown.get)  # type: ignore
        text = (
            f"NOT RECOMMENDED: NO-GO — Win probability is only {pct:.0f}%. "
            f"Weakest area: {weakest.replace('_', ' ')} ({breakdown[weakest]*100:.0f}%). "
            f"The company lacks sufficient evidence of capability and historical success "
            f"in this domain. Consider partnering with a domain expert or declining this bid "
            f"to focus resources on higher-probability opportunities."
        )

    return text


# ---------------------------------------------------------------------------
# Public API — compute full score
# ---------------------------------------------------------------------------

async def compute_bid_score(workspace_id: str) -> dict[str, Any]:
    """
    Run the 5-criteria scoring engine and return a complete score result.

    Steps
    -----
    1. Load requirements + compliance_items from Supabase
    2. Load capabilities from the in-memory FAISS store
    3. Compute each criterion score
    4. Weighted sum → overall_score
    5. Decision: GO / CONDITIONAL / NO-GO
    6. Extract gaps for CONDITIONAL recommendations
    7. Save to Supabase bid_scores table
    8. Return full result dict

    Returns
    -------
    dict with keys:
        id, workspace_id, overall_score, overall_score_pct, decision,
        breakdown, gaps, recommendation, score_breakdown (jsonb for DB)
    """
    sb = get_supabase()

    # ── 1. Load requirements ─────────────────────────────────────────────────
    try:
        req_result = (
            sb.table("requirements")
            .select("*")
            .eq("workspace_id", workspace_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error loading requirements: {exc}",
        )

    requirements = req_result.data or []
    if not requirements:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No requirements found. Run /parse and /match first.",
        )

    # ── 2. Load compliance items ─────────────────────────────────────────────
    try:
        comp_result = (
            sb.table("compliance_items")
            .select("*")
            .eq("workspace_id", workspace_id)
            .execute()
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error loading compliance items: {exc}",
        )

    compliance_items = comp_result.data or []
    if not compliance_items:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No compliance items found. Run /match first.",
        )

    # ── 3. Load capabilities + workspace (for the inferred sector) ──────────
    capabilities = get_all_capabilities()

    workspace: dict = {}
    try:
        ws_result = (
            sb.table("workspaces").select("*").eq("id", workspace_id).single().execute()
        )
        workspace = ws_result.data or {}
    except Exception as exc:
        logger.warning("Could not load workspace row for sector lookup: %s", exc)

    # ── 4. Compute each criterion ────────────────────────────────────────────
    logger.info("Computing bid score for workspace=%s …", workspace_id)

    sector = _resolve_sector(workspace, requirements)
    sector_win_rate = get_sector_win_rate(sector)

    cr = _score_compliance_rate(compliance_items)
    dm = _score_domain_match(requirements, capabilities)
    ba = _score_budget_alignment(requirements, compliance_items, capabilities)
    pwr = _score_past_win_rate(sector)
    cd = _score_capability_depth(compliance_items)

    # Competitor presence (Fix 7) — manual input stored on the workspace
    competitor_level = str(
        (workspace or {}).get("competitor_presence") or "unknown"
    ).strip().lower()
    if competitor_level not in COMPETITOR_SCORES:
        competitor_level = "unknown"
    cp = COMPETITOR_SCORES[competitor_level]
    logger.info("  Competitor presence: '%s' → %.1f", competitor_level, cp)

    breakdown = {
        "compliance_rate"    : round(cr, 4),
        "domain_match"       : round(dm, 4),
        "budget_alignment"   : round(ba, 4),
        "past_win_rate"      : round(pwr, 4),
        "capability_depth"   : round(cd, 4),
        "competitor_presence": round(cp, 4),
    }

    logger.info("  Breakdown: %s", breakdown)

    # ── 5. Heuristic weighted sum ────────────────────────────────────────────
    heuristic_score = sum(breakdown[k] * WEIGHTS[k] for k in WEIGHTS)
    heuristic_score = round(min(1.0, max(0.0, heuristic_score)), 4)

    # ── 5b. Trained model probability, blended with heuristics ──────────────
    gaps_found = sum(1 for c in compliance_items if c.get("status") in ("fail", "partial"))
    model_probability = None
    if is_trained():
        model_probability = predict_win_probability({
            "score_pct"      : heuristic_score * 100,
            "compliance_pct" : cr * 100,
            "gaps_found"     : gaps_found,
            "budget_m"       : _extract_rfp_budget_millions(requirements),
            "sector_win_rate": sector_win_rate,
        })

    if model_probability is not None:
        overall = MODEL_BLEND_WEIGHT * model_probability + HEURISTIC_BLEND_WEIGHT * heuristic_score
        logger.info(
            "  Model probability: %.4f  (blend %.0f%% model / %.0f%% heuristic)",
            model_probability, MODEL_BLEND_WEIGHT * 100, HEURISTIC_BLEND_WEIGHT * 100,
        )
    else:
        overall = heuristic_score
        logger.info("  Win model unavailable — using pure heuristic score.")

    overall = round(min(1.0, max(0.0, overall)), 4)
    overall_pct = round(overall * 100, 1)

    logger.info("  Overall score: %.4f  (%.1f%%)", overall, overall_pct)

    # ── 6. Decision ──────────────────────────────────────────────────────────
    if overall >= GO_THRESHOLD:
        decision = "GO"
    elif overall >= CONDITIONAL_THRESHOLD:
        decision = "CONDITIONAL"
    else:
        decision = "NO-GO"

    logger.info("  Decision: %s", decision)

    # ── 7. Gaps ──────────────────────────────────────────────────────────────
    gaps = _extract_gaps(compliance_items, requirements, max_gaps=5)

    # ── 8. Recommendation ────────────────────────────────────────────────────
    recommendation = _build_recommendation(decision, overall, breakdown, gaps)

    # ── 9. Build result ──────────────────────────────────────────────────────
    score_id = str(uuid.uuid4())

    score_record = {
        "id"               : score_id,
        "workspace_id"     : workspace_id,
        "budget_alignment" : ba,
        "domain_match"     : dm,
        "compliance_rate"  : cr,
        "past_win_rate"    : pwr,
        "capability_depth" : cd,
        "overall_score"    : overall,
        "decision"         : decision,
        "score_breakdown"  : {
            "weights"          : WEIGHTS,
            "raw_scores"       : breakdown,
            "overall"          : overall,
            "overall_pct"      : overall_pct,
            "decision"         : decision,
            "gap_count"        : len(gaps),
            "competitor_presence_level": competitor_level,
            "gaps"             : (
                ["Competitor intelligence not provided — score assumes neutral field"]
                if competitor_level == "unknown" else []
            ),
            "evaluation_criteria_mapped": _taxonomy_coverage(requirements),
            # Win-model fields (Fix 1) — additive, frontend-safe
            "model_probability": model_probability,
            "heuristic_score"  : heuristic_score,
            "model_insights"   : get_model_insights() if model_probability is not None else None,
            "sector"           : sector,
            "sector_win_rate"  : sector_win_rate,
            "blend_weights"    : {
                "model"    : MODEL_BLEND_WEIGHT,
                "heuristic": HEURISTIC_BLEND_WEIGHT,
            } if model_probability is not None else None,
        },
    }

    # ── 10. Save to Supabase ─────────────────────────────────────────────────
    # Canonical table name is bid_scores (Fix 12). Falls back to the legacy
    # win_scores name until the rename migration has been run.
    try:
        sb.table("bid_scores").insert(score_record).execute()
        logger.info("Saved bid score to Supabase: id=%s", score_id)
    except Exception as exc:
        # The original win_scores table has a different shape
        # (score/decision/reasons) — write that shape so saving still works
        # until the rename+column migration in schema.sql has been run.
        legacy_record = {
            "id"          : score_id,
            "workspace_id": workspace_id,
            "score"       : overall,
            "decision"    : decision,
            "reasons"     : score_record["score_breakdown"],
        }
        try:
            sb.table("win_scores").upsert(
                legacy_record, on_conflict="workspace_id"
            ).execute()
            logger.warning(
                "Saved score to LEGACY win_scores table (reduced shape) — run "
                "the bid_scores migration in schema.sql."
            )
        except Exception as exc2:
            logger.error("Supabase insert(bid_scores) failed: %s / legacy: %s", exc, exc2)
            # Don't raise — still return the score to the user

    # ── 11. Return full response ─────────────────────────────────────────────
    return {
        "id"               : score_id,
        "workspace_id"     : workspace_id,
        "overall_score"    : overall,
        "overall_score_pct": overall_pct,
        "decision"         : decision,
        "breakdown"        : breakdown,
        "weights"          : WEIGHTS,
        "gaps"             : gaps,
        "recommendation"   : recommendation,
        "score_breakdown"  : score_record["score_breakdown"],
        "stats"            : {
            "total_requirements"   : len(requirements),
            "total_compliance"     : len(compliance_items),
            "pass_count"           : sum(1 for c in compliance_items if c.get("status") == "pass"),
            "fail_count"           : sum(1 for c in compliance_items if c.get("status") == "fail"),
            "partial_count"        : sum(1 for c in compliance_items if c.get("status") == "partial"),
        },
    }
