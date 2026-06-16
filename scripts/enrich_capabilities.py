"""
BidForge AI — Capability Library Enrichment (one-time, Fix 2)
==============================================================
The raw capability summaries are too short (~25 words) to produce useful
embeddings — FAISS similarity scores come out nearly identical across all 50
records. This script rewrites each summary into a realistic 80-120 word
past-project narrative via GROQ, grounded in the record's structured fields.

- Batches 5 records per API call to stay under rate limits.
- Retries each batch up to 3 times with exponential backoff (2s/4s/8s).
- Idempotent: records whose summary is already > 200 chars are skipped, so
  re-running only fills in what a previous (failed) run missed.
- Output goes to app/data/capability_library_enriched.json (same schema,
  only `summary` replaced). The original file is never touched.

Run:  venv\\Scripts\\python.exe scripts\\enrich_capabilities.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from groq import Groq

ROOT = Path(__file__).parent.parent
RAW_PATH = ROOT / "app" / "data" / "capability_library.json"
ENRICHED_PATH = ROOT / "app" / "data" / "capability_library_enriched.json"

GROQ_MODEL = "llama-3.3-70b-versatile"
BATCH_SIZE = 5
MAX_ATTEMPTS = 3
ENRICHED_THRESHOLD = 200   # summaries longer than this are considered done

SYSTEM_PROMPT = (
    "You write past-project summaries for a Pakistani systems integrator's "
    "capability library. You always return ONLY a valid JSON array of strings, "
    "one summary per requested project, in the same order — no markdown fences, "
    "no commentary."
)

RECORD_TEMPLATE = """Project {n}:
- Domain: {domain}
- Certification held: {cert}
- Contract value: {value}
- Duration: {duration} months
- Client type: {client_type}
- Year completed: {year}
- Existing short description (expand on this, keep it consistent): {existing}"""

BATCH_INSTRUCTION = """Write a realistic 80-120 word past-project summary for EACH of the {count} projects below.

Each summary must include: scope of work, 2-3 concrete deliverables, one quantified outcome (e.g. uptime %, km of road, users onboarded), and the certification context (if a certification is held). Write in third person, past tense, no company name.

Return ONLY a JSON array of exactly {count} strings, in the same order as the projects.

{records}"""


def build_prompt(batch: list[dict]) -> str:
    blocks = []
    for n, rec in enumerate(batch, start=1):
        blocks.append(RECORD_TEMPLATE.format(
            n=n,
            domain=rec.get("domain"),
            cert=rec.get("certification") or "None",
            value=rec.get("contract_value"),
            duration=rec.get("duration_months"),
            client_type=rec.get("client_type"),
            year=rec.get("year_completed"),
            existing=rec.get("summary") or rec.get("project_title") or "n/a",
        ))
    return BATCH_INSTRUCTION.format(count=len(batch), records="\n\n".join(blocks))


def clean_summary(summary: str) -> str:
    """
    The model tends to open with a dangling pronoun ("They deployed ...").
    Strip it so summaries read as clean past-tense fragments ("Deployed ...").
    """
    cleaned = re.sub(r"^(They|The company|The firm|The team)\s+", "", summary.strip())
    return cleaned[:1].upper() + cleaned[1:] if cleaned else summary


def parse_summaries(raw: str, expected: int) -> list[str] | None:
    """Parse the LLM response into exactly `expected` non-trivial strings."""
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text).strip()

    candidate = text
    if not candidate.startswith("["):
        match = re.search(r"\[.*\]", candidate, re.DOTALL)
        if not match:
            return None
        candidate = match.group()

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None

    if not isinstance(data, list) or len(data) != expected:
        return None
    summaries = [clean_summary(str(s)) for s in data]
    if any(len(s) < 100 for s in summaries):
        return None
    return summaries


def enrich_batch(client: Groq, batch: list[dict]) -> list[str] | None:
    """One GROQ call (with retries) → list of len(batch) summaries, or None."""
    prompt = build_prompt(batch)
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                temperature=0.7,
                max_tokens=2048,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
            )
            raw = response.choices[0].message.content or ""
            summaries = parse_summaries(raw, expected=len(batch))
            if summaries:
                return summaries
            print(f"    attempt {attempt}: response did not parse into {len(batch)} summaries")
        except Exception as exc:
            print(f"    attempt {attempt}: API error: {exc}")

        if attempt < MAX_ATTEMPTS:
            delay = 2 ** attempt
            print(f"    retrying in {delay}s ...")
            time.sleep(delay)
    return None


def main() -> int:
    load_dotenv(ROOT / ".env")
    api_key = os.getenv("GROQ_API_KEY", "")
    if not api_key:
        print("ERROR: GROQ_API_KEY not set in .env")
        return 1

    raw_records = json.loads(RAW_PATH.read_text(encoding="utf-8"))

    # Start from a previous enriched file if present (idempotent re-runs)
    if ENRICHED_PATH.exists():
        records = json.loads(ENRICHED_PATH.read_text(encoding="utf-8"))
        print(f"Resuming from existing {ENRICHED_PATH.name}")
        # Apply pronoun cleanup to summaries enriched by earlier script versions
        for r in records:
            if len(r.get("summary") or "") > ENRICHED_THRESHOLD:
                r["summary"] = clean_summary(r["summary"])
    else:
        records = [dict(r) for r in raw_records]

    original_cap001 = next(
        (r.get("summary") for r in raw_records if r.get("id") == "CAP-001"), None
    )

    todo = [r for r in records if len(r.get("summary") or "") <= ENRICHED_THRESHOLD]
    print(f"{len(records)} records total — {len(todo)} to enrich, "
          f"{len(records) - len(todo)} already done.")

    client = Groq(api_key=api_key)
    api_calls = 0
    failed_batches = 0

    for i in range(0, len(todo), BATCH_SIZE):
        batch = todo[i:i + BATCH_SIZE]
        ids = ", ".join(r["id"] for r in batch)
        print(f"  batch {i // BATCH_SIZE + 1}: {ids}")
        summaries = enrich_batch(client, batch)
        api_calls += 1
        if summaries is None:
            print("    FAILED after retries — skipping batch (re-run to fill in).")
            failed_batches += 1
            continue
        for rec, summary in zip(batch, summaries):
            rec["summary"] = summary
        # Save progress after every batch so a crash loses at most one batch
        ENRICHED_PATH.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        time.sleep(1.0)   # spacing between batches for rate limits

    enriched_count = sum(1 for r in records if len(r.get("summary") or "") > ENRICHED_THRESHOLD)
    print(f"\nDone: {enriched_count}/{len(records)} enriched, "
          f"{api_calls} API calls, {failed_batches} failed batches.")

    cap001 = next((r for r in records if r.get("id") == "CAP-001"), None)
    if cap001 and original_cap001:
        print("\n--- CAP-001 BEFORE ---")
        print(original_cap001)
        print("\n--- CAP-001 AFTER ---")
        print(cap001["summary"])

    return 0 if (failed_batches == 0 and enriched_count == len(records)) else 1


if __name__ == "__main__":
    sys.exit(main())
