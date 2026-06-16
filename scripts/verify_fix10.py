"""
Fix 10 verification — cache-first LLM layer (fake LLM, no network).
Run:  venv\\Scripts\\python.exe scripts\\verify_fix10.py
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)

# Isolate the cache dir BEFORE importing call sites
import app.utils.llm_cache as lc  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="bidforge_cache_test_"))
lc.CACHE_DIR = _tmp

from app.utils.llm_cache import (  # noqa: E402
    cache_key,
    cached_invoke,
    cached_stream,
    demo_fallback,
    get_cache_stats,
    normalize_prompt,
)

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        failures.append(label)


class Msg:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    model_name = "llama-3.3-70b-versatile"

    def __init__(self, response="live response", fail=False):
        self.response, self.fail, self.calls = response, fail, 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Error code: 429 - rate_limit_exceeded")
        return Msg(self.response)

    async def astream(self, messages):
        self.calls += 1
        if self.fail:
            raise RuntimeError("connection refused")
        for word in self.response.split(" "):
            yield Msg(word + " ")


async def run():
    # ── 1. Key normalization ─────────────────────────────────────────────────
    check("whitespace runs collapse to same key",
          cache_key("m", "hello   world\n\n x") == cache_key("m", "hello world x"))
    check("case is NOT folded",
          cache_key("m", "Hello") != cache_key("m", "hello"))
    check("model is part of the key",
          cache_key("m1", "hello") != cache_key("m2", "hello"))
    check("normalize preserves case", normalize_prompt("  Hello   World ") == "Hello World")

    # ── 2. Miss → live call → write; second call is a hit ───────────────────
    llm = FakeLLM("the live answer")
    msgs = [Msg("system prompt"), Msg("extract things from this document")]
    r1 = await cached_invoke("extract_requirements", llm, msgs)
    r2 = await cached_invoke("extract_requirements", llm, msgs)
    check("live result returned", r1 == "the live answer")
    check("second call served from cache (1 LLM call total)", llm.calls == 1 and r2 == r1)

    # ── 3. DEMO fallback: dead GROQ + same tag → nearest cached ─────────────
    os.environ["DEMO_MODE"] = "true"
    similar_prompt = "system prompt\nextract things from this documents please"
    fb = demo_fallback("extract_requirements", similar_prompt)
    check("nearest cached served for same tag", fb == "the live answer", f"(got {fb!r})")
    check("different tag has no fallback", demo_fallback("compliance_match", similar_prompt) is None)

    os.environ["DEMO_MODE"] = "false"
    check("fallback disabled when DEMO_MODE=false",
          demo_fallback("extract_requirements", similar_prompt) is None)

    # ── 4. Streaming: live accumulates + caches; replay on hit ──────────────
    sllm = FakeLLM("alpha beta gamma delta")
    smsgs = [Msg("draft sys"), Msg("draft section about roads")]
    live_tokens = [t async for t in cached_stream("draft_section", sllm, smsgs)]
    cached_tokens = [t async for t in cached_stream("draft_section", sllm, smsgs)]
    check("live stream yields tokens", "".join(live_tokens).strip() == "alpha beta gamma delta")
    check("cached replay matches (1 stream call total)",
          sllm.calls == 1 and "".join(cached_tokens).strip() == "alpha beta gamma delta")
    check("replay is chunked (typing effect)", len(cached_tokens) >= 2)

    # ── 5. Streaming failure in DEMO_MODE → nearest cached stream ───────────
    os.environ["DEMO_MODE"] = "true"
    dead = FakeLLM(fail=True)
    dmsgs = [Msg("draft sys"), Msg("draft section about roadways")]   # near-identical
    rescued = [t async for t in cached_stream("draft_section", dead, dmsgs)]
    check("dead GROQ stream rescued from nearest cache",
          "".join(rescued).strip() == "alpha beta gamma delta")

    # dead GROQ, demo off → raises
    os.environ["DEMO_MODE"] = "false"
    raised = False
    try:
        _ = [t async for t in cached_stream("draft_section", dead, [Msg("x"), Msg("totally new")])]
    except RuntimeError:
        raised = True
    check("dead GROQ without demo mode raises", raised)

    # ── 6. Stats ──────────────────────────────────────────────────────────────
    stats = get_cache_stats()
    t = stats["totals"]
    check("stats: hits/misses/live/fallbacks tracked",
          t["hits"] >= 2 and t["misses"] >= 2 and t["live_calls"] >= 2 and t["fallbacks"] >= 2,
          f"(totals={t})")
    check("per-tag stats present",
          "extract_requirements" in stats["per_tag"] and "draft_section" in stats["per_tag"])
    check("entry count matches files",
          stats["cached_entries"] == len(list(_tmp.glob("*.json"))))


asyncio.run(run())
shutil.rmtree(_tmp, ignore_errors=True)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
