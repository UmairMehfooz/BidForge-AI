"""
BidForge AI — LLM Cache (Fix 10: cache-first, never cache-only)
================================================================
File-based cache shared by every GROQ call site.

Behaviour in ALL modes:
    check cache → hit returns cached → miss calls GROQ live
    → on success write cache, then return.

DEMO_MODE=true changes ONLY two things:
    a. When GROQ fails after the caller's retries, the NEAREST cached
       response for the same call-site tag is returned (logged warning) —
       degraded but never empty. Judge-day insurance.
    b. Cache TTL is ignored (entries have no TTL today; if one is ever
       added, demo mode serves stale).

Cache key   : sha256(model + normalized prompt)
Normalize   : collapse whitespace runs; nothing else is lowercased.
Cache store : app/data/cache/{key}.json — {"tag", "model", "prompt", "response"}
Tags        : each call site passes a tag ("extract_requirements",
              "compliance_match", "infer_profile", "draft_section") used for
              nearest-match fallback and per-tag stats.

Public API
----------
    response = await cached_invoke(tag, llm, messages)        # non-streaming
    async for token in cached_stream(tag, llm, messages): …   # streaming
    fallback = demo_fallback(tag, prompt)                     # after retries
    cache_key(model, prompt) / normalize_prompt(text)         # shared key fn
    get_cache_stats()                                         # debug endpoint
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, AsyncGenerator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cache directory
# ---------------------------------------------------------------------------
CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# How many tokens of cached text to replay per SSE chunk when serving a
# cached draft stream (keeps the typing effect visible in the editor).
_REPLAY_WORDS_PER_CHUNK = 3


def is_demo_mode() -> bool:
    """Read DEMO_MODE at call time (not import time) so .env edits apply."""
    return os.getenv("DEMO_MODE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Key function — shared with scripts/warm_cache.py (import, don't duplicate)
# ---------------------------------------------------------------------------

def normalize_prompt(text: str) -> str:
    """Collapse whitespace runs. Case is preserved deliberately."""
    return re.sub(r"\s+", " ", str(text)).strip()


def cache_key(model: str, prompt: str) -> str:
    raw = f"{model}::{normalize_prompt(prompt)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Stats (in-memory, per process) — served by /api/debug/cache-stats
# ---------------------------------------------------------------------------
_stats: dict[str, dict[str, int]] = defaultdict(
    lambda: {"hits": 0, "misses": 0, "live_calls": 0,
             "provider_fallbacks": 0, "fallbacks": 0}
)


def _bump(tag: str, counter: str) -> None:
    _stats[tag][counter] += 1


def clear_cache() -> int:
    """Delete all cached LLM responses and reset the in-memory index."""
    global _fallback_index
    removed = 0
    for path in CACHE_DIR.glob("*.json"):
        try:
            path.unlink()
            removed += 1
        except Exception as exc:
            logger.warning("Could not delete cache file %s: %s", path.name, exc)
    _fallback_index = None
    logger.info("LLM cache cleared: %d entries removed.", removed)
    return removed


def get_cache_stats() -> dict[str, Any]:
    per_tag = {tag: dict(counters) for tag, counters in _stats.items()}
    totals = {
        k: sum(c[k] for c in _stats.values())
        for k in ("hits", "misses", "live_calls", "provider_fallbacks", "fallbacks")
    }
    return {
        "totals"       : totals,
        "per_tag"      : per_tag,
        "cached_entries": len(list(CACHE_DIR.glob("*.json"))),
        "demo_mode"    : is_demo_mode(),
    }


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

def _read_entry(key: str) -> dict[str, Any] | None:
    path = CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_entry(key: str, tag: str, model: str, prompt: str, response: str) -> None:
    try:
        (CACHE_DIR / f"{key}.json").write_text(
            json.dumps(
                {
                    "tag"     : tag,
                    "model"   : model,
                    # normalized + truncated — only used for nearest-match
                    "prompt"  : normalize_prompt(prompt)[:4000],
                    "response": response,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        _index_entry(tag, prompt, response)
    except Exception as exc:
        logger.warning("Cache write failed for %s: %s", key[:12], exc)


def get_cached(model: str, prompt: str) -> str | None:
    entry = _read_entry(cache_key(model, prompt))
    return entry.get("response") if entry else None


# ---------------------------------------------------------------------------
# DEMO fallback — nearest cached response with the same tag
# ---------------------------------------------------------------------------

# In-memory index of cached entries per tag: {tag: [(prompt, response), ...]}.
# Without it, every rate-limited call re-read the whole cache directory
# (hundreds of files) — under sustained 429s that dominated pipeline time.
_fallback_index: dict[str, list[tuple[str, str]]] | None = None
_MATCH_CHARS = 1500   # chars of prompt used for similarity matching

# Below this similarity, serving a "nearest" cached response does more harm
# than good (it would be a different requirement's answer) — return None and
# let the caller degrade explicitly instead.
_MIN_FALLBACK_SIMILARITY = 0.35


def _get_fallback_index() -> dict[str, list[tuple[str, str]]]:
    global _fallback_index
    if _fallback_index is None:
        index: dict[str, list[tuple[str, str]]] = {}
        for path in CACHE_DIR.glob("*.json"):
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            tag = entry.get("tag")
            if tag:
                index.setdefault(tag, []).append(
                    (entry.get("prompt", "")[:_MATCH_CHARS], entry.get("response", ""))
                )
        _fallback_index = index
        logger.info(
            "Fallback cache index built: %d entries across %d tags.",
            sum(len(v) for v in index.values()), len(index),
        )
    return _fallback_index


def _index_entry(tag: str, prompt: str, response: str) -> None:
    """Keep the in-memory index in sync with new cache writes."""
    if _fallback_index is not None:
        _fallback_index.setdefault(tag, []).append(
            (normalize_prompt(prompt)[:_MATCH_CHARS], response)
        )


def demo_fallback(tag: str, prompt: str) -> str | None:
    """
    DEMO_MODE only: after the caller's retries are exhausted, return the
    cached response whose stored prompt is most similar to `prompt` among
    entries with the same call-site tag. None when demo mode is off or the
    tag has no cached entries.
    """
    if not is_demo_mode():
        return None

    target = normalize_prompt(prompt)[:_MATCH_CHARS]
    best_response, best_ratio = None, 0.0
    for stored_prompt, response in _get_fallback_index().get(tag, []):
        matcher = difflib.SequenceMatcher(None, target, stored_prompt)
        if matcher.real_quick_ratio() <= best_ratio:
            continue
        ratio = matcher.quick_ratio()
        if ratio > best_ratio:
            best_ratio, best_response = ratio, response

    if best_ratio < _MIN_FALLBACK_SIMILARITY:
        if best_response is not None:
            logger.warning(
                "DEMO fallback for tag '%s' rejected — nearest cached entry "
                "only %.2f similar (different requirement).", tag, best_ratio,
            )
        return None

    if best_response is not None:
        _bump(tag, "fallbacks")
        logger.warning(
            "DEMO fallback served for tag '%s' (similarity %.2f) — GROQ was "
            "unavailable; response is the nearest cached equivalent.",
            tag, best_ratio,
        )
    return best_response


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _messages_to_prompt(messages: list[Any]) -> str:
    """Flatten LangChain messages into one cache-keyable string."""
    return "\n".join(str(getattr(m, "content", m)) for m in messages)


def _match_text(messages: list[Any]) -> str:
    """
    Text used for nearest-match similarity: the LAST (user) message only.
    The full prompt starts with the shared system boilerplate, which made
    every prompt of a tag ~95% similar to every other — fallback then served
    the same cached response for completely different requirements.
    """
    last = messages[-1] if messages else ""
    return normalize_prompt(str(getattr(last, "content", last)))[:_MATCH_CHARS]


def _model_name(llm: Any) -> str:
    return str(getattr(llm, "model_name", None) or getattr(llm, "model", "") or "")


# ---------------------------------------------------------------------------
# Non-streaming invoke — cache-first, live on miss, write on success
# ---------------------------------------------------------------------------

async def cached_invoke(tag: str, llm: Any, messages: list[Any]) -> str:
    """
    Cache-first wrapper around llm.ainvoke(messages).
    Raises whatever the LLM raises on a miss — the caller keeps its own
    retry policy and calls demo_fallback() when retries are exhausted.
    """
    model = _model_name(llm)
    prompt = _messages_to_prompt(messages)
    key = cache_key(model, prompt)

    entry = _read_entry(key)
    if entry is not None:
        _bump(tag, "hits")
        logger.debug("Cache HIT [%s] %s…", tag, key[:12])
        return entry["response"]

    _bump(tag, "misses")
    try:
        response = await llm.ainvoke(messages)
        _bump(tag, "live_calls")
    except Exception as primary_exc:
        # Provider failover: rotate through the configured OpenRouter models —
        # free models share congested upstream capacity, so one being busy
        # must not fail the call.
        from app.services.llm_fallback import get_fallback_llms
        fallbacks = get_fallback_llms(llm)
        if not fallbacks:
            raise
        logger.warning(
            "Primary LLM failed for tag '%s' (%s) — rotating through %d OpenRouter models.",
            tag, primary_exc, len(fallbacks),
        )
        response = None
        last_exc: Exception = primary_exc
        for model_name, fallback_llm in fallbacks:
            try:
                response = await fallback_llm.ainvoke(messages)
                logger.info("OpenRouter %s served tag '%s'.", model_name, tag)
                break
            except Exception as fb_exc:
                logger.warning("OpenRouter %s failed: %s", model_name, str(fb_exc)[:200])
                last_exc = fb_exc
        if response is None:
            raise last_exc   # caller's retries / demo fallback take over
        _bump(tag, "provider_fallbacks")

    text = response.content if hasattr(response, "content") else str(response)
    _write_entry(key, tag, model, _match_text(messages), text)
    return text


# ---------------------------------------------------------------------------
# Streaming invoke — replays cached text as tokens on a hit
# ---------------------------------------------------------------------------

async def cached_stream(
    tag: str, llm: Any, messages: list[Any]
) -> AsyncGenerator[str, None]:
    """
    Cache-first wrapper around llm.astream(messages), yielding token strings.

    - Hit  → replay the cached text a few words at a time (the UI still types).
    - Miss → stream live, accumulate, write cache when the stream completes.
    - Live failure (incl. mid-stream) → in DEMO_MODE yield the nearest cached
      response for this tag; otherwise re-raise.
    """
    model = _model_name(llm)
    prompt = _messages_to_prompt(messages)
    key = cache_key(model, prompt)

    entry = _read_entry(key)
    if entry is not None:
        _bump(tag, "hits")
        words = entry["response"].split(" ")
        for i in range(0, len(words), _REPLAY_WORDS_PER_CHUNK):
            yield " ".join(words[i:i + _REPLAY_WORDS_PER_CHUNK]) + " "
        return

    def _token_of(chunk: Any) -> str:
        if hasattr(chunk, "content") and chunk.content:
            return chunk.content
        if isinstance(chunk, str):
            return chunk
        return ""

    _bump(tag, "misses")
    accumulated = ""
    try:
        async for chunk in llm.astream(messages):
            token = _token_of(chunk)
            if token:
                accumulated += token
                yield token
        _bump(tag, "live_calls")
        _write_entry(key, tag, model, _match_text(messages), accumulated)
        return
    except Exception as exc:
        logger.error("Live stream failed for tag '%s': %s", tag, exc)

        # Provider failover — only safe when nothing was yielded yet
        # (restarting mid-stream would duplicate text in the editor).
        if not accumulated:
            from app.services.llm_fallback import get_fallback_llms
            for model_name, fallback_llm in get_fallback_llms(llm):
                logger.warning("Failing over stream for tag '%s' to OpenRouter %s.",
                               tag, model_name)
                try:
                    async for chunk in fallback_llm.astream(messages):
                        token = _token_of(chunk)
                        if token:
                            accumulated += token
                            yield token
                    _bump(tag, "provider_fallbacks")
                    _write_entry(key, tag, model, _match_text(messages), accumulated)
                    return
                except Exception as fb_exc:
                    logger.error("OpenRouter %s stream failed for '%s': %s",
                                 model_name, tag, str(fb_exc)[:200])
                    if accumulated:
                        # Tokens already reached the client — replaying the
                        # cached fallback would duplicate text. Surface the error.
                        raise

        if accumulated:
            raise

        fallback = demo_fallback(tag, _match_text(messages))
        if fallback is None:
            raise
        # Serve the text from the nearest cached response
        words = fallback.split(" ")
        for i in range(0, len(words), _REPLAY_WORDS_PER_CHUNK):
            yield " ".join(words[i:i + _REPLAY_WORDS_PER_CHUNK]) + " "
