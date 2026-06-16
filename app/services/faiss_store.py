"""
BidForge AI — FAISS Capability Store (compatibility wrapper)

Historically this module held its own SentenceTransformer + FAISS index,
duplicating app/services/capability_store.py (two model loads, two indexes).
It is now a thin delegate over that single store — same public surface
(`capability_store.load() / count() / search() / get_by_id() / all_records()`)
so main.py and the routers keep working unchanged.

Usage (anywhere in the app):
    from app.services.faiss_store import capability_store
    results = capability_store.search(query_text, top_k=3)
"""

from __future__ import annotations

from typing import Any

from app.services import capability_store as _store


class CapabilityStore:
    """Delegates to the module-level store in app/services/capability_store.py."""

    def load(self) -> None:
        """Initialise the shared store (records + embeddings + FAISS index)."""
        _store.ensure_loaded()
        print(
            f"  [OK]    Capability store ready: {_store.capability_count()} records "
            f"(source={_store.loaded_source()})"
        )

    def count(self) -> int:
        return _store.capability_count()

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Dense search. Results carry `_score` (cosine) plus `_rank`."""
        results = _store.search_capabilities(query, top_k=top_k)
        for r in results:
            r["_score"] = r.get("_similarity", 0.0)
        return results

    def get_by_id(self, cap_id: str) -> dict[str, Any] | None:
        return _store.get_capability_by_id(cap_id)

    def all_records(self) -> list[dict[str, Any]]:
        return _store.get_all_capabilities()


# Singleton — imported and reused everywhere in the app
capability_store = CapabilityStore()
