"""Probe which Supabase migrations are live by selecting the new columns."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
from app.utils.supabase_client import get_supabase

sb = get_supabase()
checks = [
    ("workspaces.sector",                "workspaces", "sector"),
    ("workspaces.issuer_type",           "workspaces", "issuer_type"),
    ("workspaces.warning",               "workspaces", "warning"),
    ("workspaces.competitor_presence",   "workspaces", "competitor_presence"),
    ("workspaces.pipeline_timings",      "workspaces", "pipeline_timings"),
    ("requirements.extraction_source",   "requirements", "extraction_source"),
    ("requirements.taxonomy_id",         "requirements", "taxonomy_id"),
    ("proposal_sections.requirement_id", "proposal_sections", "requirement_id"),
    ("proposal_sections.section_title",  "proposal_sections", "section_title"),
    ("bid_scores (renamed)",             "bid_scores", "id"),
]
missing = []
for label, table, column in checks:
    try:
        sb.table(table).select(column).limit(1).execute()
        print(f"[OK]      {label}")
    except Exception:
        print(f"[MISSING] {label}")
        missing.append(label)

print()
print("ALL MIGRATIONS APPLIED" if not missing else f"MISSING: {missing}")
