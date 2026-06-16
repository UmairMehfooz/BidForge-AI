"""
BidForge AI — Compliance Matcher (batched, Fix 6)
==================================================
For every requirement extracted from an RFP, this module:
  1. Runs hybrid retrieval (dense top-10 → structured re-rank → top-3,
     see capability_store.search_capabilities_hybrid — Fix 3)
  2. Groups requirements into batches of 6 and sends ONE GROQ call per batch
     (llama-3.3-70b-versatile) to judge pass / fail / partial for all of them
  3. Returns compliance_item dicts whose confidence is
     clamp(hybrid_score + LLM confidence_adjustment, 0, 1)

Batching (Fix 6) replaces the old one-call-per-requirement design: 40+
requirements used to mean 40+ sequential GROQ calls (60-120 s and certain
rate-limiting); now it's ~7 batch calls, up to 3 in flight at once.

Public API (unchanged)
----------------------
    result = await match_requirements(workspace_id, requirements, issuer_type)
    # → MatchResult(items, pass_count, fail_count, partial_count)

    summary = await run_compliance_match(workspace_id)
    # → dict suitable for MatchResponse (loads from Supabase, saves back)

Each compliance_item dict:
    {
        "id"                    : str   fresh UUID
        "workspace_id"          : str
        "requirement_id"        : str   FK → requirements.id
        "status"                : "pass" | "fail" | "partial"
        "confidence"            : float  0.0–1.0  (hybrid ± adjustment)
        "matched_capability_id" : str | None   best-matching CAP id
        "gap_note"              : str | None    explanation for fail/partial
    }

Failure handling
----------------
- Unparseable batch response → ONE retry with "Return ONLY valid JSON"
  appended; second failure marks that batch's items status="partial",
  gap_note="auto-review needed" — the pipeline never crashes on one batch.
- 429s → exponential backoff (2s / 4s / 8s) per batch.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.services.capability_store import search_capabilities_hybrid
from app.utils.llm_cache import cached_invoke, demo_fallback
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GROQ_MODEL  = "llama-3.3-70b-versatile"
TEMPERATURE = 0.0
MAX_TOKENS  = 2048         # one batch returns up to 6 verdict objects

VALID_STATUSES = {"pass", "fail", "partial"}

BATCH_SIZE             = 6     # requirements per GROQ call
MAX_CONCURRENT_BATCHES = 3     # batches in flight at once
RETRY_DELAYS           = (2, 4, 8)   # 429 backoff per batch
MAX_ADJUSTMENT         = 0.1   # LLM may nudge confidence by at most ±0.1


# ---------------------------------------------------------------------------
# System Prompt — batched verdicts
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a compliance checker AI for a bid management system at a Pakistani IT services company.

You will be given SEVERAL bid requirements extracted from an RFP. For each requirement you also get its top-3 matching COMPANY CAPABILITIES retrieved from the capability library, including retrieval evidence (semantic similarity, domain match, certification match, recency, client-type match, and a combined hybrid score).

For EACH requirement, decide whether the company can meet it based ONLY on its listed capabilities.

Return ONLY a valid JSON array with EXACTLY one object per requirement, in the same order — no explanation, no markdown fences, no extra text:
[
  {
    "requirement_id"        : "<the id given for the requirement>",
    "status"                : "pass" | "fail" | "partial",
    "matched_capability_id" : "<id of the best-matching capability, or null>",
    "confidence_adjustment" : <float between -0.1 and 0.1>,
    "gap_note"              : "<specific missing evidence, or null>"
  }
]

Definitions:
  "pass"    — Clear, direct evidence the company fully meets this requirement.
  "partial" — Related experience exists but doesn't fully cover the requirement (different scale, domain, or missing certification).
  "fail"    — No relevant capability exists.

Rules:
  1. Base decisions ONLY on the listed capabilities. Never invent capabilities.
  2. confidence_adjustment fine-tunes the retrieval hybrid score: positive when the summary contains stronger evidence than the scores suggest, negative when weaker. Stay within -0.1..0.1.
  3. gap_note is REQUIRED whenever status is not "pass". It must name the SPECIFIC missing evidence (e.g. "No CMMI L3 certified project in the Healthcare domain after 2022"), never a generic phrase. Null only when status is "pass".
  4. matched_capability_id must be one of that requirement's listed capability ids, or null.
  5. The output array must contain exactly one object for every requirement_id given, in the same order."""


