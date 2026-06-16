"""
BidForge AI — Fallback LLM Provider (OpenRouter)
=================================================
When the primary GROQ call fails (rate limit, outage), the cache layer
retries the same messages against OpenRouter before degrading to cached
responses. Failover chain for every LLM call site:

    GROQ  →  OpenRouter (this module)  →  nearest cached (DEMO_MODE)  →  error

Configuration (.env):
    OPENROUTER_API_KEY   — get one free at https://openrouter.ai/keys
    OPENROUTER_MODEL     — default meta-llama/llama-3.3-70b-instruct:free
                           (the SAME model the app prompts on Groq, so the
                           failover is prompt-compatible with zero surprises)

Other solid free choices: deepseek/deepseek-chat:free (strongest),
google/gemini-2.0-flash-exp:free (fastest).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Free models are individually congestion-prone (shared upstream capacity),
# so the fallback ROTATES through several. Override with a comma-separated
# OPENROUTER_MODEL list to change models or their order.
# Verified against the live OpenRouter catalog (June 2026) — free slugs get
# retired without notice, so the rotation is also overridable via env.
DEFAULT_MODELS = (
    "openai/gpt-oss-120b:free",                 # strong instruction/JSON, 131k ctx
    "qwen/qwen3-next-80b-a3b-instruct:free",    # strong, 262k ctx
    "nvidia/nemotron-3-super-120b-a12b:free",   # 120B MoE, 1M ctx
    "meta-llama/llama-3.3-70b-instruct:free",   # same as Groq primary (often congested)
)


def is_configured() -> bool:
    return bool(os.getenv("OPENROUTER_API_KEY"))


def fallback_models() -> list[str]:
    raw = os.getenv("OPENROUTER_MODEL", "")
    models = [m.strip() for m in raw.split(",") if m.strip()]
    return models or list(DEFAULT_MODELS)


def fallback_model_name() -> str:
    """First model in the rotation — for display/logging."""
    return fallback_models()[0]


def _build_client(model: str, primary_llm: Any = None) -> Any | None:
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        logger.warning("langchain-openai not installed — OpenRouter fallback disabled.")
        return None

    kwargs: dict[str, Any] = {
        "model"   : model,
        "api_key" : api_key,
        "base_url": OPENROUTER_BASE_URL,
        "max_retries": 0,   # rotation handles retries — don't stack waits
        "default_headers": {
            # OpenRouter attribution headers (recommended for free tier)
            "HTTP-Referer": "https://bidforge.local",
            "X-Title"     : "BidForge AI",
        },
    }
    if primary_llm is not None:
        temperature = getattr(primary_llm, "temperature", None)
        if temperature is not None:
            kwargs["temperature"] = temperature
        max_tokens = getattr(primary_llm, "max_tokens", None)
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
    return ChatOpenAI(**kwargs)


def get_fallback_llms(primary_llm: Any = None) -> list[tuple[str, Any]]:
    """(model_name, client) pairs in rotation order. Empty when unconfigured."""
    if not is_configured():
        return []
    clients = []
    for model in fallback_models():
        client = _build_client(model, primary_llm)
        if client is not None:
            clients.append((model, client))
    return clients


def get_fallback_llm(primary_llm: Any = None) -> Any | None:
    """First fallback client (legacy single-model interface)."""
    clients = get_fallback_llms(primary_llm)
    return clients[0][1] if clients else None
