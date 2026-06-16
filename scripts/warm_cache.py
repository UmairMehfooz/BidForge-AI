"""
BidForge AI — Cache Warmer (Fix 10)
====================================
Runs the full pipeline against a workspace through the LOCAL API so every
GROQ call lands in app/data/cache/ (cache writes happen in all modes).
Run this the night before the demo, then set DEMO_MODE=true — the demo PDF
will replay entirely from cache even with no network / a dead GROQ key.

Usage:
    venv\\Scripts\\python.exe scripts\\warm_cache.py <workspace_id> [--base http://127.0.0.1:8000]

The server must be running with DEMO_MODE=false (live calls allowed) and
DEBUG=true (so cache-stats can be printed at the end).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

# Same key function as the runtime cache — imported, never duplicated.
from app.utils.llm_cache import CACHE_DIR, cache_key, normalize_prompt  # noqa: E402

TIMEOUT = httpx.Timeout(600.0)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    workspace_id = sys.argv[1]
    base = sys.argv[3] if len(sys.argv) > 3 and sys.argv[2] == "--base" else "http://127.0.0.1:8000"

    # Sanity: the shared key function behaves as the cache expects
    sample = cache_key("llama-3.3-70b-versatile", "warm  cache\n probe")
    assert sample == cache_key("llama-3.3-70b-versatile", "warm cache probe"), (
        "normalize_prompt/cache_key mismatch — whitespace runs must collapse"
    )

    before = len(list(CACHE_DIR.glob("*.json")))
    print(f"Cache entries before: {before}")

    with httpx.Client(base_url=base, timeout=TIMEOUT) as client:
        for stage, method, path in [
            ("parse",  "POST", f"/api/workspaces/{workspace_id}/parse"),
            ("match",  "POST", f"/api/workspaces/{workspace_id}/match"),
            ("draft",  "POST", f"/api/workspaces/{workspace_id}/draft"),
            ("score",  "POST", f"/api/workspaces/{workspace_id}/score"),
        ]:
            started = time.perf_counter()
            print(f"  {stage} … ", end="", flush=True)
            try:
                if stage == "draft":
                    # SSE — consume the stream to completion
                    with client.stream(method, path) as response:
                        response.raise_for_status()
                        for _ in response.iter_bytes():
                            pass
                else:
                    response = client.request(method, path)
                    response.raise_for_status()
                print(f"OK ({time.perf_counter() - started:.1f}s)")
            except Exception as exc:
                print(f"FAILED: {exc}")

        try:
            stats = client.get("/api/debug/cache-stats").json()
            print(f"\nCache stats: {stats}")
        except Exception:
            print("\n(cache-stats unavailable — set DEBUG=true to enable)")

    after = len(list(CACHE_DIR.glob("*.json")))
    print(f"Cache entries after: {after}  (+{after - before})")
    print("\nNow set DEMO_MODE=true in .env and restart the server for demo day.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