# ---------------------------------------------------------------------------
# LLM client (lazy singleton)
# ---------------------------------------------------------------------------
_llm: ChatGroq | None = None


def _get_llm() -> ChatGroq:
    global _llm
    if _llm is None:
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GROQ_API_KEY is not set in your .env file.",
            )
        _llm = ChatGroq(
            model=GROQ_MODEL,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            api_key=api_key,
        )
        logger.info("ChatGroq client initialised for compliance matching (model=%s)", GROQ_MODEL)
    return _llm


# ---------------------------------------------------------------------------
# JSON parsing — strict array, defensive fallbacks
# ---------------------------------------------------------------------------

def _parse_verdict_array(raw: str) -> list[dict[str, Any]] | None:
    """
    Parse GROQ's batched verdict response into a list of dicts.
    Returns None when nothing parseable is found (caller retries / degrades).
    """
    if not raw or not raw.strip():
        return None

    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text, flags=re.IGNORECASE).strip()

    for candidate in (text, ):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, list):
                return [o for o in obj if isinstance(o, dict)]
            if isinstance(obj, dict):
                return [obj]
        except json.JSONDecodeError:
            pass

    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group())
            if isinstance(obj, list):
                logger.warning("Batch verdict JSON extracted via regex.")
                return [o for o in obj if isinstance(o, dict)]
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Prompt builder — one batch of requirements + their candidates
# ---------------------------------------------------------------------------

