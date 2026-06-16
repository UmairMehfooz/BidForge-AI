"""Fix 4 — merge logic check (no LLM): tagging + appending missed entities."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.ner_extractor import extract_entities
from app.services.requirement_extractor import _merge_ner_findings

text = ("Proposals must be submitted no later than 15 March 2026 per Clause 2.1. "
        "Budget ceiling is PKR 250M as stated in Section 5. "
        "Bidder must hold ISO 27001 per Clause 4.2.")
entities = extract_entities(text)

# Simulated LLM output: caught the cert requirement, MISSED deadline + budget
llm_reqs = [{
    "id": "r1", "workspace_id": "ws", "section_ref": "4.2",
    "requirement": "Bidder must hold ISO 27001 certification.",
    "type": "mandatory", "deadline": None, "budget_ref": None,
    "extraction_source": "llm",
}, {
    "id": "r2", "workspace_id": "ws", "section_ref": "General",
    "requirement": "Vendor staff must be fluent in English.",
    "type": "mandatory", "deadline": None, "budget_ref": None,
    "extraction_source": "llm",
}]

merged = _merge_ner_findings(llm_reqs, entities, "ws")
for r in merged:
    print(f"  [{r['extraction_source']:>4}] type={r['type']:<20} section={r['section_ref']:<12} {r['requirement'][:70]}")

assert merged[0]["extraction_source"] == "both", "cert req should be 'both'"
assert merged[1]["extraction_source"] == "llm", "unrelated req should stay 'llm'"
ner_added = [r for r in merged if r["extraction_source"] == "ner"]
assert len(ner_added) == 2, f"expected 2 appended, got {len(ner_added)}"
deadline_req = next(r for r in ner_added if r["type"] == "submission_deadline")
assert deadline_req["deadline"] == "2026-03-15", deadline_req["deadline"]
assert deadline_req["section_ref"] == "Clause 2.1", deadline_req["section_ref"]
budget_req = next(r for r in ner_added if r["type"] == "budget")
assert budget_req["budget_ref"] == "PKR 250M", budget_req["budget_ref"]
assert budget_req["section_ref"] == "Section 5", budget_req["section_ref"]
print("\nRESULT: MERGE LOGIC OK")
