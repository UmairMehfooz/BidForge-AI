"""
BidForge AI — Proposal Generator (Streaming)
=============================================
For every draftable requirement (type = "mandatory" or "question"), this
module:
  1. Searches FAISS for top-3 matching capabilities
  2. Builds a bid-writer prompt with the requirement + evidence
  3. Streams the GROQ response token-by-token as SSE events
  4. After all sections are streamed, saves them to Supabase

Public API
----------
    async for event in stream_proposal_sections(workspace_id):
        yield event   # SSE-formatted string

    sections = await generate_and_save_proposal(workspace_id)
    # → list[dict]  — saved proposal_section records

SSE Event Format
-----------------
  In-progress token:
    data: {"section": "4.3.1", "title": "...", "token": "word ", "done": false}

  Section complete:
    data: {"section": "4.3.1", "title": "...", "token": "", "done": true}

  Stream finished:
    data: [DONE]

Design decisions
----------------
- Uses ChatGroq with `streaming=True` and iterates over `astream()` chunks.
- Each requirement gets its own section; the full text is buffered in memory
  alongside the SSE events so we can batch-insert to Supabase after the
  stream finishes.
- Only requirements with type in DRAFTABLE_TYPES are processed. Deadlines,
  budgets, and evaluation criteria are skipped (they feed into scoring, not
  narrative drafting).
- The generator is async so FastAPI's StreamingResponse can yield it
  directly — zero buffering.
"""

from __future__ import annotations

import difflib
import json
import logging
import os
import re
import uuid
from typing import Any, AsyncGenerator

from fastapi import HTTPException, status
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_groq import ChatGroq

from app.services.capability_store import search_capabilities
from app.utils.llm_cache import cached_invoke, cached_stream
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GROQ_MODEL       = "llama-3.3-70b-versatile"
TEMPERATURE      = 0.7         # slight creativity for proposal writing
MAX_TOKENS       = 2048        # 2-3 paragraphs per section
TOP_K_CAPS       = 3           # FAISS results per requirement
DRAFTABLE_TYPES  = {"mandatory", "question"}

# ---------------------------------------------------------------------------
# System Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a professional bid writer for a leading Pakistani IT services and engineering company.

Your task is to write a compelling, polished proposal response to a specific bid requirement.

Guidelines:
1. Write 2-3 focused paragraphs that directly address the requirement.
2. Use ONLY the provided company capabilities as evidence — do NOT fabricate projects, certifications, or experience.
3. Cite the specific project name and relevant details (e.g. "As demonstrated in our National Fiber Optic Backbone project...").
4. Maintain a confident yet professional tone appropriate for government and international tenders.
5. Highlight quantitative achievements where available (e.g. "reducing downtime by 31%", "serving 50,000 students").
6. If the provided capabilities only partially match, acknowledge the gap briefly and emphasise transferable skills.
7. Do NOT include section numbers, headers, or formatting — output plain narrative text only.
8. Do NOT repeat the requirement text — jump straight into the response."""


# ---------------------------------------------------------------------------
# LLM client (lazy singleton, separate from matcher to allow different temp)
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
            streaming=True,        # ← enable streaming mode
        )
        logger.info("ChatGroq streaming client initialised (model=%s)", GROQ_MODEL)
    return _llm


# ---------------------------------------------------------------------------
# Section ordering + requirement dedup
# ---------------------------------------------------------------------------

def section_sort_key(ref: Any) -> tuple:
    """
    Natural sort key for section refs: numbers compare numerically
    ("2" < "10"), mixed refs like "6a-6d" tokenize, and "General"/empty
    sorts last. Shared with the DOCX exporter.
    """
    text = str(ref or "").strip()
    if not text or text.lower() == "general":
        return (1, [])
    tokens = [
        (0, int(token)) if token.isdigit() else (1, token.lower())
        for token in re.findall(r"\d+|[A-Za-z]+", text)
    ]
    return (0, tokens)


def _dedupe_draftables(requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop requirements whose text near-duplicates an earlier one (>0.85)."""
    unique: list[dict[str, Any]] = []
    for req in requirements:
        text = req.get("requirement", "").lower().strip()
        is_duplicate = False
        for kept in unique:
            matcher = difflib.SequenceMatcher(
                None, text, kept.get("requirement", "").lower().strip()
            )
            if matcher.real_quick_ratio() > 0.85 and matcher.ratio() > 0.85:
                is_duplicate = True
                break
        if not is_duplicate:
            unique.append(req)
    skipped = len(requirements) - len(unique)
    if skipped:
        logger.info("Skipping %d near-duplicate requirements before drafting.", skipped)
    return unique


