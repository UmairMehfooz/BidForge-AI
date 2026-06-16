"""
BidForge AI — Requirement Extractor
=====================================
Sends raw RFP text to GROQ (llama-3.3-70b-versatile) via langchain-groq and
returns a structured list of requirements.

Public API
----------
    requirements = await extract_requirements(workspace_id, raw_text)
    # → List[dict] — validated & de-duplicated requirement objects

    saved = await extract_and_save(workspace_id, raw_text)
    # → int — number of requirements inserted into Supabase

Each requirement dict conforms to:
    {
        "section_ref" : str | None   e.g. "4.3.1" or "General"
        "requirement" : str          exact requirement text
        "type"        : str          one of VALID_TYPES
        "deadline"    : str | None   ISO-ish date string if found, else null
        "budget_ref"  : str | None   budget/value string if found, else null
    }

Design decisions
----------------
- We use ChatGroq (LangChain wrapper) for cleaner message typing and automatic
  retry behaviour via LangChain's built-in retry config.
- JSON is extracted with a multi-stage fallback:
    1. Direct json.loads()
    2. Strip markdown code fences (```json … ```) then retry
    3. Regex extraction of the outermost JSON array
    4. Line-by-line recovery: parse each line as a JSON object
  This ensures we never crash on a noisy LLM response.
- Requirements are inserted into Supabase in a single batch call to
  minimise latency.
- Text chunking: RFP text longer than MAX_CHARS is split into chunks and
  each chunk is sent separately; results are merged and de-duplicated.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import uuid
from typing import Any

from fastapi import HTTPException, status
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage

from app.services.bid_history import KNOWN_SECTORS, infer_sector_from_text
from app.services.ner_extractor import (
    extract_entities,
    format_for_prompt,
    nearest_clause_ref,
)
from app.utils.llm_cache import cached_invoke, demo_fallback
from app.utils.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
GROQ_MODEL    = "llama-3.3-70b-versatile"
MAX_TOKENS    = 4096          # max response tokens from GROQ
TEMPERATURE   = 0.0           # deterministic extraction — no creativity needed
MAX_CHARS     = 14_000        # ~3 500 tokens at ~4 chars/token (Fix 5)
CHUNK_OVERLAP = 800           # ~200 tokens of overlap between chunks
MAX_DOC_CHARS = 300_000       # hard cap — longer docs are truncated with a warning
CHUNK_DELAY_SECONDS = 0       # legacy knob (kept for tests); pacing now comes
                              # from MAX_CONCURRENT_CHUNKS + 429 backoff
MAX_CONCURRENT_CHUNKS = 3     # chunk extractions in flight at once
LLM_RETRY_DELAYS    = (2, 4, 8)   # exponential backoff on 429/transient errors

VALID_TYPES = {
    "mandatory",
    "evaluation_criteria",
    "submission_deadline",
    "budget",
    "question",
}

# ---------------------------------------------------------------------------
# System Prompt  (the exact string used for every GROQ call)
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """You are a specialist bid analyst AI for a Pakistani IT services company.

Your task is to extract ALL requirements from the RFP/RFQ/Tender text provided by the user.

For each requirement you identify, return a JSON object with EXACTLY these fields:
  - "section_ref"  : the section number from the document (e.g. "4.3.1", "Section 2", "General") — use "General" if no section number is present
  - "requirement"  : the verbatim or closely paraphrased requirement text — be complete, do not truncate
  - "type"         : MUST be one of exactly: "mandatory", "evaluation_criteria", "submission_deadline", "budget", "question"
  - "deadline"     : the deadline date string extracted from this requirement (e.g. "15 June 2025", "within 30 days") — use null if not present
  - "budget_ref"   : the budget or contract value mentioned in this requirement (e.g. "PKR 50M", "USD 200,000") — use null if not present

