"""
Fix 5 verification — chunking, page ranges, fuzzy dedup, graceful degradation.
Run:  venv\\Scripts\\python.exe scripts\\verify_fix5.py
(No network: the GROQ call is monkeypatched for the pipeline test.)
"""

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)

import app.services.requirement_extractor as rex  # noqa: E402
from app.services.requirement_extractor import (  # noqa: E402
    _chunk_spans,
    _deduplicate,
    _is_rate_limit,
    _merge_sources,
    _page_range,
    _strip_page_markers,
)

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        failures.append(label)


# ── 1. Page markers ──────────────────────────────────────────────────────────
marked = "[[PAGE 1]]\nFirst page text.\n\n[[PAGE 2]]\nSecond page text.\n\n[[PAGE 3]]\nThird."
clean, page_map = _strip_page_markers(marked)
check("markers stripped", "[[PAGE" not in clean)
check("page map has 3 entries", len(page_map) == 3, f"(got {page_map})")
check("page range single page", _page_range(page_map, 0, 10) == "page 1")
end = len(clean)
check("page range spans 1-3", _page_range(page_map, 0, end) == "pages 1-3",
      f"(got {_page_range(page_map, 0, end)})")
check("no markers -> None range", _page_range([], 0, 100) is None)

# ── 2. Chunking ──────────────────────────────────────────────────────────────
para = "This is a sentence about requirements. Another sentence follows here.\n\n"
big = para * 600   # ~43k chars
spans = _chunk_spans(big)
sizes = [e - s for s, e in spans]
check("multiple chunks for 43k chars", len(spans) >= 3, f"(got {len(spans)} chunks)")
check("all chunks <= 14k", all(n <= 14_000 for n in sizes), f"(max={max(sizes)})")
check("chunks overlap by ~800", all(spans[i + 1][0] == spans[i][1] - 800 for i in range(len(spans) - 1)))
check("full coverage (last chunk reaches end)", spans[-1][1] == len(big))
boundary_ok = all(
    big[e - 1] in ".\n" or big[e:e + 2] == "" or big[max(0, e - 2):e] == "\n\n"
    for _, e in spans[:-1]
)
check("chunks end at paragraph/sentence boundaries", boundary_ok)

no_para = ("A sentence that just keeps going with words. " * 800).strip()  # no \n\n
spans2 = _chunk_spans(no_para)
check("sentence fallback when no paragraphs",
      all(no_para[e - 1] == "." or e == len(no_para) for _, e in spans2),
      f"(ends: {[no_para[e-2:e] for _, e in spans2[:-1]]})")

small_spans = _chunk_spans("short doc")
check("small doc -> single span", small_spans == [(0, 9)])

# ── 3. Fuzzy dedup ───────────────────────────────────────────────────────────
def req(text, section="4.1", rtype="mandatory", source="llm", **kw):
    return {"id": "x", "workspace_id": "ws", "section_ref": section,
            "requirement": text, "type": rtype, "deadline": kw.get("deadline"),
            "budget_ref": kw.get("budget_ref"), "extraction_source": source}

d = _deduplicate([
    req("The vendor must hold ISO 27001 certification for all data centres."),
    req("The vendor must hold ISO 27001 certification for all data centers.", source="ner"),  # near-dup
    req("Bidder shall provide 24/7 support with 99.9% uptime SLA guarantees."),               # distinct, same section
])
check("near-duplicates merged (ratio>0.85)", len(d) == 2, f"(got {len(d)})")
check("merged source is 'both'", d[0]["extraction_source"] == "both")
check("distinct req in same section survives", "24/7" in d[1]["requirement"])

d2 = _deduplicate([
    req("Submit technical proposal in three hard copies before the deadline.", section="5.2", rtype="submission_deadline"),
    req("Submit the technical proposal in 3 hard copies prior to deadline date.", section="5.2", rtype="submission_deadline"),
])
check("same section+type with moderate similarity merges", len(d2) == 1, f"(got {len(d2)})")

d3 = _deduplicate([
    req("Requirement about cloud hosting.", section="General"),
    req("Requirement about staff training programs.", section="General"),
])
check("'General' section never triggers section-based merge", len(d3) == 2)

longer = _deduplicate([
    req("The vendor must provide twenty-four seven helpdesk support services for all users"),
    req("The vendor must provide twenty-four seven helpdesk support services for all users across Pakistan"),
])
check("longer text wins on merge",
      len(longer) == 1 and "across Pakistan" in longer[0]["requirement"],
      f"(kept: {longer[0]['requirement'][:60]})")

check("merge_sources both wins", _merge_sources("both", "llm") == "both")
check("merge_sources llm+ner -> both", _merge_sources("llm", "ner") == "both")

# ── 4. Rate-limit detection ──────────────────────────────────────────────────
check("429 detected", _is_rate_limit(Exception("Error code: 429 - rate_limit_exceeded")))
check("other errors not 429", not _is_rate_limit(Exception("connection reset")))

# ── 5. Mocked pipeline: truncation + partial failure + warnings ─────────────
calls = {"n": 0}

_FAKE_REQS = [
    "deploy a SIEM platform integrated with the national CERT feeds",
    "construct drainage culverts along the entire 40 km road corridor",
    "train 500 hospital staff on the new patient management system",
    "supply solar inverters rated for 50 degrees ambient temperature",
    "maintain spare-part inventories at three regional warehouses",
    "implement biometric attendance across all campus buildings",
    "provide a performance bank guarantee of five percent",
    "operate a bilingual call centre during business hours",
]

async def fake_groq(chunk, ner_block="", page_range=None):
    calls["n"] += 1
    if calls["n"] == 2:
        return None   # simulate chunk 2 dying after retries
    detail = _FAKE_REQS[calls["n"] % len(_FAKE_REQS)]
    return [{"section_ref": f"S{calls['n']}", "requirement": f"({page_range}) The bidder shall {detail}.",
             "type": "mandatory", "deadline": None, "budget_ref": None}]

orig_call, orig_delay = rex._call_groq, rex.CHUNK_DELAY_SECONDS
rex._call_groq, rex.CHUNK_DELAY_SECONDS = fake_groq, 0

# Build a marked doc > 300k chars so truncation also triggers
page_block = "Some paragraph with enough words to be realistic content here.\n\n" * 220  # ~14k
doc = "".join(f"[[PAGE {i}]]\n{page_block}" for i in range(1, 25))  # ~340k chars
reqs, warnings = asyncio.run(rex.extract_requirements("ws-test", doc))

rex._call_groq, rex.CHUNK_DELAY_SECONDS = orig_call, orig_delay

check("truncation warning emitted", "document truncated for processing" in warnings,
      f"(warnings={warnings})")
check("failed-chunk warning emitted", any("failed after retries" in w for w in warnings))
check("partial results survive a dead chunk", len(reqs) >= 2, f"(got {len(reqs)})")
check("page ranges reached the LLM prompt", any("page" in r["requirement"] for r in reqs))
check("~22 chunks attempted for 300k chars", 20 <= calls["n"] <= 24, f"(calls={calls['n']})")

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
