"""
BidForge AI — Settings Router
==============================
System status + runtime configuration for the Settings page.

Endpoints
---------
GET   /api/settings              → system status, config values, cache stats
PATCH /api/settings              → update DEMO_MODE / DEBUG / MANUAL_BASELINE_HOURS
                                   (applies immediately AND persists to .env)
POST  /api/settings/clear-cache  → delete all cached LLM responses
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.services import capability_store
from app.services.bid_history import get_overall_stats
from app.services.win_model import get_model_insights, is_trained
from app.utils.llm_cache import clear_cache, get_cache_stats

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/settings", tags=["Settings"])

_ENV_PATH = Path(__file__).parent.parent.parent / ".env"

# Only these keys may be edited from the UI — never secrets.
_EDITABLE_KEYS = ("DEMO_MODE", "DEBUG", "MANUAL_BASELINE_HOURS")


class SettingsPatch(BaseModel):
    demo_mode: Optional[bool] = None
    debug: Optional[bool] = None
    manual_baseline_hours: Optional[float] = Field(None, gt=0, le=100)


def _persist_env(updates: dict[str, str]) -> None:
    """
    Update (or append) the given keys in .env so changes survive restarts.
    Touches ONLY the editable keys — secrets are never read into responses.
    """
    try:
        lines = _ENV_PATH.read_text(encoding="utf-8").splitlines() if _ENV_PATH.exists() else []
        for key, value in updates.items():
            if key not in _EDITABLE_KEYS:
                continue
            pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
            replaced = False
            for i, line in enumerate(lines):
                if pattern.match(line):
                    lines[i] = f"{key}={value}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"{key}={value}")
        _ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist settings to .env: %s", exc)


def _payload() -> dict:
    stats = get_overall_stats()
    model = get_model_insights() if is_trained() else None
    return {
        "config": {
            "demo_mode"            : os.getenv("DEMO_MODE", "false").lower() == "true",
            "debug"                : os.getenv("DEBUG", "false").lower() == "true",
            "manual_baseline_hours": float(os.getenv("MANUAL_BASELINE_HOURS", "6.0")),
            "groq_model"           : "llama-3.3-70b-versatile",
            "groq_key_set"         : bool(os.getenv("GROQ_API_KEY")),
            "supabase_configured"  : bool(os.getenv("SUPABASE_URL")),
            "openrouter_configured": bool(os.getenv("OPENROUTER_API_KEY")),
            "openrouter_model"     : os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free"),
        },
        "system": {
            "capabilities_indexed": capability_store.capability_count(),
            "capability_source"   : capability_store.loaded_source(),
            "bid_history_rows"    : stats.get("total_bids", 0),
            "win_model_trained"   : is_trained(),
            "win_model_accuracy"  : model.get("train_accuracy") if model else None,
        },
        "cache": get_cache_stats(),
    }


@router.get("", summary="Get system status and configuration")
async def get_settings():
    return _payload()


@router.patch(
    "",
    summary="Update runtime configuration",
    description=(
        "Updates DEMO_MODE / DEBUG / MANUAL_BASELINE_HOURS. Changes apply "
        "immediately (env vars are read at call time) and are persisted to "
        ".env so they survive restarts. Secrets cannot be edited here."
    ),
)
async def patch_settings(body: SettingsPatch):
    updates: dict[str, str] = {}
    if body.demo_mode is not None:
        value = "true" if body.demo_mode else "false"
        os.environ["DEMO_MODE"] = value
        updates["DEMO_MODE"] = value
    if body.debug is not None:
        value = "true" if body.debug else "false"
        os.environ["DEBUG"] = value
        updates["DEBUG"] = value
    if body.manual_baseline_hours is not None:
        value = str(body.manual_baseline_hours)
        os.environ["MANUAL_BASELINE_HOURS"] = value
        updates["MANUAL_BASELINE_HOURS"] = value

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No editable settings provided.",
        )

    _persist_env(updates)
    logger.info("Settings updated: %s", updates)
    payload = _payload()
    payload["message"] = "Settings saved."
    return payload


@router.post("/clear-cache", summary="Delete all cached LLM responses")
async def clear_llm_cache():
    removed = clear_cache()
    payload = _payload()
    payload["message"] = f"Cache cleared — {removed} entries removed."
    return payload
