import json
from pathlib import Path

recs = json.loads((Path(__file__).parent.parent / "app/data/capability_library.json").read_text(encoding="utf-8"))
lengths = sorted(len(r.get("summary") or "") for r in recs)
print(f"records={len(recs)}  min={lengths[0]}  median={lengths[len(lengths)//2]}  max={lengths[-1]}")
print(f"under 200 chars: {sum(1 for n in lengths if n <= 200)}")
distinct = len(set(r.get("summary") for r in recs))
print(f"distinct summaries: {distinct}")
for r in recs[::10]:
    print(f"\n{r['id']} ({r['domain']}, {len(r.get('summary') or '')} chars): {r.get('summary')}")