# ---------------------------------------------------------------------------
# Build user prompt for one requirement
# ---------------------------------------------------------------------------

def _build_draft_prompt(requirement: dict[str, Any], capabilities: list[dict[str, Any]]) -> str:
    """
    Build the user-turn prompt for proposal drafting.
    """
    req_text    = requirement.get("requirement", "")
    section_ref = requirement.get("section_ref", "General")
    req_type    = requirement.get("type", "mandatory")

    lines = [
        f"REQUIREMENT (Section {section_ref}, Type: {req_type}):",
        req_text,
        "",
        "COMPANY CAPABILITIES (use these as evidence):",
    ]

    if not capabilities:
        lines.append("(No directly matching capabilities found — use general company strengths.)")
    else:
        for cap in capabilities:
            lines.append(
                f"\n--- {cap.get('id', '?')}: {cap.get('project_title', 'N/A')} ---"
                f"\n  Domain       : {cap.get('domain', 'N/A')}"
                f"\n  Summary      : {cap.get('summary', 'N/A')}"
                f"\n  Certification: {cap.get('certification') or 'None'}"
                f"\n  Client Type  : {cap.get('client_type', 'N/A')}"
                f"\n  Contract     : {cap.get('contract_value', 'N/A')}"
                f"\n  Year         : {cap.get('year_completed', 'N/A')}"
            )

    lines.append("\nWrite a compelling 2-3 paragraph proposal response to this requirement.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Format SSE event
# ---------------------------------------------------------------------------

def _sse_event(section_ref: str, title: str, token: str, done: bool) -> str:
    """
    Format a single SSE data line.
    """
    payload = json.dumps({
        "section": section_ref,
        "title":   title,
        "token":   token,
        "done":    done,
    }, ensure_ascii=False)
    return f"data: {payload}\n\n"


# ---------------------------------------------------------------------------
# Per-section persistence — sections are saved as soon as they finish so a
# stopped stream keeps everything already drafted (stop/resume support).
# ---------------------------------------------------------------------------

def _save_section(section_record: dict[str, Any]) -> bool:
    """
    Insert one proposal section. Falls back to the reduced shape when the
    table lacks the optional requirement_id / section_title columns.
    Returns True when the row landed in Supabase.
    """
    sb = get_supabase()
    try:
        sb.table("proposal_sections").insert(section_record).execute()
        return True
    except Exception as exc:
        optional_columns = ("requirement_id", "section_title")
        if any(col in str(exc) for col in optional_columns):
            stripped = {k: v for k, v in section_record.items() if k not in optional_columns}
            try:
                sb.table("proposal_sections").insert(stripped).execute()
                logger.warning(
                    "Saved section (reduced shape) — run the proposal_sections "
                    "migration in schema.sql."
                )
                return True
            except Exception as exc2:
                exc = exc2
        logger.error("Supabase insert(proposal_sections) failed: %s", exc)
        return False


def _load_existing_sections(workspace_id: str) -> list[dict[str, Any]]:
    sb = get_supabase()
    try:
        result = (
            sb.table("proposal_sections")
            .select("id, requirement_id, section_ref")
            .eq("workspace_id", workspace_id)
            .execute()
        )
        return result.data or []
    except Exception as exc:
        logger.warning("Could not load existing proposal sections: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Load draftable requirements from Supabase
# ---------------------------------------------------------------------------

async def _load_draftable_requirements(workspace_id: str) -> list[dict[str, Any]]:
    """
    Fetch requirements from Supabase that should be drafted into proposal
    sections (mandatory + question types).
    """
    sb = get_supabase()
    try:
        result = (
            sb.table("requirements")
            .select("id, requirement, type, section_ref")
            .eq("workspace_id", workspace_id)
            .in_("type", list(DRAFTABLE_TYPES))
            .order("section_ref")
            .execute()
        )
    except Exception as exc:
        logger.error("Supabase load requirements failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database error loading requirements: {exc}",
        )

    requirements = result.data or []
    if not requirements:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"No draftable requirements found for workspace '{workspace_id}'. "
                "Run POST /api/workspaces/{id}/parse first."
            ),
        )

    logger.info(
        "Loaded %d draftable requirements for workspace=%s",
        len(requirements), workspace_id,
    )
    return requirements


