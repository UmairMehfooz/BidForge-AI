"""
BidForge AI — Supabase client singleton.

Import and use `supabase_client` throughout the app.
The client is initialised lazily on first use so the app can still start
even if SUPABASE_URL / SUPABASE_KEY are missing (useful for offline dev).
"""

from __future__ import annotations

import os

from supabase import Client, create_client

_client: Client | None = None


def get_supabase() -> Client:
    """Return the Supabase client, creating it on first call."""
    global _client
    if _client is None:
        url = os.getenv("SUPABASE_URL", "")
        key = os.getenv("SUPABASE_KEY", "")
        if not url or not key:
            raise EnvironmentError(
                "SUPABASE_URL and SUPABASE_KEY must be set in your .env file."
            )
        _client = create_client(url, key)
    return _client
