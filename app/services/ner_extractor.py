"""
BidForge AI — NER Extractor (Fix 4)
====================================
Deterministic named-entity recognition that runs ALONGSIDE the LLM during
/parse. Regex + dateparser only — no spaCy (too heavy for deployment).

The NER pass serves two purposes:
  1. Its findings are injected into the LLM extraction prompt as "verified
     entities", anchoring the model on exact dates/amounts it must not drop.
  2. After LLM extraction, any NER deadline/budget the LLM missed is appended
     as its own requirement, and every requirement is tagged with
     extraction_source: "llm" | "ner" | "both".

Public API
----------
    entities = extract_entities(text)
    # → {"budgets": [...], "percentages": [...], "deadlines": [...],
    #    "certifications": [...], "clause_refs": [...]}

Each entity dict:
    {
        "value"     : str          — exact matched text
        "normalized": str | float — ISO date for deadlines, numeric millions
                                     for budgets, canonical name for certs
        "snippet"   : str          — ±60 chars of surrounding context
        "position"  : int          — char offset in the source text
    }
"""

from __future__ import annotations

import logging
import re
from typing import Any

import dateparser.search

from app.services.bid_history import parse_budget_millions

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------
_BUDGET_RE = re.compile(
    r"(?:PKR|Rs\.?|USD)\s?[\d,.]+\s?(?:M|B|million|billion|crore|lakh)?",
    re.IGNORECASE,
)

_PERCENT_RE = re.compile(r"\b\d{1,3}\s?%")

_CERT_PATTERNS: dict[str, str] = {
    "ISO 27001": r"\bISO\s*27001\b",
    "ISO 9001" : r"\bISO\s*9001\b",
    "ISO 14001": r"\bISO\s*14001\b",
    "CMMI"     : r"\bCMMI\s*L(?:evel)?\s*\d\b|\bCMMI\b",
    "PMP"      : r"\bPMP\b",
    "PEC"      : r"\bPEC\b",
    "CE Mark"  : r"\bCE\s*Mark(?:ing)?\b",
    "OHSAS"    : r"\bOHSAS(?:\s*18001)?\b",
}

_CLAUSE_RE = re.compile(
    r"(?:Section|Clause|Article|Annex(?:ure)?)\s+[\dA-Z][\d.\-A-Za-z]*",
)

# Sentences must contain one of these to be scanned for deadline dates
_DEADLINE_KEYWORDS = (
    "deadline", "submission", "submit", "due", "closing", "last date",
    "no later than", "on or before", "valid until",
)

_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?;\n])\s+")

