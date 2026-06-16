import json
from pathlib import Path

p = Path(__file__).parent.parent / "app/data/capability_library_enriched.json"
recs = json.loads(p.read_text(encoding="utf-8"))
done = [r for r in recs if len(r.get("summary") or "") > 200]
print(f"enriched: {len(done)}/{len(recs)}, distinct: {len(set(r['summary'] for r in done))}")
lengths = sorted(len(r["summary"]) for r in done)
print(f"lengths: min={lengths[0]} median={lengths[len(lengths)//2]} max={lengths[-1]}")
starts = {}
for r in done:
    w = r["summary"].split()[0]
    starts[w] = starts.get(w, 0) + 1
print("first words:", dict(sorted(starts.items(), key=lambda kv: -kv[1])))
for r in done[10:13]:
    print(f"\n{r['id']} ({r['domain']}): {r['summary'][:300]}")
