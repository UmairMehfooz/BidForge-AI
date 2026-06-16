"""Load the embedding model + FAISS index, then hold so RSS can be sampled."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.services.capability_store import search_capabilities_hybrid

search_capabilities_hybrid("ISO 27001 cybersecurity operations", top_k=3)
print("READY", flush=True)
time.sleep(20)