RULES:
1. Return ONLY a valid JSON array of these objects. No introduction. No explanation. No markdown code fences. No trailing text.
2. Extract EVERY requirement — do not summarise or skip any.
3. Do not invent requirements that are not in the text.
4. If a sentence contains multiple distinct requirements, split them into separate objects.
5. "mandatory" = must-have compliance clause or technical specification.
6. "evaluation_criteria" = how bids will be scored or weighted.
7. "submission_deadline" = any date by which something must be submitted.
8. "budget" = any financial ceiling, estimated value, or pricing requirement.
9. "question" = a question the bidder must answer in their proposal.

Example output format (do not copy these values — extract from the actual text):
[
  {"section_ref": "3.1", "requirement": "The vendor must hold ISO 27001 certification.", "type": "mandatory", "deadline": null, "budget_ref": null},
  {"section_ref": "5.2", "requirement": "Technical proposal must be submitted by 30 June 2025.", "type": "submission_deadline", "deadline": "30 June 2025", "budget_ref": null},
  {"section_ref": "General", "requirement": "Estimated contract value is PKR 50M.", "type": "budget", "deadline": null, "budget_ref": "PKR 50M"}
]"""


# ---------------------------------------------------------------------------
# LLM client (lazy — created on first call to avoid startup cost if unused)
# ---------------------------------------------------------------------------
_llm: ChatGroq | None = None


def _get_llm() -> ChatGroq:
    global _llm
    if _llm is None:
        api_key = os.getenv("GROQ_API_KEY", "")
        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="GROQ_API_KEY is not set. Add it to your .env file.",
            )
        _llm = ChatGroq(
            model=GROQ_MODEL,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            api_key=api_key,
        )
        logger.info("ChatGroq client initialised (model=%s)", GROQ_MODEL)
    return _llm


# ---------------------------------------------------------------------------
# JSON parsing — multi-stage fallback
# ---------------------------------------------------------------------------

def _safe_parse_json(raw: str) -> list[dict[str, Any]]:
    """
    Attempt to parse `raw` as a JSON array using 4 successive strategies.

    Returns an empty list (never raises) so callers always get a usable value.
    """
    if not raw or not raw.strip():
        logger.warning("LLM returned empty response.")
        return []

    text = raw.strip()

    # ── Stage 1: direct parse ───────────────────────────────────────────────
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return [result]   # single object wrapped in array
    except json.JSONDecodeError:
        pass

    # ── Stage 2: strip markdown code fences ─────────────────────────────────
    stripped = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    stripped = re.sub(r"\s*```$", "", stripped, flags=re.IGNORECASE).strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # ── Stage 3: regex — extract outermost JSON array ───────────────────────
    match = re.search(r"\[.*\]", stripped, re.DOTALL)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                logger.warning("JSON extracted via regex fallback.")
                return result
        except json.JSONDecodeError:
            pass

    # ── Stage 4: line-by-line recovery ──────────────────────────────────────
    recovered: list[dict] = []
    for line in stripped.splitlines():
        line = line.strip().rstrip(",")
        if line.startswith("{") and line.endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    recovered.append(obj)
            except json.JSONDecodeError:
                continue
    if recovered:
        logger.warning("JSON recovered line-by-line (%d objects).", len(recovered))
        return recovered

    logger.error("All JSON parsing strategies failed. Raw response:\n%s", raw[:500])
    return []


# ---------------------------------------------------------------------------
# Validation + normalisation of a single raw requirement dict
# ---------------------------------------------------------------------------

def _validate_requirement(raw: dict[str, Any], workspace_id: str) -> dict[str, Any] | None:
    """
    Coerce a raw LLM-produced dict into a clean requirement record.
    Returns None if the record is so malformed it should be discarded.
    """
    req_text = str(raw.get("requirement") or "").strip()
    if not req_text:
        return None                              # discard empty requirements

    raw_type = str(raw.get("type") or "").strip().lower()
    if raw_type not in VALID_TYPES:
        # Map common LLM variations gracefully
        type_map = {
            "mandatory_requirement": "mandatory",
            "compliance":            "mandatory",
            "evaluation":            "evaluation_criteria",
            "criteria":              "evaluation_criteria",
            "deadline":              "submission_deadline",
            "budget_ref":            "budget",
            "financial":             "budget",
            "q&a":                   "question",
        }
        raw_type = type_map.get(raw_type, "mandatory")   # default → mandatory

    section_ref = str(raw.get("section_ref") or "General").strip() or "General"
    deadline    = str(raw.get("deadline") or "").strip() or None
    budget_ref  = str(raw.get("budget_ref") or "").strip() or None

    return {
        "id"               : str(uuid.uuid4()),
        "workspace_id"     : workspace_id,
        "section_ref"      : section_ref,
        "requirement"      : req_text,
        "type"             : raw_type,
        "deadline"         : deadline,
        "budget_ref"       : budget_ref,
        "extraction_source": "llm",   # upgraded to "both" by the NER cross-check
    }


# ---------------------------------------------------------------------------
# Text chunking
# ---------------------------------------------------------------------------

_PAGE_MARKER_RE = re.compile(r"\[\[PAGE (\d+)\]\]\n?")


def _strip_page_markers(text: str) -> tuple[str, list[tuple[int, int]]]:
    """
    Remove the [[PAGE n]] markers inserted by the PDF parser.

    Returns (clean_text, page_map) where page_map is a list of
    (char_offset_in_clean_text, page_number) tuples. Empty page_map for DOCX
    or any text without markers.
    """
    page_map: list[tuple[int, int]] = []
    clean_parts: list[str] = []
    cursor = 0
    clean_len = 0
    for m in _PAGE_MARKER_RE.finditer(text):
        clean_parts.append(text[cursor:m.start()])
        clean_len += m.start() - cursor
        page_map.append((clean_len, int(m.group(1))))
        cursor = m.end()
    clean_parts.append(text[cursor:])
    return "".join(clean_parts), page_map


def _page_range(page_map: list[tuple[int, int]], start: int, end: int) -> str | None:
    """Human-readable page range ('pages 12-15') for clean-text span [start, end)."""
    if not page_map:
        return None
    first = last = None
    for offset, page in page_map:
        if offset <= start:
            first = page
        if offset < end:
            last = page
    if first is None:
        first = page_map[0][1]
    if last is None:
        last = first
    return f"page {first}" if first == last else f"pages {first}-{last}"


def _chunk_spans(text: str, max_chars: int = MAX_CHARS, overlap: int = CHUNK_OVERLAP) -> list[tuple[int, int]]:
    """
    Split `text` into (start, end) spans of at most `max_chars` chars with
    `overlap` chars of overlap. Breaks at the paragraph boundary (\\n\\n)
    nearest the limit; failing that at a sentence end — never mid-sentence.
    """
    if len(text) <= max_chars:
        return [(0, len(text))]

    spans: list[tuple[int, int]] = []
    start = 0
    while start < len(text):
        end = start + max_chars
        if end < len(text):
            boundary = text.rfind("\n\n", start + (max_chars // 2), end)
            if boundary == -1:
                # No paragraph break — fall back to the last sentence end
                sentence_break = max(
                    text.rfind(". ", start + (max_chars // 2), end),
                    text.rfind(".\n", start + (max_chars // 2), end),
                )
                boundary = sentence_break + 1 if sentence_break != -1 else -1
            if boundary > start:
                end = boundary
        spans.append((start, min(end, len(text))))
        if end >= len(text):
            break
        start = end - overlap   # back-track by overlap chars

    logger.info("RFP text split into %d chunks (max_chars=%d, overlap=%d).",
                len(spans), max_chars, overlap)
    return spans


# ---------------------------------------------------------------------------
# Core LLM call
# ---------------------------------------------------------------------------

def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return "429" in text or "rate limit" in text or "rate_limit" in text


async def _call_groq(
    chunk: str,
    ner_block: str = "",
    page_range: str | None = None,
) -> list[dict[str, Any]] | None:
    """
    Send one text chunk to GROQ and return a list of raw requirement dicts.

    - `ner_block`: deterministic NER findings for this chunk (Fix 4), injected
      as verified entities so the LLM anchors on exact dates/amounts.
    - `page_range`: e.g. "pages 12-15" (Fix 5) — keeps section_ref grounded.
    - On 429/transient errors retries with exponential backoff (2s/4s/8s),
      then returns None so the caller can keep partial results (never raises
      for a single chunk).
    """
    llm = _get_llm()
    user_content = f"Extract all requirements from the following RFP text"
    if page_range:
        user_content += f" (from {page_range} of the document)"
    user_content += f":\n\n{chunk}"
    if ner_block:
        user_content += (
            "\n\nVERIFIED ENTITIES (found by deterministic NER — these are "
            "definitely present in the text; anchor your extraction on them, "
            "use their exact values, and do not drop any):\n" + ner_block
        )
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(content=user_content),
    ]

    last_exc: Exception | None = None
    for attempt, delay in enumerate((0,) + LLM_RETRY_DELAYS):
        if delay:
            logger.warning("GROQ retry in %ds (attempt %d): %s", delay, attempt + 1, last_exc)
            await asyncio.sleep(delay)
        try:
            # Cache-first (Fix 10): hit → no GROQ call; miss → live + write
            raw_content = await cached_invoke("extract_requirements", llm, messages)
            logger.debug("GROQ raw response (%d chars): %s…", len(raw_content), raw_content[:200])
            return _safe_parse_json(raw_content)
        except Exception as exc:
            last_exc = exc
            if not _is_rate_limit(exc) and attempt >= 1:
                break   # non-429 errors get one retry, not the full backoff

    # DEMO_MODE insurance (Fix 10): nearest cached response for this tag
    fallback = demo_fallback("extract_requirements", messages[-1].content)
    if fallback is not None:
        return _safe_parse_json(fallback)

    logger.error("GROQ chunk extraction failed after retries: %s", last_exc)
    return None


# ---------------------------------------------------------------------------
# De-duplication
# ---------------------------------------------------------------------------

def _merge_sources(a: str | None, b: str | None) -> str:
    """'both' wins; differing llm/ner merge to 'both'."""
    sources = {s for s in (a, b) if s}
    if "both" in sources or sources == {"llm", "ner"}:
        return "both"
    return next(iter(sources), "llm")


def _deduplicate(requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Fuzzy de-duplication for chunk-overlap repeats (Fix 5).

    Two requirements are duplicates when:
      - difflib.SequenceMatcher ratio > 0.85 on the requirement text, OR
      - identical section_ref + same type AND ratio > 0.60.
        (The playbook's plain section_ref+type rule would collapse distinct
        requirements that legitimately share a section — e.g. several
        mandatory clauses under "General" — so a moderate text-similarity
        floor is required as well.)

    The longer/more specific text wins; extraction_source merges ('both' wins).
    """
    unique: list[dict[str, Any]] = []
    removed = 0

    for req in requirements:
        text = req.get("requirement", "").lower().strip()
        if not text:
            continue

        duplicate_of = None
        for kept in unique:
            kept_text = kept.get("requirement", "").lower().strip()
            matcher = difflib.SequenceMatcher(None, text, kept_text)
            if matcher.real_quick_ratio() <= 0.60:
                continue   # cheap upper-bound prefilter
            ratio = matcher.ratio()
            same_section_and_type = (
                req.get("section_ref") == kept.get("section_ref")
                and req.get("type") == kept.get("type")
                and req.get("section_ref") not in (None, "", "General")
            )
            if ratio > 0.85 or (same_section_and_type and ratio > 0.60):
                duplicate_of = kept
                break

        if duplicate_of is None:
            unique.append(req)
        else:
            removed += 1
            # Keep the longer/more specific text and merge metadata
            if len(req.get("requirement", "")) > len(duplicate_of.get("requirement", "")):
                duplicate_of["requirement"] = req["requirement"]
            duplicate_of["extraction_source"] = _merge_sources(
                duplicate_of.get("extraction_source"), req.get("extraction_source")
            )
            for field in ("deadline", "budget_ref"):
                if not duplicate_of.get(field) and req.get(field):
                    duplicate_of[field] = req[field]

    if removed:
        logger.info("De-duplicated %d overlapping requirements.", removed)
    return unique