# ---------------------------------------------------------------------------
# Public API — Streaming SSE generator
# ---------------------------------------------------------------------------

async def stream_proposal_sections(
    workspace_id: str,
    restart: bool = False,
) -> AsyncGenerator[str, None]:
    """
    Async generator that yields SSE events for each proposal section.

    For each draftable requirement:
      1. FAISS top-3 search
      2. Stream GROQ response token-by-token as SSE events
      3. Save the section to Supabase as soon as it completes

    Stop/resume: sections are persisted one at a time, so an aborted stream
    keeps everything already drafted. The next call (restart=False) skips
    requirements that already have a saved section and continues from there.
    restart=True clears all sections and drafts from scratch.

    Yields
    ------
    str — SSE-formatted data lines, one per token or per section-complete signal.
    """
    requirements = await _load_draftable_requirements(workspace_id)

    # Near-duplicate requirements (left over from older parses) would each
    # produce an identical section — draft one per distinct requirement.
    requirements = _dedupe_draftables(requirements)

    # Draft in natural section order (1, 2, … 10; letters after; General last)
    # so the stream, the editor list, and the export all read top-to-bottom.
    requirements.sort(key=lambda r: section_sort_key(r.get("section_ref")))

    total_count = len(requirements)

    # ── Resume vs restart ────────────────────────────────────────────────────
    existing = [] if restart else _load_existing_sections(workspace_id)
    # Legacy rows without requirement_id can't be matched to a requirement —
    # resuming would duplicate them, so fall back to a clean restart.
    if existing and any(not s.get("requirement_id") for s in existing):
        logger.info("Existing sections lack requirement_id — restarting draft.")
        existing = []
        restart = True

    if restart:
        try:
            sb = get_supabase()
            sb.table("proposal_sections").delete().eq("workspace_id", workspace_id).execute()
        except Exception as exc:
            logger.warning("Could not clear previous proposal sections: %s", exc)

    drafted_req_ids = {s["requirement_id"] for s in existing}
    pending = [r for r in requirements if r.get("id") not in drafted_req_ids]

    logger.info(
        "Draft stream: %d total draftables, %d already saved, %d to draft (restart=%s)",
        total_count, len(drafted_req_ids), len(pending), restart,
    )

    # Mark the workspace as mid-draft so a stopped stream is recognisable
    # (the UI offers Resume whenever status is 'drafting').
    if pending:
        try:
            sb = get_supabase()
            sb.table("workspaces").update({"status": "drafting"}).eq("id", workspace_id).execute()
        except Exception as exc:
            logger.warning("Could not set workspace status to 'drafting': %s", exc)

    # Progress meta event so the UI can show "resuming at X of Y"
    yield "data: " + json.dumps({
        "meta"     : True,
        "total"    : total_count,
        "done"     : total_count - len(pending),
        "remaining": len(pending),
        "resumed"  : bool(drafted_req_ids),
    }, ensure_ascii=False) + "\n\n"

    llm = _get_llm()

    saved_count = 0

    for idx, req in enumerate(pending, start=len(drafted_req_ids) + 1):
        req_text    = req.get("requirement", "")
        section_ref = req.get("section_ref", "General")
        req_id      = req.get("id", "")

        # Build a readable title for the SSE event and DB record
        title = f"Section {section_ref}: {req_text[:80]}{'…' if len(req_text) > 80 else ''}"

        # SSE key must be UNIQUE per requirement — many requirements share a
        # section_ref (e.g. "General"), and the client keys streamed sections
        # by this value; identical keys made sections overwrite each other.
        stream_key = f"{idx}. {section_ref}"

        logger.info(
            "Drafting section %d/%d: [%s] '%s…'",
            idx, total_count, section_ref, req_text[:60],
        )

        # ── FAISS search ─────────────────────────────────────────────────────
        try:
            capabilities = search_capabilities(req_text, top_k=TOP_K_CAPS)
        except Exception as exc:
            logger.warning("FAISS search failed: %s", exc)
            capabilities = []

        # ── Build messages ───────────────────────────────────────────────────
        user_prompt = _build_draft_prompt(req, capabilities)
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        # ── Stream (cache-first, Fix 10): cached sections replay token-by-
        # token; live streams are cached on completion; in DEMO_MODE a dead
        # GROQ falls back to the nearest cached section instead of an error.
        full_text = ""
        try:
            async for token in cached_stream("draft_section", llm, messages):
                full_text += token
                yield _sse_event(stream_key, title, token, done=False)

        except Exception as exc:
            logger.error("GROQ streaming failed for section '%s': %s", section_ref, exc)
            error_msg = f"[Error generating this section: {exc}]"
            full_text = error_msg
            yield _sse_event(stream_key, title, error_msg, done=False)

        # ── Save THIS section immediately (stop/resume safety) ──────────────
        section_record = {
            "id"           : str(uuid.uuid4()),
            "workspace_id" : workspace_id,
            "requirement_id": req_id,
            "section_title": title,
            "section_ref"  : section_ref,
            "ai_draft"     : full_text.strip(),
            "edited_draft" : None,
            "status"       : "draft",
        }
        if _save_section(section_record):
            saved_count += 1
        else:
            # Don't raise — the section was already streamed to the client.
            yield _sse_event(
                "system", "Database Warning",
                "[Warning: section streamed but DB save failed]",
                done=True,
            )

        # ── Section complete signal (after the save, so a section the client
        # sees as done is guaranteed to survive a stop) ───────────────────────
        yield _sse_event(stream_key, title, "", done=True)

        logger.info(
            "  ✅ Section '%s' drafted and saved: %d chars",
            section_ref, len(full_text),
        )

    # ── All pending sections completed — mark workspace drafted ──────────────
    # (Only reached when the loop finishes; an aborted stream is cancelled
    # mid-loop, keeping the previous status so the UI can offer Resume.)
    try:
        sb = get_supabase()
        sb.table("workspaces").update({"status": "drafted"}).eq("id", workspace_id).execute()
    except Exception as exc:
        logger.error("Failed to update workspace status to 'drafted': %s", exc)

    # ── Final SSE signal ──────────────────────────────────────────────────────
    yield "data: [DONE]\n\n"

    logger.info(
        "Proposal streaming complete: workspace=%s  drafted=%d (of %d total)",
        workspace_id, saved_count, total_count,
    )


