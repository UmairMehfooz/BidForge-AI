"""
Provider failover verification — GROQ → OpenRouter → cache (fakes, no network).
Run:  venv\\Scripts\\python.exe scripts\\verify_provider_fallback.py
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.ERROR)

import app.utils.llm_cache as lc  # noqa: E402
import app.services.llm_fallback as lf  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="bidforge_failover_test_"))
lc.CACHE_DIR = _tmp

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        failures.append(label)


class Msg:
    def __init__(self, content):
        self.content = content


class FakeLLM:
    def __init__(self, name, response="", fail=False):
        self.model_name, self.response, self.fail = name, response, fail
        self.calls = 0
        self.temperature, self.max_tokens = 0.0, 512

    async def ainvoke(self, messages):
        self.calls += 1
        if self.fail:
            raise RuntimeError("429 rate_limit")
        return Msg(self.response)

    async def astream(self, messages):
        self.calls += 1
        if self.fail:
            raise RuntimeError("connection refused")
        for w in self.response.split(" "):
            yield Msg(w + " ")


async def run():
    primary_dead = FakeLLM("groq-llama", fail=True)
    openrouter = FakeLLM("openrouter-llama", response="answer from openrouter")

    # Patch the fallback factory
    import app.services.llm_fallback as fallback_module
    lf_orig = fallback_module.get_fallback_llms
    fallback_module.get_fallback_llms = lambda primary=None: [("fake-or", openrouter)]
    os.environ["OPENROUTER_API_KEY"] = "test"

    # ── 1. invoke: primary dies → OpenRouter serves → response cached ────────
    msgs = [Msg("sys"), Msg("extract requirements from this tender")]
    r = await lc.cached_invoke("extract_requirements", primary_dead, msgs)
    check("invoke failover serves OpenRouter response", r == "answer from openrouter")
    check("OpenRouter called once", openrouter.calls == 1)

    r2 = await lc.cached_invoke("extract_requirements", primary_dead, msgs)
    check("second call is a cache hit (no extra calls)",
          r2 == "answer from openrouter" and openrouter.calls == 1)

    # ── 2. stream: primary dies before yielding → OpenRouter streams ─────────
    sprimary = FakeLLM("groq-llama", fail=True)
    sopenrouter = FakeLLM("openrouter-llama", response="streamed fallback text")
    fallback_module.get_fallback_llms = lambda primary=None: [("fake-or", sopenrouter)]
    smsgs = [Msg("sys"), Msg("draft a section about bridges")]
    tokens = [t async for t in lc.cached_stream("draft_section", sprimary, smsgs)]
    check("stream failover yields OpenRouter tokens",
          "".join(tokens).strip() == "streamed fallback text")

    # ── 3. both providers dead, demo off → raises (rotation exhausted) ──────
    os.environ["DEMO_MODE"] = "false"
    fallback_module.get_fallback_llms = lambda primary=None: [
        ("or-dead-1", FakeLLM("d1", fail=True)),
        ("or-dead-2", FakeLLM("d2", fail=True)),
    ]
    raised = False
    try:
        await lc.cached_invoke("compliance_match", FakeLLM("g", fail=True), [Msg("s"), Msg("new prompt")])
    except RuntimeError:
        raised = True
    check("both dead + demo off raises", raised)

    # ── 4. both dead, demo on → nearest cached for the tag ───────────────────
    os.environ["DEMO_MODE"] = "true"
    fb = lc.demo_fallback("extract_requirements", "extract requirements from this tender pls")
    check("both dead + demo on serves cached", fb == "answer from openrouter")

    # ── 5. no OpenRouter key → original error propagates ────────────────────
    fallback_module.get_fallback_llms = lambda primary=None: []
    raised = False
    try:
        await lc.cached_invoke("infer_profile", FakeLLM("g", fail=True), [Msg("s"), Msg("another new one")])
    except RuntimeError:
        raised = True
    check("no fallback configured -> primary error propagates", raised)

    # ── 6. stats track provider fallbacks ────────────────────────────────────
    stats = lc.get_cache_stats()
    check("provider_fallbacks counted", stats["totals"]["provider_fallbacks"] >= 2,
          f"(got {stats['totals']['provider_fallbacks']})")

    fallback_module.get_fallback_llms = lf_orig


asyncio.run(run())
shutil.rmtree(_tmp, ignore_errors=True)
os.environ.pop("OPENROUTER_API_KEY", None)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
