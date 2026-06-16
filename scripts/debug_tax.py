import sys
sys.path.insert(0, ".")
import numpy as np
from app.services.criteria_taxonomy import get_taxonomy, _entry_embedding_text
from app.services.capability_store import embed_texts

tax = get_taxonomy()
vecs = embed_texts([_entry_embedding_text(e) for e in tax])
for q in ["Marks awarded for how cheap the offer is compared to others",
          "Points given for novel ideas that improve the outcome"]:
    qv = embed_texts([q])[0]
    sims = vecs @ qv
    order = np.argsort(-sims)[:3]
    print(q)
    for i in order:
        print(f"   {tax[i]['name']:<35} {sims[i]:.3f}")
