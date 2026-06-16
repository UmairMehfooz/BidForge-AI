"""
Fix 8 verification — criteria taxonomy + classifier.
Run:  venv\\Scripts\\python.exe scripts\\verify_fix8.py
"""

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)

from app.services.criteria_taxonomy import (  # noqa: E402
    classify_criterion,
    classify_requirements,
    get_taxonomy,
)

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        failures.append(label)


# ── 1. Taxonomy file ─────────────────────────────────────────────────────────
raw = json.loads((Path(__file__).parent.parent / "app/data/criteria_taxonomy.json").read_text(encoding="utf-8"))
check("valid JSON with exactly 15 entries", len(raw) == 15, f"(got {len(raw)})")
check("all entries have required fields",
      all(set(e) >= {"id", "name", "sector_hint", "typical_weight_range", "keywords"} for e in raw))
check("ids unique", len({e["id"] for e in raw}) == 15)
required_names = {"Technical Approach", "Relevant Experience", "Financial Proposal / Price",
                  "Project Timeline", "Quality Assurance", "Methodology", "Risk Management",
                  "Financial Stability", "Past Performance", "Local Presence"}
names = {e["name"] for e in raw}
check("playbook-required names present", required_names <= names,
      f"(missing: {required_names - names})")
check("get_taxonomy loads 15", len(get_taxonomy()) == 15)

# ── 2. Keyword classification ────────────────────────────────────────────────
cases = {
    "Technical proposal will be evaluated on solution design and architecture: 40%": "Technical Approach",
    "Financial bid carries 30% weight; lowest bid scores highest": "Financial Proposal / Price",
    "CVs of key personnel including the project manager qualifications (15 points)": "Key Personnel / CVs",
    "Bidder must demonstrate similar projects completed in the last 5 years": "Relevant Experience",
    "Health and safety plan and OHSAS compliance will be scored": "HSE (Health, Safety, Environment)",
    "Implementation schedule with milestones: 10%": "Project Timeline",
    "Annual turnover and audited accounts for last 3 years": "Financial Stability",
    "Warranty and after-sales support arrangements": "After-Sales Support",
}
for text, expected in cases.items():
    result = classify_criterion(text)
    got = result["taxonomy_name"] if result else None
    check(f"'{text[:45]}...' -> {expected}", got == expected, f"(got {got})")

# ── 3. Embedding fallback (no keyword overlap on purpose) ────────────────────
fuzzy = classify_criterion("Marks awarded for how cheap the offer is compared to others")
check("embedding fallback classifies price-like text",
      fuzzy is not None and fuzzy["taxonomy_name"] == "Financial Proposal / Price",
      f"(got {fuzzy})")

junk = classify_criterion("xyzzy plugh quux foobar")
check("nonsense stays unclassified", junk is None, f"(got {junk})")

# ── 4. classify_requirements mutates + counts ────────────────────────────────
reqs = [
    {"type": "evaluation_criteria", "requirement": "Technical approach and methodology: 40%"},
    {"type": "evaluation_criteria", "requirement": "Price competitiveness: 30%"},
    {"type": "mandatory", "requirement": "Must hold ISO 27001."},
]
mapped = classify_requirements(reqs)
check("2 of 2 eval criteria mapped", mapped == 2, f"(got {mapped})")
check("eval reqs got taxonomy fields", reqs[0]["taxonomy_id"] and reqs[1]["taxonomy_name"])
check("mandatory reqs untouched", "taxonomy_id" not in reqs[2])

# ── 5. Coverage string in scoring ────────────────────────────────────────────
from app.services.scoring_engine import _taxonomy_coverage  # noqa: E402
reqs[0]["taxonomy_id"] = "TAX-01"
cov = _taxonomy_coverage([
    {"type": "evaluation_criteria", "taxonomy_id": "TAX-01"},
    {"type": "evaluation_criteria", "taxonomy_id": None},
    {"type": "mandatory"},
])
check("coverage string '1/2'", cov == "1/2", f"(got {cov})")

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
