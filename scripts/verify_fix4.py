"""
Fix 4 verification — deterministic NER extractor.
Run:  venv\\Scripts\\python.exe scripts\\verify_fix4.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from app.services.ner_extractor import (  # noqa: E402
    extract_entities,
    format_for_prompt,
    nearest_clause_ref,
)

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        failures.append(label)


# ── Playbook unit test ───────────────────────────────────────────────────────
t = ("Proposals must be submitted no later than 15 March 2026. "
     "Budget ceiling is PKR 250M. Technical evaluation: 70%, Financial: 30%. "
     "Bidder must hold ISO 27001 per Clause 4.2.")
e = extract_entities(t)

check("deadline 2026-03-15", any("2026-03-15" in str(d) for d in e["deadlines"]),
      f"(got {[d['normalized'] for d in e['deadlines']]})")
check("budget normalized 250", any("250" in str(b["normalized"]) for b in e["budgets"]),
      f"(got {[b['normalized'] for b in e['budgets']]})")
check("exactly 2 percentages", len(e["percentages"]) == 2,
      f"(got {[p['value'] for p in e['percentages']]})")
check("ISO 27001 found", "ISO 27001" in str(e["certifications"]))
check("Clause 4.2 found", "Clause 4.2" in str(e["clause_refs"]))

# ── Entity structure ─────────────────────────────────────────────────────────
d0 = e["deadlines"][0]
check("entities carry value/normalized/snippet/position",
      all(k in d0 for k in ("value", "normalized", "snippet", "position")))
check("percentage context ~40 chars", "Technical evaluation" in e["percentages"][0]["context"])

# ── Additional patterns ──────────────────────────────────────────────────────
t2 = ("Per Section 3.1, vendor must be CMMI Level 3 certified and PEC registered. "
      "Closing date: 01/08/2026. Estimated cost Rs. 1.2B per Annexure A. "
      "Quality must follow OHSAS 18001 and CE Marking standards.")
e2 = extract_entities(t2)
check("CMMI L3 normalized", "CMMI L3" in str(e2["certifications"]),
      f"(got {[c['normalized'] for c in e2['certifications']]})")
check("PEC + OHSAS + CE Mark found",
      all(c in str(e2["certifications"]) for c in ("PEC", "OHSAS", "CE Mark")))
check("numeric date parsed (2026-08-01, DMY)",
      any("2026-08-01" in str(d["normalized"]) for d in e2["deadlines"]),
      f"(got {[d['normalized'] for d in e2['deadlines']]})")
check("Rs 1.2B -> 1200M", any(float(b["normalized"]) == 1200.0 for b in e2["budgets"]),
      f"(got {[b['normalized'] for b in e2['budgets']]})")
check("Section + Annexure refs", "Section 3.1" in str(e2["clause_refs"])
      and any("Annex" in r["value"] for r in e2["clause_refs"]))

# ── nearest_clause_ref — same-sentence citation beats distant header ────────
budget_pos = e2["budgets"][0]["position"]
check("budget cites Annexure A (same-sentence ref)",
      nearest_clause_ref(e2, budget_pos) == "Annexure A",
      f"(got {nearest_clause_ref(e2, budget_pos)})")

# ── Prompt block ─────────────────────────────────────────────────────────────
block = format_for_prompt(e)
check("prompt block mentions all kinds",
      all(s in block for s in ("Deadlines", "Budgets", "Percentages", "Certifications", "Clause")))
print("\n--- prompt block sample ---")
print(block)

# ── Empty input ──────────────────────────────────────────────────────────────
e3 = extract_entities("")
check("empty text -> empty entity lists", all(not v for v in e3.values()))

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
