"""
Fix 6 verification — batched compliance matching (mocked LLM, no network).
Run:  venv\\Scripts\\python.exe scripts\\verify_fix6.py
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.WARNING)

import app.services.compliance_matcher as cm  # noqa: E402

failures = []


def check(label, condition, detail=""):
    print(f"[{'PASS' if condition else 'FAIL'}] {label} {detail}")
    if not condition:
        failures.append(label)


# ── Fake hybrid retrieval: two candidates per requirement ────────────────────
def fake_search(query, top_k=3, issuer_type=None):
    return [
        {"id": "CAP-001", "domain": "Cybersecurity", "summary": "s1", "certification": "ISO 27001",
         "client_type": "Federal Govt", "year_completed": 2023,
         "_dense": 0.5, "_structured": 0.9, "_structured_sub": {}, "_hybrid": 0.7, "_rank": 1},
        {"id": "CAP-002", "domain": "Hospital IT", "summary": "s2", "certification": None,
         "client_type": "Private Sector", "year_completed": 2021,
         "_dense": 0.4, "_structured": 0.6, "_structured_sub": {}, "_hybrid": 0.5, "_rank": 2},
    ]


def reqs(n):
    return [{"id": f"req-{i}", "requirement": f"Requirement number {i} text.", "type": "mandatory"}
            for i in range(n)]


cm.search_capabilities_hybrid = fake_search

# ── Scenario 1: happy path, verdicts shuffled within each batch ─────────────
calls = []

async def good_llm(messages):
    prompt = messages[1].content
    ids = [line.split(": ")[1] for line in prompt.splitlines() if line.startswith("requirement_id")]
    calls.append(ids)
    verdicts = [{
        "requirement_id": rid,
        "status": "pass" if int(rid.split("-")[1]) % 3 else "fail",
        "matched_capability_id": "CAP-001",
        "confidence_adjustment": 0.05 if int(rid.split("-")[1]) % 2 else -0.3,  # -0.3 must clamp
        "gap_note": None if int(rid.split("-")[1]) % 3 else "No ISO 27001 project in Logistics after 2022",
    } for rid in ids]
    verdicts.reverse()   # shuffled order — must map by requirement_id
    return json.dumps(verdicts)

cm._invoke_with_backoff = good_llm
result = asyncio.run(cm.match_requirements("ws", reqs(20)))

check("20 reqs -> 4 batches", len(calls) == 4, f"(got {len(calls)}: sizes {[len(c) for c in calls]})")
check("batch sizes 6/6/6/2", [len(c) for c in calls] == [6, 6, 6, 2])
check("exactly one item per requirement", result.total == 20)
ids_covered = {i["requirement_id"] for i in result.items}
check("all requirement ids covered", ids_covered == {f"req-{i}" for i in range(20)})

by_rid = {i["requirement_id"]: i for i in result.items}
check("confidence = hybrid + adj (0.7+0.05)", by_rid["req-1"]["confidence"] == 0.75,
      f"(got {by_rid['req-1']['confidence']})")
check("negative adjustment clamped to -0.1 (0.7-0.1)", by_rid["req-2"]["confidence"] == 0.6,
      f"(got {by_rid['req-2']['confidence']})")
check("fail items keep specific gap_note", "ISO 27001" in (by_rid["req-0"]["gap_note"] or ""))
check("pass items have null gap_note", by_rid["req-1"]["gap_note"] is None)
check("counts add up", result.pass_count + result.fail_count + result.partial_count == 20)
check("confidence within [0,1]", all(0 <= i["confidence"] <= 1 for i in result.items))

# ── Scenario 2: unknown capability id falls back to top candidate ────────────
async def unknown_cap_llm(messages):
    prompt = messages[1].content
    ids = [line.split(": ")[1] for line in prompt.splitlines() if line.startswith("requirement_id")]
    return json.dumps([{"requirement_id": rid, "status": "pass",
                        "matched_capability_id": "CAP-999",
                        "confidence_adjustment": 0, "gap_note": None} for rid in ids])

cm._invoke_with_backoff = unknown_cap_llm
r2 = asyncio.run(cm.match_requirements("ws", reqs(3)))
check("unknown cap id -> top candidate", all(i["matched_capability_id"] == "CAP-001" for i in r2.items))

# ── Scenario 3: markdown fences stripped ─────────────────────────────────────
async def fenced_llm(messages):
    prompt = messages[1].content
    ids = [line.split(": ")[1] for line in prompt.splitlines() if line.startswith("requirement_id")]
    body = json.dumps([{"requirement_id": rid, "status": "partial",
                        "matched_capability_id": "CAP-002",
                        "confidence_adjustment": 0.1,
                        "gap_note": "Only partial domain coverage in Hospital IT"} for rid in ids])
    return f"```json\n{body}\n```"

cm._invoke_with_backoff = fenced_llm
r3 = asyncio.run(cm.match_requirements("ws", reqs(2)))
check("fenced JSON parsed", r3.partial_count == 2)
check("CAP-002 hybrid 0.5 + 0.1", all(i["confidence"] == 0.6 for i in r3.items))

# ── Scenario 4: unparseable twice -> partial 'auto-review needed' ────────────
attempts = {"n": 0}

async def garbage_llm(messages):
    attempts["n"] += 1
    return "I think the company looks pretty good overall!"

cm._invoke_with_backoff = garbage_llm
r4 = asyncio.run(cm.match_requirements("ws", reqs(2)))
check("strict-JSON retry attempted (2 calls)", attempts["n"] == 2, f"(got {attempts['n']})")
check("degraded items are partial", r4.partial_count == 2)
check("degraded gap_note = auto-review needed",
      all(i["gap_note"] == "auto-review needed" for i in r4.items))
check("degraded confidence = top hybrid", all(i["confidence"] == 0.7 for i in r4.items))

# ── Scenario 5: missing gap_note on non-pass gets a fallback note ────────────
async def lazy_llm(messages):
    prompt = messages[1].content
    ids = [line.split(": ")[1] for line in prompt.splitlines() if line.startswith("requirement_id")]
    return json.dumps([{"requirement_id": rid, "status": "fail",
                        "matched_capability_id": None,
                        "confidence_adjustment": 0, "gap_note": None} for rid in ids])

cm._invoke_with_backoff = lazy_llm
r5 = asyncio.run(cm.match_requirements("ws", reqs(1)))
check("non-pass without gap_note gets fallback note",
      r5.items[0]["gap_note"] is not None and "manual check" in r5.items[0]["gap_note"])

print()
if failures:
    print(f"RESULT: {len(failures)} FAILED -> {failures}")
    sys.exit(1)
print("RESULT: ALL CHECKS PASSED")
