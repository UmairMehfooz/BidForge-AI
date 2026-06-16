import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.capability_store import search_capabilities, search_capabilities_hybrid

for q in ["Bidder must hold ISO 27001 certification for cloud infrastructure services",
          "Construction of 25km dual carriageway road"]:
    dense = search_capabilities(q, top_k=10)
    hybrid = search_capabilities_hybrid(q, top_k=5)
    ds = [r["_similarity"] for r in dense]
    hs = [r["_hybrid"] for r in hybrid]
    print(q[:50])
    print(f"  dense  top1-top3={ds[0]-ds[2]:.4f}  top1-top5={ds[0]-ds[4]:.4f}  top1-top10={ds[0]-ds[9]:.4f}")
    print(f"  hybrid top1-top3={hs[0]-hs[2]:.4f}  top1-top5={hs[0]-hs[4]:.4f}")
    print(f"  domains in hybrid top3: {[r['domain'] for r in hybrid[:3]]}")