# ---------------------------------------------------------------------------
# Public API — Non-streaming (generate + save, returns list)
# ---------------------------------------------------------------------------

async def generate_and_save_proposal(workspace_id: str) -> list[dict[str, Any]]:
    """
    Non-streaming alternative: generates all sections, saves to Supabase,
    and returns the list of section records.

    Useful for background processing or when SSE is not available.
    """
    requirements = await _load_draftable_requirements(workspace_id)
    llm = _get_llm()

    sections: list[dict[str, Any]] = []

    for idx, req in enumerate(requirements, start=1):
        req_text    = req.get("requirement", "")
        section_ref = req.get("section_ref", "General")
        req_id      = req.get("id", "")
        title       = f"Section {section_ref}: {req_text[:80]}{'…' if len(req_text) > 80 else ''}"

        logger.info("Generating section %d/%d [%s]…", idx, len(requirements), section_ref)

        try:
            capabilities = search_capabilities(req_text, top_k=TOP_K_CAPS)
        except Exception:
            capabilities = []

        user_prompt = _build_draft_prompt(req, capabilities)
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ]

        try:
            full_text = await cached_invoke("draft_section", llm, messages)
        except Exception as exc:
            logger.error("GROQ generation failed for section '%s': %s", section_ref, exc)
            full_text = f"[Error generating this section: {exc}]"

        sections.append({
            "id"            : str(uuid.uuid4()),
            "workspace_id"  : workspace_id,
            "requirement_id": req_id,
            "section_title" : title,
            "section_ref"   : section_ref,
            "ai_draft"      : full_text.strip(),
            "edited_draft"  : None,
            "status"        : "draft",
        })

    # Batch insert
    if sections:
        sb = get_supabase()
        try:
            sb.table("proposal_sections").insert(sections).execute()
        except Exception as exc:
            logger.error("Supabase insert(proposal_sections) failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Database error saving proposal sections: {exc}",
            )

        # Update status
        try:
            sb.table("workspaces").update({"status": "drafted"}).eq("id", workspace_id).execute()
        except Exception as exc:
            logger.error("Failed to update workspace status: %s", exc)

    logger.info(
        "Proposal generated: workspace=%s  sections=%d",
        workspace_id, len(sections),
    )
    return sections