# ---------------------------------------------------------------------------
# Public API — extract only (no DB write)
# ---------------------------------------------------------------------------

async def extract_requirements(
    workspace_id: str, raw_text: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Extract requirements from `raw_text` using chunked GROQ calls + NER.

    Parameters
    ----------
    workspace_id : str   UUID of the workspace (used to populate the `workspace_id` field)
    raw_text     : str   Full RFP text from document_parser.parse_document()
                         (may contain [[PAGE n]] markers from the PDF parser)

    Returns
    -------
    (requirements, warnings)
        requirements — clean, validated records ready for Supabase insert
        warnings     — non-fatal pipeline warnings ("document truncated…",
                       "chunk 5/7 failed…") to surface on the workspace
    """
    raw_text = raw_text.strip()
    if not raw_text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot extract requirements: RFP text is empty.",
        )

    warnings: list[str] = []

    # ── Strip page markers; keep the char→page map for chunk page ranges ────
    text, page_map = _strip_page_markers(raw_text)

    # ── Hard cap: 300k chars (~75k tokens) — truncate with a warning ────────
    if len(text) > MAX_DOC_CHARS:
        warnings.append("document truncated for processing")
        logger.warning(
            "Document is %d chars — truncating to %d.", len(text), MAX_DOC_CHARS
        )
        text = text[:MAX_DOC_CHARS]

    spans = _chunk_spans(text)
    logger.info(
        "Extracting requirements from workspace=%s, text=%d chars, chunks=%d",
        workspace_id, len(text), len(spans),
    )

    # ── NER pass over the full document (Fix 4) — runs BEFORE the LLM ───────
    entities = extract_entities(text)

    # ── Call GROQ per chunk — up to MAX_CONCURRENT_CHUNKS in flight at once.
    # (Sequential + fixed sleeps cost ~6.5s/chunk of dead time on big docs;
    # bounded concurrency with the existing 429 backoff is ~3x faster.)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHUNKS)
    progress = {"done": 0, "count": 0}

    async def _extract_chunk(i: int, start: int, end: int):
        chunk = text[start:end].strip()
        page_range = _page_range(page_map, start, end)
        chunk_entities = extract_entities(chunk) if len(spans) > 1 else entities
        async with semaphore:
            raw_items = await _call_groq(
                chunk,
                ner_block=format_for_prompt(chunk_entities),
                page_range=page_range,
            )
            if CHUNK_DELAY_SECONDS:
                await asyncio.sleep(CHUNK_DELAY_SECONDS)
        progress["done"] += 1
        if raw_items is not None:
            progress["count"] += len(raw_items)
            logger.info(
                "chunk %d/%d extracted, %d requirements so far%s",
                progress["done"], len(spans), progress["count"],
                f" ({page_range})" if page_range else "",
            )
        return i, raw_items

    chunk_results = await asyncio.gather(
        *(_extract_chunk(i, s, e) for i, (s, e) in enumerate(spans, start=1))
    )

    all_raw: list[dict[str, Any]] = []
    failed_chunks = 0
    for i, raw_items in sorted(chunk_results):   # keep document order
        if raw_items is None:
            failed_chunks += 1
            warnings.append(f"chunk {i}/{len(spans)} failed after retries — partial extraction")
            logger.warning("Chunk %d/%d failed — continuing with partial results.", i, len(spans))
        else:
            all_raw.extend(raw_items)

    # Nothing at all extracted AND at least one hard failure → the document
    # could not be processed; surface a real error instead of empty success.
    if not all_raw and failed_chunks:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GROQ API unavailable — no requirements could be extracted.",
        )

    # ── Validate, normalise, de-duplicate ────────────────────────────────────
    validated: list[dict[str, Any]] = []
    for raw in all_raw:
        cleaned = _validate_requirement(raw, workspace_id)
        if cleaned:
            validated.append(cleaned)

    final = _deduplicate(validated)

    # ── Cross-check against NER: tag sources, append missed entities ────────
    final = _merge_ner_findings(final, entities, workspace_id)

    # ── Classify evaluation criteria against the taxonomy (Fix 8) ───────────
    from app.services.criteria_taxonomy import classify_requirements
    mapped = classify_requirements(final)
    eval_total = sum(1 for r in final if r.get("type") == "evaluation_criteria")
    if eval_total:
        logger.info("Evaluation criteria mapped to taxonomy: %d/%d", mapped, eval_total)
    # Supabase bulk inserts need uniform keys — default the taxonomy fields
    for r in final:
        r.setdefault("taxonomy_id", None)
        r.setdefault("taxonomy_name", None)

    logger.info(
        "Extraction complete: workspace=%s  total=%d requirements  warnings=%d",
        workspace_id, len(final), len(warnings),
    )
    return final, warnings


# ---------------------------------------------------------------------------
# NER cross-check (Fix 4) — tag sources, append entities the LLM missed
# ---------------------------------------------------------------------------

def _requirement_covers(req: dict[str, Any], needles: list[str]) -> bool:
    """True if any needle string appears in the requirement's text fields."""
    haystack = " ".join(
        str(req.get(k) or "") for k in ("requirement", "deadline", "budget_ref")
    ).lower()
    return any(n and n.lower() in haystack for n in needles)


def _merge_ner_findings(
    requirements: list[dict[str, Any]],
    entities: dict[str, list[dict]],
    workspace_id: str,
) -> list[dict[str, Any]]:
    """
    1. Tag each LLM requirement with extraction_source "both" when it overlaps
       a deterministic NER entity (deadline / budget / certification).
    2. Append any NER deadline or budget the LLM missed as its own
       requirement (extraction_source "ner"), with section_ref taken from the
       nearest preceding clause reference.
    """
    # ── 1. Upgrade matching requirements to "both" ───────────────────────────
    for req in requirements:
        markers: list[str] = []
        for ent in entities.get("deadlines", []):
            markers += [ent["value"], str(ent["normalized"])]
        for ent in entities.get("budgets", []):
            markers.append(ent["value"])
        for ent in entities.get("certifications", []):
            markers.append(str(ent["normalized"]))
        if _requirement_covers(req, markers):
            req["extraction_source"] = "both"

    appended = 0
    # Coverage is judged against the LLM's own output — appended NER snippets
    # must not mask other entities that happen to share the same sentence.
    llm_requirements = list(requirements)

    # ── 2. Append missed deadlines ───────────────────────────────────────────
    for ent in entities.get("deadlines", []):
        if any(
            _requirement_covers(r, [ent["value"], str(ent["normalized"])])
            for r in llm_requirements
        ):
            continue
        requirements.append({
            "id"               : str(uuid.uuid4()),
            "workspace_id"     : workspace_id,
            "section_ref"      : nearest_clause_ref(entities, ent["position"]) or "General",
            "requirement"      : f"Deadline identified in document: {ent['snippet']}",
            "type"             : "submission_deadline",
            "deadline"         : str(ent["normalized"]),   # ISO date
            "budget_ref"       : None,
            "extraction_source": "ner",
        })
        appended += 1

    # ── 3. Append missed budgets ─────────────────────────────────────────────
    for ent in entities.get("budgets", []):
        if any(_requirement_covers(r, [ent["value"]]) for r in llm_requirements):
            continue
        requirements.append({
            "id"               : str(uuid.uuid4()),
            "workspace_id"     : workspace_id,
            "section_ref"      : nearest_clause_ref(entities, ent["position"]) or "General",
            "requirement"      : f"Budget reference identified in document: {ent['snippet']}",
            "type"             : "budget",
            "deadline"         : None,
            "budget_ref"       : ent["value"],
            "extraction_source": "ner",
        })
        appended += 1

    if appended:
        logger.info("NER cross-check appended %d requirements the LLM missed.", appended)
    both = sum(1 for r in requirements if r.get("extraction_source") == "both")
    logger.info(
        "Extraction sources: %d both, %d llm, %d ner",
        both, len(requirements) - both - appended, appended,
    )
    return requirements


# ---------------------------------------------------------------------------
# Sector + issuer-type inference — one extra lightweight LLM call during /parse
# ---------------------------------------------------------------------------

_PROFILE_PROMPT = (
    "You are classifying an RFP/tender document. Reply with ONLY a JSON object "
    '{"sector": "<sector>", "issuer_type": "govt" or "private"} and nothing else. '
    "sector must be exactly one of: " + ", ".join(KNOWN_SECTORS) + ". "
    "issuer_type is 'govt' if the issuing organisation is a government body, "
    "ministry, authority, or state-owned enterprise; otherwise 'private'."
)

_GOVT_KEYWORDS = (
    "government", "ministry", "authority", "ppra", "public sector", "federal",
    "provincial", "municipal", "state-owned", "directorate", "secretariat",
    "tender notice", "public procurement",
)


def _infer_issuer_type_keywords(text: str) -> str:
    """Keyword fallback: 'govt' if government markers dominate the opening."""
    lowered = text[:6000].lower()
    hits = sum(lowered.count(kw) for kw in _GOVT_KEYWORDS)
    return "govt" if hits >= 2 else "private"


async def infer_rfp_profile(raw_text: str) -> dict[str, str | None]:
    """
    Infer the RFP's sector (one of KNOWN_SECTORS — used for the sector-level
    win rate in scoring) and issuer type ('govt'/'private' — used by hybrid
    capability matching).

    Strategy: one small LLM call over the document opening (first 4 000 chars,
    where issuer/subject info lives), validated against the known values; on
    any failure fall back to keyword matching. Never raises.
    """
    snippet = raw_text[:4000]
    sector: str | None = None
    issuer_type: str | None = None

    try:
        llm = _get_llm()
        answer = await cached_invoke("infer_profile", llm, [
            SystemMessage(content=_PROFILE_PROMPT),
            HumanMessage(content=f"RFP text:\n\n{snippet}\n\nJSON:"),
        ])
        parsed = _safe_parse_json(answer.strip())
        obj = parsed[0] if parsed else {}
        raw_sector = str(obj.get("sector") or "")
        raw_issuer = str(obj.get("issuer_type") or "").strip().lower()
        for known in KNOWN_SECTORS:
            if known.lower() in raw_sector.lower():
                sector = known
                break
        if raw_issuer in ("govt", "private"):
            issuer_type = raw_issuer
        logger.info("RFP profile from LLM: sector=%s issuer_type=%s", sector, issuer_type)
    except Exception as exc:
        logger.warning("LLM profile inference failed (%s) — using keyword fallback.", exc)

    if sector is None:
        sector = infer_sector_from_text(raw_text)
        if sector:
            logger.info("Sector inferred by keywords: %s", sector)
    if issuer_type is None:
        issuer_type = _infer_issuer_type_keywords(raw_text)
        logger.info("Issuer type inferred by keywords: %s", issuer_type)

    return {"sector": sector, "issuer_type": issuer_type}


def _save_workspace_warning(workspace_id: str, warnings: list[str]) -> None:
    """
    Persist pipeline warnings ("document truncated…", failed chunks) on the
    workspace row. Logs and continues if the column doesn't exist yet.
    """
    if not warnings:
        return
    try:
        sb = get_supabase()
        sb.table("workspaces").update(
            {"warning": "; ".join(warnings)}
        ).eq("id", workspace_id).execute()
        logger.info("Workspace %s warning saved: %s", workspace_id, warnings)
    except Exception as exc:
        logger.warning(
            "Could not save warning to workspace (%s). Run: "
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS warning TEXT;", exc,
        )


def _save_workspace_profile(workspace_id: str, profile: dict[str, str | None]) -> None:
    """
    Persist inferred sector/issuer_type on the workspace row. Logs and
    continues if the columns don't exist yet (run the ALTERs in schema.sql).
    """
    payload = {k: v for k, v in profile.items() if v}
    if not payload:
        return
    try:
        sb = get_supabase()
        sb.table("workspaces").update(payload).eq("id", workspace_id).execute()
        logger.info("Workspace %s profile saved: %s", workspace_id, payload)
    except Exception as exc:
        logger.warning(
            "Could not save RFP profile to workspace (%s). Run: "
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS sector TEXT; "
            "ALTER TABLE workspaces ADD COLUMN IF NOT EXISTS issuer_type TEXT;",
            exc,
        )


# ---------------------------------------------------------------------------
# Public API — extract + save to Supabase
# ---------------------------------------------------------------------------

async def extract_and_save(workspace_id: str, raw_text: str) -> int:
    """
    Extract requirements from `raw_text` and persist them to the
    Supabase `requirements` table.

    Parameters
    ----------
    workspace_id : str   UUID of the workspace
    raw_text     : str   Full RFP text

    Returns
    -------
    int   Number of requirements successfully inserted.

    Raises
    ------
    HTTPException 400   If raw_text is empty.
    HTTPException 503   If GROQ API or Supabase is unavailable.
    HTTPException 500   If DB insert fails unexpectedly.
    """
    # Profile inference (sector + issuer type) is an independent LLM call —
    # run it concurrently with chunk extraction instead of after it.
    (requirements, warnings), profile = await asyncio.gather(
        extract_requirements(workspace_id, raw_text),
        infer_rfp_profile(raw_text),
    )
    _save_workspace_profile(workspace_id, profile)
    _save_workspace_warning(workspace_id, warnings)

    if not requirements:
        logger.warning("No requirements extracted from workspace=%s", workspace_id)
        return 0

    # ── Batch insert into Supabase ────────────────────────────────────────────
    # Re-parsing REPLACES previous results — without this, every re-run
    # appended a fresh copy of all requirements (and downstream stages then
    # multiplied compliance items and proposal sections).
    sb = get_supabase()
    try:
        sb.table("requirements").delete().eq("workspace_id", workspace_id).execute()
    except Exception as exc:
        logger.warning("Could not clear previous requirements: %s", exc)

    try:
        result = sb.table("requirements").insert(requirements).execute()
    except Exception as exc:
        # Most likely cause: one of the newer optional columns doesn't exist
        # yet. Retry once without all of them rather than failing the parse.
        optional_columns = ("extraction_source", "taxonomy_id", "taxonomy_name")
        if any(col in str(exc) for col in optional_columns):
            logger.warning(
                "Insert failed on an optional column — retrying without %s. "
                "Run the ALTER TABLE migrations in schema.sql. Error: %s",
                optional_columns, exc,
            )
            stripped = [
                {k: v for k, v in r.items() if k not in optional_columns}
                for r in requirements
            ]
            try:
                result = sb.table("requirements").insert(stripped).execute()
            except Exception as exc2:
                logger.error("Supabase insert(requirements) failed: %s", exc2)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=f"Database error while saving requirements: {exc2}",
                )
        else:
            logger.error("Supabase insert(requirements) failed: %s", exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Database error while saving requirements: {exc}",
            )

    if result.data is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Supabase insert returned None. Check RLS policies on `requirements` table.",
        )

    inserted_count = len(result.data)
    logger.info(
        "Saved %d requirements to Supabase for workspace=%s",
        inserted_count, workspace_id,
    )
    return inserted_count