# dateparser settings: prefer day-first (Pakistani convention), future dates
_DATEPARSER_SETTINGS = {
    "DATE_ORDER": "DMY",
    "PREFER_DATES_FROM": "future",
    "REQUIRE_PARTS": ["day", "month", "year"],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snippet(text: str, start: int, end: int, margin: int = 60) -> str:
    """±margin chars of context around [start, end), whitespace-collapsed."""
    lo = max(0, start - margin)
    hi = min(len(text), end + margin)
    return re.sub(r"\s+", " ", text[lo:hi]).strip()


def _entity(text: str, value: str, normalized: Any, start: int, end: int) -> dict[str, Any]:
    return {
        "value"     : value.strip(),
        "normalized": normalized,
        "snippet"   : _snippet(text, start, end),
        "position"  : start,
    }


# ---------------------------------------------------------------------------
# Individual extractors
# ---------------------------------------------------------------------------

def _extract_budgets(text: str) -> list[dict[str, Any]]:
    results = []
    for m in _BUDGET_RE.finditer(text):
        millions = parse_budget_millions(m.group())
        if millions is None:
            continue
        results.append(_entity(text, m.group(), str(millions), m.start(), m.end()))
    return results


def _extract_percentages(text: str) -> list[dict[str, Any]]:
    """Percentages with 40 chars of context — captures evaluation weights."""
    results = []
    for m in _PERCENT_RE.finditer(text):
        lo = max(0, m.start() - 40)
        hi = min(len(text), m.end() + 40)
        context = re.sub(r"\s+", " ", text[lo:hi]).strip()
        ent = _entity(text, m.group(), m.group().replace(" ", ""), m.start(), m.end())
        ent["context"] = context
        results.append(ent)
    return results


def _extract_deadlines(text: str) -> list[dict[str, Any]]:
    """
    dateparser.search_dates over sentences containing deadline keywords.
    Normalized form is the ISO date (YYYY-MM-DD).
    """
    results = []
    seen: set[str] = set()
    offset = 0
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        pos = text.find(sentence, offset)
        if pos >= 0:
            offset = pos
        lowered = sentence.lower()
        if not any(kw in lowered for kw in _DEADLINE_KEYWORDS):
            continue
        try:
            found = dateparser.search.search_dates(
                sentence, languages=["en"], settings=_DATEPARSER_SETTINGS
            )
        except Exception as exc:                      # dateparser edge cases
            logger.debug("dateparser failed on sentence: %s", exc)
            continue
        for matched_text, dt in (found or []):
            # Discard noise like bare numbers that dateparser over-matches
            if not re.search(r"[A-Za-z]{3,}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}", matched_text):
                continue
            iso = dt.date().isoformat()
            if iso in seen:
                continue
            seen.add(iso)
            start = pos + max(0, sentence.find(matched_text))
            results.append(_entity(text, matched_text, iso, start, start + len(matched_text)))
    return results


def _extract_certifications(text: str) -> list[dict[str, Any]]:
    results = []
    seen: set[str] = set()
    for canonical, pattern in _CERT_PATTERNS.items():
        for m in re.finditer(pattern, text, re.IGNORECASE):
            # Normalize CMMI levels to e.g. "CMMI L3"
            normalized = canonical
            if canonical == "CMMI":
                level = re.search(r"\d", m.group())
                normalized = f"CMMI L{level.group()}" if level else "CMMI"
            key = f"{normalized}@{m.start()}"
            if key in seen:
                continue
            seen.add(key)
            results.append(_entity(text, m.group(), normalized, m.start(), m.end()))
    return results


def _extract_clause_refs(text: str) -> list[dict[str, Any]]:
    results = []
    for m in _CLAUSE_RE.finditer(text):
        value = m.group().strip().rstrip(".")
        results.append(_entity(text, value, value, m.start(), m.end()))
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_entities(text: str) -> dict[str, list[dict[str, Any]]]:
    """
    Run all five deterministic extractors over `text`.

    Returns a dict with keys: budgets, percentages, deadlines,
    certifications, clause_refs — each a list of entity dicts.
    """
    if not text:
        return {
            "budgets": [], "percentages": [], "deadlines": [],
            "certifications": [], "clause_refs": [],
        }

    entities = {
        "budgets"       : _extract_budgets(text),
        "percentages"   : _extract_percentages(text),
        "deadlines"     : _extract_deadlines(text),
        "certifications": _extract_certifications(text),
        "clause_refs"   : _extract_clause_refs(text),
    }
    logger.info(
        "NER: %d budgets, %d percentages, %d deadlines, %d certifications, %d clause refs",
        *(len(entities[k]) for k in
          ("budgets", "percentages", "deadlines", "certifications", "clause_refs")),
    )
    return entities


def nearest_clause_ref(entities: dict[str, list[dict]], position: int) -> str | None:
    """
    The clause/section reference nearest to `position` — used as section_ref
    for NER-appended requirements. Prefers the closest ref BEFORE the entity
    (within 2 000 chars, i.e. the section the entity sits in); falls back to
    a ref shortly AFTER it (within 150 chars — same sentence, e.g.
    "... by 15 March 2026 per Clause 2.1").
    """
    best, best_distance = None, 2_000
    for ref in entities.get("clause_refs", []):
        if ref["position"] <= position:
            distance = position - ref["position"]          # preceding section header
        else:
            distance = ref["position"] - position
            if distance >= 150:                            # after-refs: same sentence only
                continue
        if distance < best_distance:
            best, best_distance = ref["value"], distance
    return best


def format_for_prompt(entities: dict[str, list[dict]], max_per_kind: int = 8) -> str:
    """
    Compact textual form of the NER findings, injected into the LLM
    extraction prompt as "verified entities" so the model anchors on them.
    """
    lines = []
    kinds = (
        ("deadlines", "Deadlines"),
        ("budgets", "Budgets"),
        ("percentages", "Percentages/weights"),
        ("certifications", "Certifications"),
        ("clause_refs", "Clause references"),
    )
    for key, label in kinds:
        items = entities.get(key, [])[:max_per_kind]
        if not items:
            continue
        if key == "deadlines":
            rendered = ", ".join(f"{e['value']} ({e['normalized']})" for e in items)
        elif key == "budgets":
            rendered = ", ".join(f"{e['value']} (~{e['normalized']}M PKR)" for e in items)
        elif key == "percentages":
            rendered = "; ".join(e.get("context", e["value"]) for e in items)
        else:
            rendered = ", ".join(dict.fromkeys(e["normalized"] for e in items))
        lines.append(f"- {label}: {rendered}")
    return "\n".join(lines)