def _build_batch_prompt(batch: list[dict[str, Any]]) -> str:
    """
    `batch` items: {"requirement": <req dict>, "candidates": [<cap dicts>]}.
    """
    lines = [f"Evaluate the following {len(batch)} requirements.\n"]

    for n, item in enumerate(batch, start=1):
        req = item["requirement"]
        lines.append(
            f"=== REQUIREMENT {n} ===\n"
            f"requirement_id: {req.get('id')}\n"
            f"Type          : {req.get('type', 'mandatory')}\n"
            f"Text          : {req.get('requirement', '')}"
        )
        caps = item["candidates"]
        if not caps:
            lines.append("(No capabilities found in the library for this requirement.)")
        for cap in caps:
            sub = cap.get("_structured_sub", {})
            lines.append(
                f"  --- Capability {cap.get('id', '?')} ---\n"
                f"  Domain       : {cap.get('domain', 'N/A')}\n"
                f"  Summary      : {cap.get('summary', 'N/A')}\n"
                f"  Certification: {cap.get('certification', 'None')}\n"
                f"  Client Type  : {cap.get('client_type', 'N/A')}\n"
                f"  Year         : {cap.get('year_completed', 'N/A')}\n"
                f"  Evidence     : hybrid={cap.get('_hybrid', 0.0):.3f} "
                f"(semantic={cap.get('_dense', 0.0):.3f}, "
                f"domain_match={sub.get('domain_match', 0.0):.2f}, "
                f"certification_match={sub.get('certification_match', 0.0):.2f}, "
                f"recency={sub.get('recency', 0.0):.2f}, "
                f"client_type_match={sub.get('client_type_match', 0.0):.2f})"
            )
        lines.append("")

    lines.append(
        "Return the JSON array now — one object per requirement, same order, "
        "grounding every gap_note in the specific missing evidence above."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Verdict normalisation
# ---------------------------------------------------------------------------

def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _normalise_item(
    verdict: dict[str, Any] | None,
    requirement: dict[str, Any],
    candidates: list[dict[str, Any]],
    workspace_id: str,
) -> dict[str, Any]:
    """
    Build one compliance_item from an LLM verdict (possibly None → degraded
    "partial / auto-review needed") plus the requirement's retrieval evidence.

    Final confidence = clamp(hybrid_score + confidence_adjustment, 0, 1).
    """
    hybrid_by_id = {c.get("id"): float(c.get("_hybrid", 0.0)) for c in candidates}
    top_id = candidates[0].get("id") if candidates else None
    top_hybrid = hybrid_by_id.get(top_id, 0.0)

    if verdict is None:
        return {
            "id"                    : str(uuid.uuid4()),
            "workspace_id"          : workspace_id,
            "requirement_id"        : requirement.get("id"),
            "status"                : "partial",
            "confidence"            : round(_clamp(top_hybrid, 0.0, 1.0), 4),
            "matched_capability_id" : top_id,
            "gap_note"              : "auto-review needed",
        }

    raw_status = str(verdict.get("status") or "partial").strip().lower()
    status_val = raw_status if raw_status in VALID_STATUSES else "partial"

    # matched_capability_id must belong to this requirement's candidates
    cap_id = verdict.get("matched_capability_id")
    cap_id = str(cap_id).strip() if cap_id else None
    if cap_id and cap_id.lower() in ("null", "none", ""):
        cap_id = None
    if cap_id is not None and cap_id not in hybrid_by_id:
        logger.debug("LLM matched unknown capability '%s' — using top candidate.", cap_id)
        cap_id = top_id

    # confidence_adjustment — clamp to ±0.1, tolerate junk
    try:
        adjustment = float(verdict.get("confidence_adjustment", 0.0))
    except (TypeError, ValueError):
        adjustment = 0.0
    adjustment = _clamp(adjustment, -MAX_ADJUSTMENT, MAX_ADJUSTMENT)

    base_hybrid = hybrid_by_id.get(cap_id, top_hybrid)
    confidence = round(_clamp(base_hybrid + adjustment, 0.0, 1.0), 4)

    gap_note = verdict.get("gap_note")
    if not gap_note or str(gap_note).strip().lower() in ("null", "none", ""):
        gap_note = None
    else:
        gap_note = str(gap_note).strip()
    if status_val != "pass" and not gap_note:
        gap_note = "Gap not specified by reviewer — manual check recommended."

    return {
        "id"                    : str(uuid.uuid4()),
        "workspace_id"          : workspace_id,
        "requirement_id"        : requirement.get("id"),
        "status"                : status_val,
        "confidence"            : confidence,
        "matched_capability_id" : cap_id if status_val != "fail" or cap_id else None,
        "gap_note"              : gap_note,
    }


# ---------------------------------------------------------------------------
# One batch: GROQ call with 429 backoff + one strict-JSON retry
# ---------------------------------------------------------------------------

def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


async def _invoke_with_backoff(messages: list) -> str | None:
    """
    One cache-first LLM call (Fix 10) with 2s/4s/8s backoff on 429s.
    After retries: DEMO_MODE serves the nearest cached batch verdict for this
    tag; otherwise None (the batch degrades to partial/auto-review).
    """
    llm = _get_llm()
    last_exc: Exception | None = None
    for attempt, delay in enumerate((0,) + RETRY_DELAYS):
        if delay:
            logger.warning("Batch GROQ retry in %ds: %s", delay, last_exc)
            await asyncio.sleep(delay)
        try:
            return await cached_invoke("compliance_match", llm, messages)
        except Exception as exc:
            last_exc = exc
            if not _is_rate_limit(exc) and attempt >= 1:
                break

    fallback = demo_fallback("compliance_match", str(messages[-1].content))
    if fallback is not None:
        return fallback

    logger.error("Batch GROQ call failed after retries: %s", last_exc)
    return None


async def _check_batch(
    batch: list[dict[str, Any]],
    workspace_id: str,
    batch_no: int,
    total_batches: int,
) -> list[dict[str, Any]]:
    """
    Judge one batch of ≤6 requirements in a single GROQ call.
    Always returns exactly one compliance_item per requirement.
    """
    started = time.perf_counter()
    prompt = _build_batch_prompt(batch)
    messages = [SystemMessage(content=SYSTEM_PROMPT), HumanMessage(content=prompt)]

    verdicts: list[dict[str, Any]] | None = None
    raw = await _invoke_with_backoff(messages)
    if raw is not None:
        verdicts = _parse_verdict_array(raw)
        if verdicts is None:
            # Retry ONCE with an explicit strict-JSON instruction
            logger.warning("Batch %d/%d: unparseable response — retrying with strict-JSON nudge.",
                           batch_no, total_batches)
            retry_messages = messages + [HumanMessage(content="Return ONLY valid JSON.")]
            raw_retry = await _invoke_with_backoff(retry_messages)
            if raw_retry is not None:
                verdicts = _parse_verdict_array(raw_retry)

    # Map verdicts by requirement_id (fall back to positional order)
    by_id: dict[str, dict] = {}
    if verdicts:
        for pos, verdict in enumerate(verdicts):
            rid = str(verdict.get("requirement_id") or "").strip()
            if rid:
                by_id[rid] = verdict
            elif pos < len(batch):
                by_id[str(batch[pos]["requirement"].get("id"))] = verdict

    items = []
    for item in batch:
        rid = str(item["requirement"].get("id"))
        items.append(_normalise_item(by_id.get(rid), item["requirement"], item["candidates"], workspace_id))

    degraded = sum(1 for i in items if i["gap_note"] == "auto-review needed")
    logger.info(
        "Batch %d/%d done in %.1fs (%d items%s)",
        batch_no, total_batches, time.perf_counter() - started, len(items),
        f", {degraded} degraded to auto-review" if degraded else "",
    )
    return items


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class MatchResult:
    items        : list[dict[str, Any]] = field(default_factory=list)
    pass_count   : int = 0
    fail_count   : int = 0
    partial_count: int = 0

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def compliance_rate(self) -> float:
        if self.total == 0:
            return 0.0
        # partial counts as 0.5 toward compliance
        score = self.pass_count + (self.partial_count * 0.5)
        return round(score / self.total, 4)

    def to_summary_dict(self, workspace_id: str) -> dict[str, Any]:
        return {
            "workspace_id"   : workspace_id,
            "total"          : self.total,
            "pass_count"     : self.pass_count,
            "fail_count"     : self.fail_count,
            "partial_count"  : self.partial_count,
            "compliance_rate": self.compliance_rate,
        }


# ---------------------------------------------------------------------------
# Public API — match only (no DB write)
# ---------------------------------------------------------------------------

async def match_requirements(
    workspace_id : str,
    requirements : list[dict[str, Any]],
    issuer_type  : str | None = None,
) -> MatchResult:
    """
    Hybrid retrieval + batched GROQ compliance check for every requirement.

    Parameters
    ----------
    workspace_id : str            UUID of the workspace
    requirements : list[dict]     Requirement records from Supabase (must have
                                  "id" and "requirement" keys at minimum)
    issuer_type  : str | None     'govt'/'private' — feeds structured scoring

    Returns
    -------
    MatchResult  dataclass with items list and counts
    """
    if not requirements:
        logger.warning("match_requirements called with empty list for workspace=%s", workspace_id)
        return MatchResult()

    started = time.perf_counter()

    valid = [r for r in requirements if r.get("requirement", "").strip()]
    skipped = len(requirements) - len(valid)
    if skipped:
        logger.warning("Skipping %d empty requirements.", skipped)

    result = MatchResult()
    if not valid:
        return result

    # ── 1. Hybrid retrieval per requirement (local FAISS — fast) ────────────
    work_items = []
    for req in valid:
        try:
            candidates = search_capabilities_hybrid(
                req["requirement"], top_k=3, issuer_type=issuer_type
            )
        except Exception as exc:
            logger.warning("Hybrid search failed for req '%s…': %s",
                           req["requirement"][:60], exc)
            candidates = []
        work_items.append({"requirement": req, "candidates": candidates})

    # ── 2. Batch into groups of 6, judge up to 3 batches concurrently ───────
    batches = [work_items[i:i + BATCH_SIZE] for i in range(0, len(work_items), BATCH_SIZE)]
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_BATCHES)

    async def _run_batch(i: int, batch: list) -> list[dict[str, Any]]:
        async with semaphore:
            return await _check_batch(batch, workspace_id, i, len(batches))

    batch_results = await asyncio.gather(
        *(_run_batch(i, b) for i, b in enumerate(batches, start=1))
    )
    for items in batch_results:
        result.items.extend(items)

    for item in result.items:
        s = item["status"]
        if s == "pass":
            result.pass_count += 1
        elif s == "fail":
            result.fail_count += 1
        else:
            result.partial_count += 1

    elapsed = time.perf_counter() - started
    logger.info(
        "Matching complete: workspace=%s  %d requirements in %d batches → %.1fs "
        "(pass=%d fail=%d partial=%d rate=%.1f%%)",
        workspace_id, len(valid), len(batches), elapsed,
        result.pass_count, result.fail_count, result.partial_count,
        result.compliance_rate * 100,
    )
    return result


