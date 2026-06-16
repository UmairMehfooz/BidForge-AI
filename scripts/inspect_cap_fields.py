import json
from collections import Counter
from pathlib import Path

recs = json.loads((Path(__file__).parent.parent / "app/data/capability_library.json").read_text(encoding="utf-8"))
for field in ("domain", "certification", "client_type", "year_completed"):
    print(field, dict(Counter(str(r.get(field)) for r in recs)))
