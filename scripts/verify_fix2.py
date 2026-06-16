"""
Fix 2 verification — enriched capability library + FAISS similarity spread.
Run:  venv\\Scripts\\python.exe scripts\\verify_fix2.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent
failures = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label} {detail}")
    if not condition:
        failures.append(label)


# ── 1. Enriched file integrity ──────────────────────────────────────────────
enriched_path = ROOT / "app/data/capability_library_enriched.json"
check("enriched file exists", enriched_path.exists())
recs = json.loads(enriched_path.read_text(encoding="utf-8"))
check("50 records", len(recs) == 50, f"(got {len(recs)})")
short = [r["id"] for r in recs if len(r.get("summary") or "") <= 200]
check("all summaries > 200 chars", not short, f"(short: {short})")
check("summaries distinct", len(set(r["summary"] for r in recs)) == len(recs))
raw = json.loads((ROOT / "app/data/capability_library.json").read_text(encoding="utf-8"))
check("raw file untouched (still <= 200 chars)",
      all(len(r.get("summary") or "") <= 200 for r in raw))
check("schema preserved", set(recs[0].keys()) == set(raw[0].keys()))

# ── 2. FAISS store loads the enriched file and scores show spread ───────────
from app.services.faiss_store import capability_store  # noqa: E402

capability_store.load()
check("store loads 50 capabilities", capability_store.count() == 50)
loaded_one = capability_store.get_by_id("CAP-001")
check("store serves enriched summaries", len(loaded_one.get("summary", "")) > 200)

for query in [
    "Bidder must hold ISO 27001 certification for cloud infrastructure services",
    "Construction of 25km dual carriageway road",
]:
    results = capability_store.search(query, top_k=5)
    scores = [r["_score"] for r in results]
    # With all 50 summaries enriched, the same-domain top-3 legitimately
    # cluster — discrimination shows up beyond the cluster (top-1 vs top-5).
    spread = scores[0] - scores[4]
    print(f"\n  query: {query[:60]}...")
    for r in results:
        print(f"    {r['_rank']}. {r['id']} {r['domain']:<22} score={r['_score']:.4f}")
    check("top-1 vs top-5 spread > 0.05", spread > 0.05, f"(spread={spread:.4f})")

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
