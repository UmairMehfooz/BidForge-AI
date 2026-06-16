"""
BidForge AI — FastAPI Backend
Entry point: uvicorn app.main:app --reload
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.routers import bid_history, capabilities, settings, workspaces
from app.services.bid_history import get_overall_stats, load_bid_history
from app.services.faiss_store import capability_store
from app.services.win_model import is_trained, train_win_model

# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown logic
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Startup: initialise the FAISS capability store (in-memory singleton).
    Shutdown: nothing to clean up — FAISS is pure in-memory.
    """
    print("[START]  BidForge AI — starting up...")
    capability_store.load()
    print(f"FAISS store ready — {capability_store.count()} capabilities indexed.")

    df = load_bid_history()
    if df is not None:
        stats = get_overall_stats()
        print(
            f"Bid history loaded: {len(df)} rows — "
            f"overall win rate {stats['overall_win_rate']:.1%}."
        )
    if train_win_model():
        print("Win model trained (LogisticRegression on historical bids).")
    else:
        print("Win model unavailable — scoring will use pure heuristics.")
    yield
    print("[STOP]   BidForge AI — shutting down.")


# ---------------------------------------------------------------------------
# App instance
# ---------------------------------------------------------------------------
app = FastAPI(
    title="BidForge AI",
    description=(
        "AI-powered Bid & Proposal Response Engine — "
        "CUST Hackathon 2026, Problem #1 (TEKROWE)"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allow Vite dev server and any Vercel preview URL
# ---------------------------------------------------------------------------
origins = [
    "http://localhost:5173",       # Vite dev
    "http://localhost:3000",       # CRA / fallback
    "https://*.vercel.app",        # Vercel preview / production
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(workspaces.router, prefix="/api")
app.include_router(bid_history.router, prefix="/api")
app.include_router(capabilities.router, prefix="/api")
app.include_router(settings.router, prefix="/api")

# ---------------------------------------------------------------------------
# Core endpoints
# ---------------------------------------------------------------------------
@app.get("/health", tags=["Health"])
async def health_check():
    """Liveness probe — confirms the service is running."""
    return JSONResponse(
        content={
            "status": "ok",
            "service": "BidForge AI",
            "faiss_capabilities": capability_store.count(),
            "win_model_trained": is_trained(),
            "demo_mode": os.getenv("DEMO_MODE", "false").lower() == "true",
        }
    )


@app.get("/api/debug/match", tags=["Debug"])
async def debug_match(q: str):
    """
    Hybrid retrieval inspector (Fix 3) — top-5 candidates with dense,
    structured, and hybrid scores side by side. Enabled only when DEBUG=true.
    """
    if os.getenv("DEBUG", "false").lower() != "true":
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found.")

    from app.services.capability_store import search_capabilities_hybrid

    results = search_capabilities_hybrid(q, top_k=5)
    return {
        "query": q,
        "candidates": [
            {
                "rank"          : r["_rank"],
                "id"            : r.get("id"),
                "domain"        : r.get("domain"),
                "certification" : r.get("certification"),
                "year_completed": r.get("year_completed"),
                "client_type"   : r.get("client_type"),
                "dense"         : r["_dense"],
                "structured"    : r["_structured"],
                "structured_sub": r["_structured_sub"],
                "hybrid"        : r["_hybrid"],
            }
            for r in results
        ],
    }


@app.get("/api/debug/cache-stats", tags=["Debug"])
async def debug_cache_stats():
    """
    LLM cache statistics (Fix 10) — hits, misses, live calls, fallbacks,
    per call-site tag. Enabled only when DEBUG=true.
    """
    if os.getenv("DEBUG", "false").lower() != "true":
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Not found.")

    from app.utils.llm_cache import get_cache_stats
    return get_cache_stats()


@app.get("/api/test-stream", tags=["Debug"])
async def test_stream():
    """
    SSE smoke-test endpoint (Task 10 — Gate Test).
    Returns a plain JSON confirmation that SSE infrastructure is reachable.
    The actual SSE stream is tested via /api/workspaces/{id}/draft.
    """
    from fastapi.responses import StreamingResponse
    import asyncio

    async def event_generator():
        for i in range(1, 6):
            yield f"data: SSE token {i} — BidForge AI stream OK\n\n"
            await asyncio.sleep(0.3)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # disable Nginx buffering on Railway/Render
        },
    )