# ---------------------------------------------------------------------------
# Public API — load requirements + match + save to Supabase
# ---------------------------------------------------------------------------

async def run_compliance_match(workspace_id: str) -> dict[str, Any]:
    """
    Full compliance matching pipeline:
      1. Load requirements (and the workspace's issuer_type) from Supabase
      2. Run hybrid retrieval + batched GROQ matching
      3. Batch-insert compliance_items into Supabase
      4. Return summary dict

    Raises
    ------
    HTTPException 422  If no requirements found (workspace not yet parsed)
    HTTPException 503  If Supabase or GROQ is unavailable
    HTTPException 500  If DB insert fails
    """
    sb = get_supabase()

    # ── 1. Load requirements from Supabase ───────────────────────────────────
    try:
        req_result = (
            sb.table("requirements")
            .select("id, requirement, type, section_ref")
            .eq("workspace_id", workspace_id)
            .execute()
        )
    except Exception as exc:
        logger.error("Supabase load requirements failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error loading requirements: {exc}",
        )

    requirements = req_result.data or []
    if not requirements:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"No requirements found for workspace '{workspace_id}'. "
                "Run POST /api/workspaces/{id}/parse first."
            ),
        )

    logger.info("Loaded %d requirements from Supabase for workspace=%s", len(requirements), workspace_id)

    # ── 1b. Load the workspace's issuer type (inferred during /parse) ────────
    issuer_type: str | None = None
    try:
        ws_result = (
            sb.table("workspaces").select("issuer_type").eq("id", workspace_id)
            .single().execute()
        )
        issuer_type = (ws_result.data or {}).get("issuer_type")
    except Exception as exc:
        logger.warning("Could not load issuer_type for workspace %s: %s", workspace_id, exc)

    # ── 2. Run matching ───────────────────────────────────────────────────────
    match_result = await match_requirements(workspace_id, requirements, issuer_type=issuer_type)

    if not match_result.items:
        logger.warning("No compliance items produced for workspace=%s", workspace_id)
        return match_result.to_summary_dict(workspace_id)

    # ── 3. Batch insert compliance_items into Supabase ────────────────────────
    # Re-matching REPLACES previous results (idempotent pipeline re-runs)
    try:
        sb.table("compliance_items").delete().eq("workspace_id", workspace_id).execute()
    except Exception as exc:
        logger.warning("Could not clear previous compliance items: %s", exc)

    try:
        insert_result = (
            sb.table("compliance_items")
            .insert(match_result.items)
            .execute()
        )
    except Exception as exc:
        logger.error("Supabase insert(compliance_items) failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error while saving compliance items: {exc}",
        )

    if insert_result.data is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supabase insert returned None. Check RLS policies on `compliance_items` table.",
        )

    inserted = len(insert_result.data)
    logger.info("Saved %d compliance items to Supabase for workspace=%s", inserted, workspace_id)

    return match_result.to_summary_dict(workspace_id)
